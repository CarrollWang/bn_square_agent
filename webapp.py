from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import httpx
from pydantic import BaseModel, Field

from .ai.llm import StructuredLLM
from .core.config import AccountConfig, Settings
from .core.config import add_no_proxy_host
from .publishing.account_check import BinanceAccountChecker
from .publishing.mcp_client import RemoteMCPClient
from .services import build_services
from .sources.binance_square import MaterialSourceService
from .storage.database import Database


PACKAGE_DIR = Path(__file__).resolve().parent
DIST_DIR = PACKAGE_DIR / "dist"

monitor_state: dict[str, Any] = {
    "running": False,
    "last_started_at": None,
    "last_finished_at": None,
    "last_results": [],
    "last_consume_results": [],
    "last_error": None,
    "expired_count": 0,
}


async def run_material_monitor_once() -> dict[str, Any]:
    settings = get_settings()
    db = get_db()
    monitor_state["running"] = True
    monitor_state["last_started_at"] = datetime.now(timezone.utc).isoformat()
    try:
        expired_count = db.expire_stale_material_items(
            ttl_seconds=settings.material_ttl_seconds
        )
        results = await asyncio.to_thread(MaterialSourceService(db).check_all)
        consume_results: list[dict[str, Any]] = []
        if settings.auto_consume_materials:
            materials = db.list_material_items(
                status="new",
                limit=settings.material_consume_batch_size,
            )
            if materials:
                services = await asyncio.to_thread(build_services)
                for material in materials:
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
                                }
                                for run in runs
                            ],
                        }
                    )
        monitor_state.update(
            {
                "last_results": results,
                "last_consume_results": consume_results,
                "last_error": None,
                "expired_count": expired_count,
            }
        )
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
        monitor_state["last_finished_at"] = datetime.now(timezone.utc).isoformat()


async def material_monitor_loop() -> None:
    while True:
        settings = get_settings()
        try:
            await run_material_monitor_once()
        except Exception:
            pass
        await asyncio.sleep(max(30, settings.material_poll_interval_seconds))


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
    cookie: str = Field(min_length=1)


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
    auto_publish: bool | None = None
    auto_consume_materials: bool | None = None
    material_poll_interval_seconds: int | None = None
    material_ttl_seconds: int | None = None
    material_consume_batch_size: int | None = None


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
    db = Database(base.database_path)
    return base.with_overrides(db.get_app_settings())


def get_db() -> Database:
    return Database(Settings.from_env().database_path)


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
            "signature_key": row.get("signature_key"),
            "check_status": row.get("check_status"),
            "checked_at": row.get("checked_at"),
            "check_error": row.get("check_error"),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


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
        "auto_publish": settings.auto_publish,
        "auto_consume_materials": settings.auto_consume_materials,
        "material_poll_interval_seconds": settings.material_poll_interval_seconds,
        "material_ttl_seconds": settings.material_ttl_seconds,
        "material_consume_batch_size": settings.material_consume_batch_size,
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
    }
    secret_fields = {
        "llm_api_key": "LLM_API_KEY",
        "dashscope_api_key": "DASHSCOPE_API_KEY",
    }
    bool_fields = {
        "auto_publish": "AUTO_PUBLISH",
        "auto_consume_materials": "AUTO_CONSUME_MATERIALS",
    }
    int_fields = {
        "material_poll_interval_seconds": "MATERIAL_POLL_INTERVAL_SECONDS",
        "material_ttl_seconds": "MATERIAL_TTL_SECONDS",
        "material_consume_batch_size": "MATERIAL_CONSUME_BATCH_SIZE",
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
    key = payload.account_key.strip()
    name = (payload.name or key).strip()
    cookie = payload.cookie.strip()
    if not key or not cookie:
        raise HTTPException(status_code=400, detail="账号标识和 Cookie 必填")
    get_db().upsert_account(account_key=key, name=name, cookie=cookie)
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
    result = BinanceAccountChecker().check(account["cookie"])
    status = "valid" if result.valid else "invalid"
    db.update_account_check(
        account_key,
        signature_key=result.signature_key,
        status=status,
        error=result.error,
    )
    return {
        "valid": result.valid,
        "signature_key": result.signature_key,
        "error": result.error,
    }


@app.get("/api/mcp/tools")
def mcp_tools() -> dict:
    settings = get_settings()
    client = RemoteMCPClient(settings.mcp_url, auth_token=settings.mcp_auth_token)
    client.initialize()
    tools = client.list_tools()
    return {
        "mcp_url": settings.mcp_url,
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
    accounts = [
        AccountConfig(key=row["account_key"], name=row["name"], cookie=row["cookie"])
        for row in services.db.list_accounts()
    ]
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
    if payload.source_type != "binance_square":
        raise HTTPException(status_code=400, detail="当前只支持 binance_square 素材源")
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
        "ttl_seconds": settings.material_ttl_seconds,
        "auto_consume_materials": settings.auto_consume_materials,
        "consume_batch_size": settings.material_consume_batch_size,
    }


@app.post("/api/material-items/run")
def run_material_item(payload: RunMaterialPayload) -> dict:
    services = build_services()
    accounts = [
        AccountConfig(key=row["account_key"], name=row["name"], cookie=row["cookie"])
        for row in services.db.list_accounts()
    ]
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
