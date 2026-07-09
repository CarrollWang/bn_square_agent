from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
import smtplib
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import httpx
from pydantic import BaseModel, Field

from .ai.llm import StructuredLLM
from .ai.material_tagger import MaterialTagger
from .core.config import (
    AccountConfig,
    Settings,
    add_no_proxy_host,
    mask_url_credentials,
    normalize_proxy_url,
)
from .publishing.account_check import BinanceAccountChecker
from .publishing.mcp_client import RemoteMCPClient
from .services import build_services
from .sources.binance_square import MaterialSourceService
from .storage.database import Database


PACKAGE_DIR = Path(__file__).resolve().parent
DIST_DIR = PACKAGE_DIR / "dist"
SOURCE_CHECK_TIMEOUT_SECONDS = 90

monitor_state: dict[str, Any] = {
    "running": False,
    "last_started_at": None,
    "last_finished_at": None,
    "last_results": [],
    "last_consume_results": [],
    "last_tag_results": [],
    "last_error": None,
    "expired_count": 0,
    "next_run_after_seconds": None,
    "next_run_reason": "poll",
    "current_stage": None,
    "consecutive_publish_failures": 0,
    "last_alert_at": None,
    "last_alert_error": None,
    "last_alert_sent": False,
}


def _consume_results_have_failure(consume_results: list[dict[str, Any]]) -> bool:
    for item in consume_results:
        for run in item.get("runs") or []:
            if run.get("error") or run.get("publish_success") is False:
                return True
    return False


def _consume_results_have_success(consume_results: list[dict[str, Any]]) -> bool:
    for item in consume_results:
        for run in item.get("runs") or []:
            if run.get("publish_success") is True:
                return True
    return False


def _consume_results_failure_count(consume_results: list[dict[str, Any]]) -> int:
    count = 0
    for item in consume_results:
        runs = item.get("runs") or []
        if not runs and item.get("error"):
            count += 1
        for run in runs:
            if run.get("error") or run.get("publish_success") is False:
                count += 1
    return count


def _send_publish_failure_alert_email(
    settings: Settings,
    failure_count: int,
    consume_results: list[dict[str, Any]],
) -> tuple[bool, str | None]:
    if not settings.alert_email_enabled:
        return False, "邮件提醒未开启"
    missing = [
        name
        for name, value in (
            ("ALERT_EMAIL_TO", settings.alert_email_to),
            ("SMTP_HOST", settings.smtp_host),
            ("SMTP_FROM", settings.smtp_from or settings.smtp_username),
        )
        if not value
    ]
    if missing:
        return False, f"邮件提醒缺少配置: {', '.join(missing)}"

    lines = [
        f"BN Square Agent 已连续 {failure_count} 次发文失效，自动循环已暂停。",
        "",
        "最近失败记录：",
    ]
    for item in consume_results[:5]:
        title = item.get("title") or f"material#{item.get('material_item_id')}"
        lines.append(f"- {title}")
        for run in item.get("runs") or []:
            error = run.get("error") or run.get("publish_result") or "未知错误"
            lines.append(f"  账号 {run.get('account_key')}: {error}")

    message = EmailMessage()
    message["Subject"] = f"BN Square Agent 连续 {failure_count} 次发文失效"
    message["From"] = settings.smtp_from or settings.smtp_username
    message["To"] = settings.alert_email_to
    message.set_content("\n".join(lines))

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
            if settings.smtp_use_tls:
                smtp.starttls()
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)
        return True, None
    except Exception as exc:
        return False, str(exc)


def _update_publish_failure_guard(
    settings: Settings,
    db: Database,
    consume_results: list[dict[str, Any]],
) -> None:
    if not consume_results:
        return
    if _consume_results_have_success(consume_results):
        monitor_state["consecutive_publish_failures"] = 0
        monitor_state["last_alert_error"] = None
        monitor_state["last_alert_sent"] = False
        return

    failed_count = _consume_results_failure_count(consume_results)
    if not failed_count:
        return

    current_count = int(monitor_state.get("consecutive_publish_failures") or 0)
    current_count += failed_count
    monitor_state["consecutive_publish_failures"] = current_count

    threshold = max(1, settings.publish_failure_alert_threshold)
    if current_count < threshold:
        return

    sent, error = _send_publish_failure_alert_email(
        settings,
        current_count,
        consume_results,
    )
    db.set_app_settings({"AUTO_MONITOR_ENABLED": "0"})
    monitor_state["last_alert_at"] = datetime.now(timezone.utc).isoformat()
    monitor_state["last_alert_sent"] = sent
    monitor_state["last_alert_error"] = error
    monitor_state["next_run_after_seconds"] = _paused_monitor_delay(settings)
    monitor_state["next_run_reason"] = "paused_after_failures"
    monitor_state["current_stage"] = "连续发文失效，自动循环已暂停"


def _next_monitor_delay(settings: Settings, result: dict[str, Any]) -> tuple[int, str]:
    consume_results = result.get("consume_results") or []
    source_results = result.get("results") or []
    if _consume_results_have_failure(consume_results):
        return max(30, settings.material_failure_interval_seconds), "publish_failed"
    if consume_results:
        return max(30, settings.material_success_interval_seconds), "published"
    if any(item.get("error") for item in source_results):
        return max(30, settings.material_failure_interval_seconds), "collect_failed"
    return max(30, settings.material_poll_interval_seconds), "poll"


def _paused_monitor_delay(settings: Settings) -> int:
    return max(10, min(settings.material_poll_interval_seconds, 60))


async def run_material_monitor_once() -> dict[str, Any]:
    settings = get_settings()
    db = get_db()
    monitor_state["running"] = True
    monitor_state["current_stage"] = "清理过期素材"
    monitor_state["last_started_at"] = datetime.now(timezone.utc).isoformat()
    monitor_state["last_finished_at"] = None
    try:
        expired_count = db.expire_stale_material_items(
            ttl_seconds=settings.material_ttl_seconds
        )
        monitor_state["expired_count"] = expired_count
        monitor_state["current_stage"] = "采集素材源"
        try:
            results = await asyncio.wait_for(
                asyncio.to_thread(MaterialSourceService(db).check_all),
                timeout=SOURCE_CHECK_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            results = [
                {
                    "source_id": "all",
                    "found": 0,
                    "inserted": 0,
                    "error": f"采集超时 {SOURCE_CHECK_TIMEOUT_SECONDS}s，已跳过本轮采集",
                }
            ]
        monitor_state["last_results"] = results
        monitor_state["current_stage"] = "素材打标"
        tag_results: list[dict[str, Any]] = []
        tagger = MaterialTagger()
        for material in db.pending_material_items_for_tagging(limit=100):
            try:
                tag = tagger.tag(
                    title=material.get("title"),
                    content=material["content"],
                )
                tag_status = "accepted" if tag.accepted else "rejected"
                db.save_material_tag(
                    material["id"],
                    tag_status=tag_status,
                    tag=tag.to_dict(),
                )
                tag_results.append(
                    {
                        "material_item_id": material["id"],
                        "tag_status": tag_status,
                        "tag": tag.to_dict(),
                    }
                )
            except Exception as exc:
                db.save_material_tag(
                    material["id"],
                    tag_status="failed",
                    error=str(exc),
                )
                tag_results.append(
                    {
                        "material_item_id": material["id"],
                        "tag_status": "failed",
                        "error": str(exc),
                    }
                )
        monitor_state["last_tag_results"] = tag_results
        monitor_state["current_stage"] = "等待消费素材"
        consume_results: list[dict[str, Any]] = []
        if settings.auto_consume_materials:
            materials = db.list_material_items(
                status="new",
                tag_status="accepted",
                limit=settings.material_consume_batch_size,
            )
            if materials:
                services = await asyncio.to_thread(build_services)
                for material in materials:
                    monitor_state["current_stage"] = (
                        f"消费素材 material#{material['id']}"
                    )
                    runs = await asyncio.to_thread(
                        services.operator.run_material_item_for_all_accounts,
                        material["id"],
                    )
                    consume_results.append(
                        {
                            "material_item_id": material["id"],
                            "title": material.get("title"),
                            "runs": [
                                {
                                    "account_key": run.account_key,
                                    "approved_generated_id": run.approved_generated_id,
                                    "error": run.error,
                                    "publish_success": run.publish_result.success
                                    if run.publish_result
                                    else None,
                                    "publish_result": run.publish_result.result
                                    if run.publish_result
                                    else None,
                                }
                                for run in runs
                            ],
                        }
                    )
                    monitor_state["last_consume_results"] = consume_results
        monitor_state.update(
            {
                "last_results": results,
                "last_tag_results": tag_results,
                "last_consume_results": consume_results,
                "last_error": None,
                "expired_count": expired_count,
            }
        )
        _update_publish_failure_guard(settings, db, consume_results)
        return {
            "expired_count": expired_count,
            "results": results,
            "consume_results": consume_results,
        }
    except Exception as exc:
        monitor_state["last_error"] = str(exc)
        raise
    finally:
        monitor_state["running"] = False
        if monitor_state.get("next_run_reason") != "paused_after_failures":
            monitor_state["current_stage"] = None
        monitor_state["last_finished_at"] = datetime.now(timezone.utc).isoformat()


async def material_monitor_loop() -> None:
    while True:
        settings = get_settings()
        if not settings.auto_monitor_enabled:
            monitor_state["running"] = False
            monitor_state["current_stage"] = "自动循环已暂停"
            monitor_state["next_run_after_seconds"] = _paused_monitor_delay(settings)
            monitor_state["next_run_reason"] = "paused"
            await asyncio.sleep(_paused_monitor_delay(settings))
            continue
        delay_seconds = max(30, settings.material_poll_interval_seconds)
        reason = "poll"
        try:
            result = await run_material_monitor_once()
            latest_settings = get_settings()
            if not latest_settings.auto_monitor_enabled:
                delay_seconds = _paused_monitor_delay(latest_settings)
                reason = monitor_state.get("next_run_reason") or "paused"
            else:
                delay_seconds, reason = _next_monitor_delay(settings, result)
        except Exception:
            delay_seconds = max(30, settings.material_failure_interval_seconds)
            reason = "error"
        monitor_state["next_run_after_seconds"] = delay_seconds
        monitor_state["next_run_reason"] = reason
        await asyncio.sleep(delay_seconds)


@asynccontextmanager
async def lifespan(app_: FastAPI):
    task = asyncio.create_task(material_monitor_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="BN Square Agent", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=DIST_DIR), name="static")


class AccountPayload(BaseModel):
    account_key: str = Field(min_length=1)
    name: str | None = None
    cookie: str | None = None
    proxy_url: str | None = None
    mcp_url: str | None = None
    mcp_auth_token: str | None = None


class RunPayload(BaseModel):
    content: str = Field(min_length=1)
    title: str | None = None
    url: str | None = None
    auto_publish: bool = True


class SettingsPayload(BaseModel):
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None
    dashscope_api_key: str | None = None
    dashscope_embedding_model: str | None = None
    mcp_url: str | None = None
    mcp_publish_tool: str | None = None
    mcp_auth_token: str | None = None
    auto_publish: bool | None = None
    auto_monitor_enabled: bool | None = None
    auto_consume_materials: bool | None = None
    material_poll_interval_seconds: int | None = None
    material_success_interval_seconds: int | None = None
    material_failure_interval_seconds: int | None = None
    material_ttl_seconds: int | None = None
    material_consume_batch_size: int | None = None
    publish_failure_alert_threshold: int | None = None
    alert_email_enabled: bool | None = None
    alert_email_to: str | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_use_tls: bool | None = None


class MaterialSourcePayload(BaseModel):
    name: str = Field(min_length=1)
    url: str = Field(min_length=1)
    source_type: str = "binance_square"
    enabled: bool = True


class RunMaterialPayload(BaseModel):
    material_item_id: int
    auto_publish: bool = True


class LLMTestResult(BaseModel):
    ok: bool
    message: str


def mask_secret(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * 8}{value[-4:]}"


def is_masked_secret(value: str | None) -> bool:
    if not value:
        return False
    return "*" in value or "•" in value


def fetch_openai_models(settings: Settings) -> list[str]:
    missing = [
        name
        for name, value in (
            ("LLM_API_KEY", settings.llm_api_key),
            ("LLM_BASE_URL", settings.llm_base_url),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"缺少配置: {', '.join(missing)}")

    url = f"{settings.llm_base_url.rstrip('/')}/models"
    with httpx.Client(trust_env=False, timeout=20) as client:
        response = client.get(
            url,
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        )
        response.raise_for_status()
        payload = response.json()

    data = payload.get("data", payload)
    if not isinstance(data, list):
        raise ValueError("模型接口返回格式不正确")

    models = []
    for item in data:
        if isinstance(item, str):
            models.append(item)
        elif isinstance(item, dict) and item.get("id"):
            models.append(str(item["id"]))
    return sorted(dict.fromkeys(models))


def get_settings() -> Settings:
    base = Settings.from_env()
    db = base.build_database()
    return base.with_overrides(db.get_app_settings())


def get_db() -> Database:
    return Settings.from_env().build_database()


def _account_from_row(row: dict[str, Any]) -> AccountConfig:
    return AccountConfig(
        key=row["account_key"],
        name=row["name"],
        cookie=row.get("cookie") or "",
        proxy_url=row.get("proxy_url") or "",
        mcp_url=row.get("mcp_url") or "",
        mcp_auth_token=row.get("mcp_auth_token") or "",
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(DIST_DIR / "index.html")


@app.get("/api/accounts")
def list_accounts() -> list[dict]:
    rows = get_db().list_accounts()
    return [
        {
            "account_key": row["account_key"],
            "name": row["name"],
            "enabled": bool(row["enabled"]),
            "cookie_saved": bool(row["cookie"]),
            "cookie_length": len(row["cookie"] or ""),
            "cookie_names": [
                item["name"]
                for item in BinanceAccountChecker._parse_cookie_header(row["cookie"] or "")
            ],
            "check_status": row.get("check_status"),
            "checked_at": row.get("checked_at"),
            "check_error": row.get("check_error"),
            "proxy_configured": bool(row.get("proxy_url")),
            "proxy_url_masked": mask_url_credentials(row.get("proxy_url") or ""),
            "mcp_url": row.get("mcp_url") or "",
            "mcp_auth_token_configured": bool(row.get("mcp_auth_token")),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


@app.get("/api/accounts/{account_key}")
def read_account(account_key: str) -> dict:
    account = next(
        (
            row
            for row in get_db().list_accounts(include_disabled=True)
            if row["account_key"] == account_key
        ),
        None,
    )
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    return {
        "account_key": account["account_key"],
        "name": account["name"],
        "cookie_saved": bool(account.get("cookie")),
        "proxy_url": account.get("proxy_url") or "",
        "mcp_url": account.get("mcp_url") or "",
        "mcp_auth_token_configured": bool(account.get("mcp_auth_token")),
    }


@app.get("/api/settings")
def read_settings() -> dict:
    settings = get_settings()
    return {
        "llm_api_key_configured": bool(settings.llm_api_key),
        "llm_api_key_masked": mask_secret(settings.llm_api_key),
        "llm_base_url": settings.llm_base_url,
        "llm_model": settings.llm_model,
        "llm_model_options": [settings.llm_model] if settings.llm_model else [],
        "dashscope_api_key_configured": bool(settings.dashscope_api_key),
        "dashscope_api_key_masked": mask_secret(settings.dashscope_api_key),
        "dashscope_embedding_model": settings.dashscope_embedding_model,
        "mcp_url": settings.mcp_url,
        "mcp_publish_tool": settings.mcp_publish_tool,
        "mcp_auth_token_configured": bool(settings.mcp_auth_token),
        "mcp_auth_token_masked": mask_secret(settings.mcp_auth_token),
        "auto_monitor_enabled": settings.auto_monitor_enabled,
        "auto_publish": settings.auto_publish,
        "auto_consume_materials": settings.auto_consume_materials,
        "material_poll_interval_seconds": settings.material_poll_interval_seconds,
        "material_success_interval_seconds": settings.material_success_interval_seconds,
        "material_failure_interval_seconds": settings.material_failure_interval_seconds,
        "material_ttl_seconds": settings.material_ttl_seconds,
        "material_consume_batch_size": settings.material_consume_batch_size,
        "publish_failure_alert_threshold": settings.publish_failure_alert_threshold,
        "alert_email_enabled": settings.alert_email_enabled,
        "alert_email_to": settings.alert_email_to,
        "smtp_host": settings.smtp_host,
        "smtp_port": settings.smtp_port,
        "smtp_username": settings.smtp_username,
        "smtp_password_configured": bool(settings.smtp_password),
        "smtp_password_masked": mask_secret(settings.smtp_password),
        "smtp_from": settings.smtp_from,
        "smtp_use_tls": settings.smtp_use_tls,
    }


@app.post("/api/settings")
def save_settings(payload: SettingsPayload) -> dict:
    values: dict[str, str] = {}
    normal_fields = {
        "llm_base_url": "LLM_BASE_URL",
        "llm_model": "LLM_MODEL",
        "dashscope_embedding_model": "DASHSCOPE_EMBEDDING_MODEL",
        "mcp_url": "MCP_URL",
        "mcp_publish_tool": "MCP_PUBLISH_TOOL",
        "alert_email_to": "ALERT_EMAIL_TO",
        "smtp_host": "SMTP_HOST",
        "smtp_username": "SMTP_USERNAME",
        "smtp_from": "SMTP_FROM",
    }
    secret_fields = {
        "llm_api_key": "LLM_API_KEY",
        "dashscope_api_key": "DASHSCOPE_API_KEY",
        "mcp_auth_token": "MCP_AUTH_TOKEN",
        "smtp_password": "SMTP_PASSWORD",
    }
    bool_fields = {
        "auto_monitor_enabled": "AUTO_MONITOR_ENABLED",
        "auto_publish": "AUTO_PUBLISH",
        "auto_consume_materials": "AUTO_CONSUME_MATERIALS",
        "alert_email_enabled": "ALERT_EMAIL_ENABLED",
        "smtp_use_tls": "SMTP_USE_TLS",
    }
    int_fields = {
        "material_poll_interval_seconds": "MATERIAL_POLL_INTERVAL_SECONDS",
        "material_success_interval_seconds": "MATERIAL_SUCCESS_INTERVAL_SECONDS",
        "material_failure_interval_seconds": "MATERIAL_FAILURE_INTERVAL_SECONDS",
        "material_ttl_seconds": "MATERIAL_TTL_SECONDS",
        "material_consume_batch_size": "MATERIAL_CONSUME_BATCH_SIZE",
        "publish_failure_alert_threshold": "PUBLISH_FAILURE_ALERT_THRESHOLD",
        "smtp_port": "SMTP_PORT",
    }
    data = payload.model_dump()
    for field, key in normal_fields.items():
        value = data.get(field)
        if value is not None:
            values[key] = str(value).strip()
    for field, key in secret_fields.items():
        value = data.get(field)
        if value:
            secret_value = str(value).strip()
            if not is_masked_secret(secret_value):
                values[key] = secret_value
    for field, key in bool_fields.items():
        value = data.get(field)
        if value is not None:
            values[key] = "1" if value else "0"
    for field, key in int_fields.items():
        value = data.get(field)
        if value is not None:
            values[key] = str(max(1, int(value)))
    get_db().set_app_settings(values)
    return {"ok": True, "saved": sorted(values)}


@app.post("/api/settings/test-llm")
def test_llm() -> dict:
    settings = get_settings()
    try:
        settings.validate_for_llm()
        llm = StructuredLLM(settings)
        result = llm.invoke(
            system_prompt="你是连接测试助手。只返回符合 schema 的 JSON。",
            user_prompt="返回 ok=true，message='LLM 连接正常'。",
            response_model=LLMTestResult,
            retries=1,
        )
        return result.model_dump()
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


@app.post("/api/settings/models")
def list_llm_models() -> dict:
    settings = get_settings()
    try:
        models = fetch_openai_models(settings)
        return {"ok": True, "models": models}
    except Exception as exc:
        return {"ok": False, "message": str(exc), "models": []}


@app.post("/api/settings/test-embedding")
def test_embedding() -> dict:
    settings = get_settings()
    try:
        settings.validate_for_rag()
        add_no_proxy_host("dashscope.aliyuncs.com")
        from langchain_community.embeddings import DashScopeEmbeddings

        embeddings = DashScopeEmbeddings(
            model=settings.dashscope_embedding_model,
            dashscope_api_key=settings.dashscope_api_key,
        )
        vector = embeddings.embed_query("embedding 连接测试")
        return {
            "ok": True,
            "message": "Embedding 连接正常",
            "dimension": len(vector),
            "model": settings.dashscope_embedding_model,
        }
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


@app.post("/api/accounts")
def save_account(payload: AccountPayload) -> dict:
    db = get_db()
    key = payload.account_key.strip()
    existing = next(
        (row for row in db.list_accounts(include_disabled=True) if row["account_key"] == key),
        None,
    )
    raw_name = payload.name if payload.name is not None else (
        existing.get("name") if existing else key
    )
    name = raw_name.strip() if raw_name else key
    cookie = payload.cookie.strip() if payload.cookie is not None else None
    proxy_url = payload.proxy_url if payload.proxy_url is not None else None
    mcp_url = payload.mcp_url.strip() if payload.mcp_url is not None else None
    mcp_auth_token = (
        payload.mcp_auth_token.strip()
        if payload.mcp_auth_token is not None
        else None
    )
    try:
        if proxy_url is not None:
            proxy_url = normalize_proxy_url(proxy_url) if proxy_url.strip() else ""
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not key:
        raise HTTPException(status_code=400, detail="账号标识必填")
    if existing is None and not cookie:
        raise HTTPException(status_code=400, detail="新账号必须提供 Cookie")
    db.upsert_account(
        account_key=key,
        name=name,
        cookie=cookie,
        proxy_url=proxy_url,
        mcp_url=mcp_url,
        mcp_auth_token=mcp_auth_token,
    )
    return {"ok": True}


@app.delete("/api/accounts/{account_key}")
def delete_account(account_key: str) -> dict:
    get_db().disable_account(account_key)
    return {"ok": True}


@app.post("/api/accounts/{account_key}/check")
def check_account(account_key: str) -> dict:
    db = get_db()
    account = next(
        (row for row in db.list_accounts() if row["account_key"] == account_key),
        None,
    )
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    if not account["cookie"]:
        raise HTTPException(status_code=400, detail="账号缺少 Cookie")
    result = BinanceAccountChecker().check(
        account["cookie"],
        proxy_url=account.get("proxy_url") or "",
    )
    status = "valid" if result.valid else "invalid"
    db.update_account_check(
        account_key,
        signature_key=None,
        status=status,
        error=result.error,
    )
    return {
        "valid": result.valid,
        "error": result.error,
    }


@app.get("/api/mcp/tools")
def mcp_tools() -> dict:
    settings = get_settings()
    selected_account: dict[str, Any] | None = None
    mcp_url = settings.mcp_url
    mcp_auth_token = settings.mcp_auth_token
    if not mcp_url:
        for row in get_db().list_accounts():
            if row.get("mcp_url"):
                selected_account = row
                mcp_url = row["mcp_url"]
                mcp_auth_token = row.get("mcp_auth_token") or settings.mcp_auth_token
                break
    if not mcp_url:
        raise HTTPException(
            status_code=400,
            detail="未配置全局 MCP_URL，也没有账号配置独立 MCP 地址",
        )
    client = RemoteMCPClient(mcp_url, auth_token=mcp_auth_token)
    client.initialize()
    tools = client.list_tools()
    return {
        "mcp_url": mcp_url,
        "account_key": selected_account["account_key"] if selected_account else None,
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "required": tool.input_schema.get("required", [])
                if tool.input_schema
                else [],
            }
            for tool in tools
        ],
    }


@app.post("/api/run")
def run(payload: RunPayload) -> dict:
    services = build_services()
    accounts = [_account_from_row(row) for row in services.db.list_accounts()]
    if not accounts:
        raise HTTPException(status_code=400, detail="请先添加至少一个账号 Cookie")
    services.operator.accounts = tuple(accounts)
    services.operator.auto_publish = payload.auto_publish
    runs = services.operator.generate_for_all_accounts(
        content=payload.content,
        title=payload.title,
        url=payload.url,
    )
    return {
        "runs": [
            {
                "account_key": run.account_key,
                "generated_ids": run.generated_ids,
                "approved_generated_id": run.approved_generated_id,
                "error": run.error,
                "publish_result": {
                    "success": run.publish_result.success,
                    "result": run.publish_result.result,
                }
                if run.publish_result
                else None,
            }
            for run in runs
        ]
    }


@app.get("/api/material-sources")
def list_material_sources() -> list[dict]:
    return get_db().list_material_sources(include_disabled=True)


@app.post("/api/material-sources")
def save_material_source(payload: MaterialSourcePayload) -> dict:
    if payload.source_type not in {"binance_square", "techflow_newsletter"}:
        raise HTTPException(status_code=400, detail="当前只支持 BN 广场和 TechFlow 快讯素材源")
    source_id = get_db().upsert_material_source(
        name=payload.name.strip(),
        source_type=payload.source_type,
        url=payload.url.strip(),
        enabled=payload.enabled,
    )
    return {"ok": True, "source_id": source_id}


@app.delete("/api/material-sources/{source_id}")
def delete_material_source(source_id: int) -> dict:
    get_db().disable_material_source(source_id)
    return {"ok": True}


@app.post("/api/material-sources/check")
async def check_material_sources() -> dict:
    return await run_material_monitor_once()


@app.post("/api/material-sources/{source_id}/check")
def check_material_source(source_id: int) -> dict:
    db = get_db()
    source = next(
        (item for item in db.list_material_sources(include_disabled=True) if item["id"] == source_id),
        None,
    )
    if not source:
        raise HTTPException(status_code=404, detail="素材源不存在")
    return MaterialSourceService(db).check_source(source)


@app.get("/api/material-items")
def list_material_items(status: str | None = "new", limit: int = 50) -> list[dict]:
    return get_db().list_material_items(status=status, limit=limit)


@app.get("/api/material-monitor")
def material_monitor_status() -> dict:
    settings = get_settings()
    return {
        **monitor_state,
        "poll_interval_seconds": settings.material_poll_interval_seconds,
        "success_interval_seconds": settings.material_success_interval_seconds,
        "failure_interval_seconds": settings.material_failure_interval_seconds,
        "ttl_seconds": settings.material_ttl_seconds,
        "auto_consume_materials": settings.auto_consume_materials,
        "auto_monitor_enabled": settings.auto_monitor_enabled,
        "consume_batch_size": settings.material_consume_batch_size,
        "publish_failure_alert_threshold": settings.publish_failure_alert_threshold,
        "alert_email_enabled": settings.alert_email_enabled,
        "alert_email_configured": bool(
            settings.alert_email_to
            and settings.smtp_host
            and (settings.smtp_from or settings.smtp_username)
        ),
    }


class MonitorEnabledPayload(BaseModel):
    enabled: bool


@app.post("/api/material-monitor/enabled")
def set_material_monitor_enabled(payload: MonitorEnabledPayload) -> dict:
    get_db().set_app_settings(
        {"AUTO_MONITOR_ENABLED": "1" if payload.enabled else "0"}
    )
    monitor_state["next_run_reason"] = "poll" if payload.enabled else "paused"
    monitor_state["current_stage"] = None if payload.enabled else "自动循环已暂停"
    return {"ok": True, "enabled": payload.enabled}


@app.post("/api/material-items/run")
def run_material_item(payload: RunMaterialPayload) -> dict:
    services = build_services()
    accounts = [_account_from_row(row) for row in services.db.list_accounts()]
    if not accounts:
        raise HTTPException(status_code=400, detail="请先添加至少一个账号 Cookie")
    services.operator.accounts = tuple(accounts)
    services.operator.auto_publish = payload.auto_publish
    runs = services.operator.run_material_item_for_all_accounts(payload.material_item_id)
    return {
        "runs": [
            {
                "account_key": run.account_key,
                "generated_ids": run.generated_ids,
                "approved_generated_id": run.approved_generated_id,
                "error": run.error,
                "publish_result": {
                    "success": run.publish_result.success,
                    "result": run.publish_result.result,
                }
                if run.publish_result
                else None,
            }
            for run in runs
        ]
    }
