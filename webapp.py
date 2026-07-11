from __future__ import annotations

import asyncio
import base64
import binascii
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
import json
import ipaddress
import logging
import os
import secrets
from pathlib import Path
import smtplib
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
import httpx
from pydantic import BaseModel, Field

from .ai.llm import StructuredLLM
from .ai.material_tagger import MaterialTagger
from .core.config import (
    AccountConfig,
    Settings,
    mask_url_credentials,
    normalize_proxy_url,
)
from .knowledge.style_rag import create_embeddings
from .publishing.mcp_client import RemoteMCPClient
from .services import build_services
from .sources.binance_square import MaterialSourceService
from .storage.database import Database
from .core.url_policy import validate_binance_url, validate_techflow_url


PACKAGE_DIR = Path(__file__).resolve().parent
DIST_DIR = PACKAGE_DIR / "dist"
MONITOR_LOCK_NAME = "material_monitor"
MONITOR_LOCK_LEASE_SECONDS = 30 * 60
MONITOR_LOCK_RENEW_SECONDS = 5 * 60
MAX_REQUEST_BODY_BYTES = 20 * 1024 * 1024
LOGGER = logging.getLogger(__name__)

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
    "account_queue_cursor": 0,
}
def _serialize_account_run(run: Any) -> dict[str, Any]:
    publish_result = getattr(run, "publish_result", None)
    return {
        "account_key": run.account_key,
        "status": getattr(run, "status", None),
        "generated_ids": list(getattr(run, "generated_ids", []) or []),
        "approved_generated_id": getattr(run, "approved_generated_id", None),
        "skipped_reason": getattr(run, "skipped_reason", None),
        "error": getattr(run, "error", None),
        "publish_success": publish_result.success if publish_result else None,
        "publish_result": publish_result.result if publish_result else None,
    }


def _serialize_consume_result(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "material_item_id": item.get("material_item_id"),
        "title": item.get("title"),
        "account_key": item.get("account_key"),
        "runs": [_serialize_account_run(run) for run in item.get("runs") or []],
    }


def _monitor_locked_result(settings: Settings) -> dict[str, Any]:
    monitor_state["running"] = False
    monitor_state["current_stage"] = "已有其他运行实例，本轮跳过"
    monitor_state["last_error"] = None
    monitor_state["last_finished_at"] = datetime.now(timezone.utc).isoformat()
    monitor_state["next_run_after_seconds"] = _paused_monitor_delay(settings)
    monitor_state["next_run_reason"] = "locked"
    return {
        "skipped": True,
        "reason": "locked",
        "expired_count": monitor_state.get("expired_count", 0),
        "results": [],
        "consume_results": [],
    }


def _acquire_pipeline_lock_or_raise(db: Database) -> str:
    owner_id = uuid4().hex
    locked = db.try_acquire_job_lock(
        MONITOR_LOCK_NAME,
        owner_id=owner_id,
        lease_seconds=MONITOR_LOCK_LEASE_SECONDS,
    )
    if not locked:
        raise HTTPException(status_code=409, detail="当前已有一轮任务在运行，请稍后再试")
    return owner_id


async def _renew_pipeline_lock(
    db: Database,
    *,
    owner_id: str,
    stop: asyncio.Event,
) -> None:
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=MONITOR_LOCK_RENEW_SECONDS)
            return
        except asyncio.TimeoutError:
            try:
                renewed = await asyncio.to_thread(
                    db.renew_job_lock,
                    MONITOR_LOCK_NAME,
                    owner_id=owner_id,
                    lease_seconds=MONITOR_LOCK_LEASE_SECONDS,
                )
            except Exception:
                LOGGER.exception("Failed to renew material monitor lock")
                continue
            if not renewed:
                LOGGER.error("Material monitor lock ownership was lost")
                return


def _round_percent(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator * 100 / denominator, 1)


def _max_iso(values: list[str | None]) -> str | None:
    candidates = [value for value in values if value]
    return max(candidates) if candidates else None


def _account_health(
    *,
    check_status: str | None,
    published_count: int,
    failed_count: int,
    skipped_count: int,
) -> tuple[str, str]:
    if check_status in {"invalid", "missing"}:
        return "配置缺失", "danger"
    attempts = published_count + failed_count
    success_rate = published_count / attempts if attempts else 0.0
    if published_count >= 5 and success_rate >= 0.8:
        return "优秀", "success"
    if published_count >= 1 and success_rate >= 0.6:
        return "稳定", "success"
    if failed_count >= max(2, published_count + 1):
        return "观察", "warning"
    if skipped_count >= max(3, published_count + 1):
        return "受限", "warning"
    if published_count == 0 and failed_count == 0 and skipped_count == 0:
        return "空闲", "info"
    return "观察", "warning"


def _consume_results_have_failure(consume_results: list[dict[str, Any]]) -> bool:
    for item in consume_results:
        for run in item.get("runs") or []:
            if (run.get("publish_result") or {}).get("outcome") == "rate_limited":
                continue
            if run.get("error") or run.get("publish_success") is False:
                return True
    return False


def _consume_results_have_success(consume_results: list[dict[str, Any]]) -> bool:
    for item in consume_results:
        for run in item.get("runs") or []:
            if run.get("publish_success") is True:
                return True
    return False


def _consume_results_have_rate_limit(consume_results: list[dict[str, Any]]) -> bool:
    for item in consume_results:
        for run in item.get("runs") or []:
            if (run.get("publish_result") or {}).get("outcome") == "rate_limited":
                return True
    return False


def _consume_results_failure_count(consume_results: list[dict[str, Any]]) -> int:
    count = 0
    for item in consume_results:
        runs = item.get("runs") or []
        if not runs and item.get("error"):
            count += 1
        for run in runs:
            if (run.get("publish_result") or {}).get("outcome") == "rate_limited":
                continue
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
    if _consume_results_have_rate_limit(consume_results):
        return 60 * 60, "rate_limited"
    if consume_results:
        return max(30, settings.material_success_interval_seconds), "published"
    if any(item.get("error") for item in source_results):
        return max(30, settings.material_failure_interval_seconds), "collect_failed"
    return max(30, settings.material_poll_interval_seconds), "poll"


def _paused_monitor_delay(settings: Settings) -> int:
    return max(10, min(settings.material_poll_interval_seconds, 60))


async def run_material_monitor_once(*, fail_if_locked: bool = False) -> dict[str, Any]:
    settings = get_settings()
    db = get_db()
    lock_owner = uuid4().hex
    if not db.try_acquire_job_lock(
        MONITOR_LOCK_NAME,
        owner_id=lock_owner,
        lease_seconds=MONITOR_LOCK_LEASE_SECONDS,
    ):
        result = _monitor_locked_result(settings)
        if fail_if_locked:
            raise HTTPException(status_code=409, detail="当前已有一轮任务在运行，请稍后再试")
        return result
    lock_heartbeat_stop = asyncio.Event()
    lock_heartbeat = asyncio.create_task(
        _renew_pipeline_lock(
            db,
            owner_id=lock_owner,
            stop=lock_heartbeat_stop,
        )
    )
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
        # 每个采集器已有自己的网络超时。这里等待线程真实结束，避免 wait_for
        # 超时后后台线程仍继续写库并与下一轮任务重叠。
        results = await asyncio.to_thread(MaterialSourceService(db).check_all)
        monitor_state["last_results"] = results
        monitor_state["current_stage"] = "素材打标"
        tag_results: list[dict[str, Any]] = []
        tagger = MaterialTagger()
        for material in db.pending_material_items_for_tagging(
            limit=100,
            strategy=MaterialTagger.STRATEGY,
        ):
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
                        "title": material.get("title"),
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
                        "title": material.get("title"),
                        "tag_status": "failed",
                        "error": str(exc),
                    }
                )
        monitor_state["last_tag_results"] = tag_results
        monitor_state["current_stage"] = "等待消费素材"
        consume_results: list[dict[str, Any]] = []
        if settings.auto_consume_materials:
            queue_candidates = db.list_material_items(
                status="new",
                tag_status="accepted",
                limit=max(settings.material_consume_batch_size * 5, 10),
            )
            if queue_candidates:
                services = await asyncio.to_thread(build_services)
                queue_cursor = int(monitor_state.get("account_queue_cursor") or 0)
                queue_runs = await asyncio.to_thread(
                    services.operator.run_pending_material_queue,
                    limit_per_account=max(1, settings.material_consume_batch_size),
                    account_offset=queue_cursor,
                    max_total_runs=max(1, settings.material_consume_batch_size),
                )
                monitor_state["account_queue_cursor"] = queue_cursor + 1
                for item in queue_runs:
                    monitor_state["current_stage"] = (
                        f"账号 {item.get('account_key') or '-'} 消费素材 material#{item['material_item_id']}"
                    )
                    consume_results.append(_serialize_consume_result(item))
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
        lock_heartbeat_stop.set()
        await lock_heartbeat
        try:
            db.release_job_lock(MONITOR_LOCK_NAME, owner_id=lock_owner)
        except Exception:
            pass
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
            elif result.get("skipped") and result.get("reason") == "locked":
                delay_seconds = _paused_monitor_delay(latest_settings)
                reason = "locked"
            else:
                delay_seconds, reason = _next_monitor_delay(settings, result)
        except Exception:
            LOGGER.exception("Material monitor loop failed")
            delay_seconds = max(30, settings.material_failure_interval_seconds)
            reason = "error"
        monitor_state["next_run_after_seconds"] = delay_seconds
        monitor_state["next_run_reason"] = reason
        await asyncio.sleep(delay_seconds)


@asynccontextmanager
async def lifespan(app_: FastAPI):
    get_db()
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
app.mount("/assets", StaticFiles(directory=DIST_DIR / "assets"), name="assets")
app.mount("/static", StaticFiles(directory=DIST_DIR), name="static")


def _basic_auth_matches(header: str, username: str, password: str) -> bool:
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return False
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    supplied_username, separator, supplied_password = decoded.partition(":")
    if not separator:
        return False
    return secrets.compare_digest(
        supplied_username.encode("utf-8"),
        username.encode("utf-8"),
    ) and secrets.compare_digest(
        supplied_password.encode("utf-8"),
        password.encode("utf-8"),
    )


def _is_loopback_client(host: str | None) -> bool:
    if not host:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() in {"localhost", "testclient"}


@app.middleware("http")
async def protect_self_hosted_console(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_REQUEST_BODY_BYTES:
                return Response(status_code=413, content="Request body too large")
        except ValueError:
            return Response(status_code=400, content="Invalid Content-Length")

    settings = Settings.from_env()
    username = settings.web_auth_username
    password = settings.web_auth_password
    if bool(username) != bool(password):
        return Response(
            status_code=503,
            content="WEB_AUTH_USERNAME 和 WEB_AUTH_PASSWORD 必须同时配置",
        )
    if username and request.url.path != "/healthz":
        if not _basic_auth_matches(
            request.headers.get("authorization", ""),
            username,
            password,
        ):
            return Response(
                status_code=401,
                content="Authentication required",
                headers={
                    "WWW-Authenticate": 'Basic realm="BN Square Agent", charset="UTF-8"'
                },
            )
    elif (
        request.url.path != "/healthz"
        and not settings.allow_insecure_public_bind
        and not _is_loopback_client(request.client.host if request.client else None)
    ):
        return Response(
            status_code=403,
            content="非本机访问必须配置 WEB_AUTH_USERNAME / WEB_AUTH_PASSWORD",
        )

    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


class AccountPayload(BaseModel):
    account_key: str = Field(min_length=1, max_length=64)
    name: str | None = Field(default=None, max_length=120)
    square_openapi_key: str | None = Field(default=None, max_length=8_192)
    proxy_url: str | None = Field(default=None, max_length=2_048)
    mcp_url: str | None = Field(default=None, max_length=2_048)
    mcp_auth_token: str | None = Field(default=None, max_length=8_192)


class RunPayload(BaseModel):
    content: str = Field(min_length=1, max_length=50_000)
    title: str | None = Field(default=None, max_length=500)
    url: str | None = Field(default=None, max_length=2_048)
    auto_publish: bool = True


class SettingsPayload(BaseModel):
    llm_api_key: str | None = Field(default=None, max_length=8_192)
    llm_base_url: str | None = Field(default=None, max_length=2_048)
    llm_model: str | None = Field(default=None, max_length=200)
    embedding_provider: str | None = Field(default=None, max_length=40)
    embedding_api_key: str | None = Field(default=None, max_length=8_192)
    embedding_base_url: str | None = Field(default=None, max_length=2_048)
    embedding_model: str | None = Field(default=None, max_length=200)
    mcp_url: str | None = Field(default=None, max_length=2_048)
    mcp_publish_tool: str | None = Field(default=None, max_length=200)
    mcp_auth_token: str | None = Field(default=None, max_length=8_192)
    auto_publish: bool | None = None
    auto_monitor_enabled: bool | None = None
    auto_consume_materials: bool | None = None
    material_poll_interval_seconds: int | None = Field(default=None, ge=10, le=86_400)
    material_success_interval_seconds: int | None = Field(default=None, ge=10, le=86_400)
    material_failure_interval_seconds: int | None = Field(default=None, ge=10, le=86_400)
    material_ttl_seconds: int | None = Field(default=None, ge=60, le=604_800)
    material_consume_batch_size: int | None = Field(default=None, ge=1, le=20)
    publish_failure_alert_threshold: int | None = Field(default=None, ge=1, le=100)
    max_posts_per_account_per_hour: int | None = Field(default=None, ge=1, le=5)
    max_posts_per_account_per_day: int | None = Field(default=None, ge=1, le=80)
    alert_email_enabled: bool | None = None
    alert_email_to: str | None = Field(default=None, max_length=1_000)
    smtp_host: str | None = Field(default=None, max_length=253)
    smtp_port: int | None = Field(default=None, ge=1, le=65_535)
    smtp_username: str | None = Field(default=None, max_length=320)
    smtp_password: str | None = Field(default=None, max_length=8_192)
    smtp_from: str | None = Field(default=None, max_length=320)
    smtp_use_tls: bool | None = None


class MaterialSourcePayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=1, max_length=2_048)
    source_type: str = "binance_square"
    enabled: bool = True


class RunMaterialPayload(BaseModel):
    material_item_id: int = Field(ge=1)
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


def publish_evidence(payload: Any) -> tuple[str | None, str | None]:
    post_id: str | None = None
    post_url: str | None = None

    def visit(value: Any) -> None:
        nonlocal post_id, post_url
        if isinstance(value, dict):
            for key, child in value.items():
                lowered = str(key).lower()
                if post_id is None and lowered in {"post_id", "postid"} and child:
                    post_id = str(child)
                if post_url is None and lowered in {
                    "post_url",
                    "posturl",
                    "sharelink",
                    "url",
                } and child:
                    post_url = str(child)
                visit(child)
            return
        if isinstance(value, list):
            for child in value:
                visit(child)
            return
        if isinstance(value, str) and value.lstrip().startswith("{"):
            try:
                visit(json.loads(value))
            except ValueError:
                pass

    visit(payload)
    if post_id:
        post_url = f"https://www.binance.com/zh-CN/square/post/{post_id}"
    return post_id, post_url


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
        square_openapi_key=row.get("square_openapi_key") or "",
        proxy_url=row.get("proxy_url") or "",
        mcp_url=row.get("mcp_url") or "",
        mcp_auth_token=row.get("mcp_auth_token") or "",
        check_status=row.get("check_status") or "unchecked",
        enabled=bool(row.get("enabled", 1)),
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(DIST_DIR / "index.html")


@app.get("/healthz")
def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/api/accounts")
def list_accounts() -> list[dict]:
    rows = get_db().list_accounts()
    return [
        {
            "account_key": row["account_key"],
            "name": row["name"],
            "enabled": bool(row["enabled"]),
            "square_openapi_key_configured": bool(row.get("square_openapi_key")),
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
        "square_openapi_key_configured": bool(account.get("square_openapi_key")),
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
        "embedding_provider": settings.embedding_provider,
        "embedding_api_key_configured": bool(settings.resolved_embedding_api_key()),
        "embedding_api_key_masked": mask_secret(settings.embedding_api_key),
        "embedding_uses_llm_credentials": bool(
            settings.embedding_provider == "openai"
            and not settings.embedding_api_key
            and settings.llm_api_key
        ),
        "embedding_base_url": settings.embedding_base_url,
        "embedding_model": settings.embedding_model,
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
        "max_posts_per_account_per_hour": settings.max_posts_per_account_per_hour,
        "max_posts_per_account_per_day": settings.max_posts_per_account_per_day,
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
        "embedding_provider": "EMBEDDING_PROVIDER",
        "embedding_base_url": "EMBEDDING_BASE_URL",
        "embedding_model": "EMBEDDING_MODEL",
        "mcp_url": "MCP_URL",
        "mcp_publish_tool": "MCP_PUBLISH_TOOL",
        "alert_email_to": "ALERT_EMAIL_TO",
        "smtp_host": "SMTP_HOST",
        "smtp_username": "SMTP_USERNAME",
        "smtp_from": "SMTP_FROM",
    }
    secret_fields = {
        "llm_api_key": "LLM_API_KEY",
        "embedding_api_key": "EMBEDDING_API_KEY",
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
        "max_posts_per_account_per_hour": "MAX_POSTS_PER_ACCOUNT_PER_HOUR",
        "max_posts_per_account_per_day": "MAX_POSTS_PER_ACCOUNT_PER_DAY",
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
        embeddings = create_embeddings(settings)
        vector = embeddings.embed_query("embedding 连接测试")
        return {
            "ok": True,
            "message": "Embedding 连接正常",
            "dimension": len(vector),
            "provider": settings.embedding_provider,
            "model": settings.embedding_model,
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
    square_openapi_key = (
        payload.square_openapi_key.strip()
        if payload.square_openapi_key is not None
        else None
    )
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
    if existing is None and not square_openapi_key:
        raise HTTPException(
            status_code=400,
            detail="新账号必须提供 Binance Square OpenAPI Key",
        )
    db.upsert_account(
        account_key=key,
        name=name,
        square_openapi_key=square_openapi_key,
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
    configured = bool(account.get("square_openapi_key"))
    error = None if configured else "未配置 Binance Square OpenAPI Key"
    status = "configured" if configured else "missing"
    db.update_account_check(
        account_key,
        signature_key=None,
        status=status,
        error=error,
    )
    return {
        "configured": configured,
        "error": error,
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
    owner_id = _acquire_pipeline_lock_or_raise(services.db)
    try:
        accounts = [_account_from_row(row) for row in services.db.list_accounts()]
        if not accounts:
            raise HTTPException(
                status_code=400,
                detail="请先添加至少一个 Binance Square OpenAPI 账号",
            )
        services.operator.accounts = tuple(accounts)
        services.operator.auto_publish = payload.auto_publish
        runs = services.operator.generate_for_all_accounts(
            content=payload.content,
            title=payload.title,
            url=payload.url,
        )
        return {"runs": [_serialize_account_run(run) for run in runs]}
    finally:
        services.db.release_job_lock(MONITOR_LOCK_NAME, owner_id=owner_id)


@app.get("/api/material-sources")
def list_material_sources() -> list[dict]:
    return get_db().list_material_sources(include_disabled=True)


@app.post("/api/material-sources")
def save_material_source(payload: MaterialSourcePayload) -> dict:
    if payload.source_type not in {"binance_square", "techflow_newsletter"}:
        raise HTTPException(status_code=400, detail="当前只支持 BN 广场和 TechFlow 快讯素材源")
    try:
        source_url = (
            validate_binance_url(payload.url, label="BN 广场素材源")
            if payload.source_type == "binance_square"
            else validate_techflow_url(payload.url, label="TechFlow 素材源")
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    source_id = get_db().upsert_material_source(
        name=payload.name.strip(),
        source_type=payload.source_type,
        url=source_url,
        enabled=payload.enabled,
    )
    return {"ok": True, "source_id": source_id}


@app.delete("/api/material-sources/{source_id}")
def delete_material_source(source_id: int) -> dict:
    get_db().disable_material_source(source_id)
    return {"ok": True}


@app.post("/api/material-sources/check")
async def check_material_sources() -> dict:
    return await run_material_monitor_once(fail_if_locked=True)


@app.post("/api/material-sources/{source_id}/check")
def check_material_source(source_id: int) -> dict:
    db = get_db()
    owner_id = _acquire_pipeline_lock_or_raise(db)
    try:
        source = next(
            (
                item
                for item in db.list_material_sources(include_disabled=True)
                if item["id"] == source_id
            ),
            None,
        )
        if not source:
            raise HTTPException(status_code=404, detail="素材源不存在")
        return MaterialSourceService(db).check_source(source)
    finally:
        db.release_job_lock(MONITOR_LOCK_NAME, owner_id=owner_id)


@app.get("/api/material-items")
def list_material_items(status: str | None = "new", limit: int = 50) -> list[dict]:
    return get_db().list_material_items(
        status=status,
        limit=max(1, min(limit, 300)),
    )


@app.get("/api/history/publishes")
def list_publish_history(
    limit: int = 100,
    account_key: str | None = None,
    status: str | None = None,
) -> list[dict]:
    rows = get_db().list_publish_history(
        limit=max(1, min(limit, 300)),
        account_key=account_key.strip() if account_key else None,
        status=status.strip() if status else None,
    )
    result = []
    for row in rows:
        payload = None
        raw_payload = row.get("publish_json")
        if isinstance(raw_payload, str) and raw_payload.strip():
            try:
                payload = json.loads(raw_payload)
            except ValueError:
                payload = raw_payload
        post_id, post_url = publish_evidence(payload)
        result.append(
            {
                "material_item_id": row["material_item_id"],
                "account_key": row["account_key"],
                "account_name": row.get("account_name") or row["account_key"],
                "account_check_status": row.get("account_check_status"),
                "status": row["status"],
                "generated_id": row.get("generated_id"),
                "attempt_count": int(row.get("attempt_count") or 0),
                "published_at": row.get("published_at"),
                "last_attempted_at": row.get("last_attempted_at"),
                "last_activity_at": row.get("published_at")
                or row.get("last_attempted_at")
                or row.get("updated_at"),
                "error": row.get("error"),
                "publish_result": payload,
                "post_id": post_id,
                "post_url": post_url,
                "material_title": row.get("material_title"),
                "material_content": row.get("material_content"),
                "material_url": row.get("material_url"),
                "source_name": row.get("source_name"),
                "source_type": row.get("source_type"),
                "source_created_at": row.get("source_created_at"),
                "generated_content": row.get("generated_content"),
                "generated_publish_status": row.get("generated_publish_status"),
                "generated_published_at": row.get("generated_published_at"),
            }
        )
    return result


@app.get("/api/history/accounts")
def publish_account_summaries() -> list[dict]:
    rows = get_db().publish_account_summaries()
    return [
        {
            "account_key": row["account_key"],
            "name": row["name"],
            "enabled": bool(row["enabled"]),
            "check_status": row.get("check_status"),
            "checked_at": row.get("checked_at"),
            "published_count": int(row.get("published_count") or 0),
            "failed_count": int(row.get("failed_count") or 0),
            "skipped_count": int(row.get("skipped_count") or 0),
            "last_published_at": row.get("last_published_at"),
            "last_activity_at": row.get("last_activity_at"),
        }
        for row in rows
    ]


@app.get("/api/performance/accounts")
def account_performance_dashboard(days: int = 7) -> dict:
    window_days = max(1, min(int(days), 365))
    db = get_db()
    history = db.list_publish_history(limit=5000, days=window_days)
    accounts = [_account_from_row(row) for row in db.list_accounts()]

    account_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    daily_counts: dict[str, Counter[str]] = defaultdict(Counter)
    source_counts: dict[tuple[str, str | None], Counter[str]] = defaultdict(Counter)

    total_published = 0
    total_failed = 0
    total_skipped = 0
    attempt_sum = 0
    attempt_samples = 0

    for row in history:
        account_key = str(row["account_key"])
        account_rows[account_key].append(row)
        status = str(row.get("status") or "")
        event_at = (
            row.get("published_at")
            or row.get("last_attempted_at")
            or row.get("updated_at")
        )
        if event_at:
            daily_counts[str(event_at)[:10]][status] += 1
        source_key = (
            str(row.get("source_name") or row.get("source_type") or "未知来源"),
            str(row.get("source_type") or "") or None,
        )
        source_counts[source_key][status] += 1
        if status == "published":
            total_published += 1
        elif status == "failed":
            total_failed += 1
        elif status == "skipped":
            total_skipped += 1
        attempts = int(row.get("attempt_count") or 0)
        if attempts > 0:
            attempt_sum += attempts
            attempt_samples += 1

    now = datetime.now(timezone.utc).date()
    daily = []
    for offset in range(window_days - 1, -1, -1):
        day = (now - timedelta(days=offset)).isoformat()
        counts = daily_counts.get(day) or Counter()
        published_count = int(counts.get("published", 0))
        failed_count = int(counts.get("failed", 0))
        skipped_count = int(counts.get("skipped", 0))
        daily.append(
            {
                "date": day,
                "published_count": published_count,
                "failed_count": failed_count,
                "skipped_count": skipped_count,
                "total_count": published_count + failed_count + skipped_count,
            }
        )

    account_metrics = []
    issues = []
    publishing_accounts = 0
    invalid_accounts = 0
    idle_accounts = 0
    limited_accounts = 0

    for account in accounts:
        rows = account_rows.get(account.key, [])
        published_count = sum(1 for row in rows if row.get("status") == "published")
        failed_count = sum(1 for row in rows if row.get("status") == "failed")
        skipped_count = sum(1 for row in rows if row.get("status") == "skipped")
        total_runs = len(rows)
        total_attempted = published_count + failed_count
        success_rate = _round_percent(published_count, total_attempted)
        avg_attempt_count = round(
            sum(int(row.get("attempt_count") or 0) for row in rows) / total_runs,
            2,
        ) if total_runs else 0.0
        active_days = len(
            {
                str(
                    row.get("published_at")
                    or row.get("last_attempted_at")
                    or row.get("updated_at")
                    or ""
                )[:10]
                for row in rows
                if (
                    row.get("published_at")
                    or row.get("last_attempted_at")
                    or row.get("updated_at")
                )
            }
        )
        last_published_at = _max_iso([row.get("published_at") for row in rows])
        last_activity_at = _max_iso(
            [
                row.get("published_at")
                or row.get("last_attempted_at")
                or row.get("updated_at")
                for row in rows
            ]
        )
        source_counter = Counter(
            (
                str(row.get("source_name") or row.get("source_type") or "未知来源"),
                str(row.get("source_type") or "") or None,
            )
            for row in rows
            if row.get("status") == "published"
        )
        top_source_name = None
        top_source_type = None
        top_source_count = 0
        if source_counter:
            (top_source_name, top_source_type), top_source_count = source_counter.most_common(1)[0]

        health_label, health_tone = _account_health(
            check_status=account.check_status,
            published_count=published_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
        )

        issue: dict[str, Any] | None = None
        if account.check_status in {"invalid", "missing"}:
            invalid_accounts += 1
            issue = {
                "account_key": account.key,
                "name": account.name,
                "severity": "high",
                "severity_label": "高",
                "reason": "账号发布配置缺失，请检查 Square OpenAPI Key",
            }
        elif total_runs and published_count == 0:
            issue = {
                "account_key": account.key,
                "name": account.name,
                "severity": "medium",
                "severity_label": "中",
                "reason": f"近 {window_days} 天有运行但没有成功发文",
            }
        elif total_attempted >= 3 and success_rate < 40:
            issue = {
                "account_key": account.key,
                "name": account.name,
                "severity": "high",
                "severity_label": "高",
                "reason": f"近 {window_days} 天成功率只有 {success_rate}%",
            }
        elif skipped_count >= max(3, published_count + 1):
            limited_accounts += 1
            issue = {
                "account_key": account.key,
                "name": account.name,
                "severity": "medium",
                "severity_label": "中",
                "reason": f"近 {window_days} 天被跳过 {skipped_count} 次，建议检查 OpenAPI Key / 策略限制",
            }
        elif total_runs == 0:
            issue = {
                "account_key": account.key,
                "name": account.name,
                "severity": "low",
                "severity_label": "低",
                "reason": f"近 {window_days} 天暂无活跃记录",
            }

        if published_count > 0:
            publishing_accounts += 1
        elif total_runs == 0:
            idle_accounts += 1

        if issue:
            issues.append(issue)

        account_metrics.append(
            {
                "account_key": account.key,
                "name": account.name,
                "check_status": account.check_status,
                "published_count": published_count,
                "failed_count": failed_count,
                "skipped_count": skipped_count,
                "total_runs": total_runs,
                "total_attempted": total_attempted,
                "success_rate": success_rate,
                "avg_attempt_count": avg_attempt_count,
                "active_days": active_days,
                "last_published_at": last_published_at,
                "last_activity_at": last_activity_at,
                "top_source_name": top_source_name,
                "top_source_type": top_source_type,
                "top_source_count": top_source_count,
                "health_label": health_label,
                "health_tone": health_tone,
                "issue_reason": issue["reason"] if issue else None,
            }
        )

    sources = []
    for (source_name, source_type), counts in source_counts.items():
        published_count = int(counts.get("published", 0))
        failed_count = int(counts.get("failed", 0))
        skipped_count = int(counts.get("skipped", 0))
        sources.append(
            {
                "source_name": source_name,
                "source_type": source_type,
                "published_count": published_count,
                "failed_count": failed_count,
                "skipped_count": skipped_count,
                "success_rate": _round_percent(
                    published_count,
                    published_count + failed_count,
                ),
            }
        )
    sources.sort(
        key=lambda item: (
            -int(item["published_count"]),
            -float(item["success_rate"]),
            str(item["source_name"]),
        )
    )

    issues.sort(
        key=lambda item: (
            {"high": 0, "medium": 1, "low": 2}.get(str(item["severity"]), 3),
            str(item["account_key"]),
        )
    )
    account_metrics.sort(
        key=lambda item: (
            -int(item["published_count"]),
            -float(item["success_rate"]),
            str(item["account_key"]),
        )
    )

    return {
        "period_days": window_days,
        "summary": {
            "active_accounts": len(accounts),
            "publishing_accounts": publishing_accounts,
            "idle_accounts": idle_accounts,
            "invalid_accounts": invalid_accounts,
            "limited_accounts": limited_accounts,
            "total_published": total_published,
            "total_failed": total_failed,
            "total_skipped": total_skipped,
            "success_rate": _round_percent(
                total_published,
                total_published + total_failed,
            ),
            "avg_attempt_count": round(
                attempt_sum / attempt_samples,
                2,
            ) if attempt_samples else 0.0,
        },
        "daily": daily,
        "accounts": account_metrics,
        "issues": issues[:8],
        "sources": sources[:10],
    }


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
        "max_posts_per_account_per_hour": settings.max_posts_per_account_per_hour,
        "max_posts_per_account_per_day": settings.max_posts_per_account_per_day,
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
    owner_id = _acquire_pipeline_lock_or_raise(services.db)
    try:
        accounts = [_account_from_row(row) for row in services.db.list_accounts()]
        if not accounts:
            raise HTTPException(
                status_code=400,
                detail="请先添加至少一个 Binance Square OpenAPI 账号",
            )
        services.operator.accounts = tuple(accounts)
        services.operator.auto_publish = payload.auto_publish
        runs = services.operator.run_material_item_for_all_accounts(payload.material_item_id)
        return {"runs": [_serialize_account_run(run) for run in runs]}
    finally:
        services.db.release_job_lock(MONITOR_LOCK_NAME, owner_id=owner_id)
