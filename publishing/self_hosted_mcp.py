from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import uuid

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..core.config import Settings as AgentSettings
from ..core.config import mask_url_credentials, normalize_proxy_url
from .browser_square_mcp_publisher import (
    BrowserBinanceSquarePublisher,
    BrowserPublishResult,
)
from .live_browser_session import DEFAULT_LOGIN_URL, LiveBrowserSessionManager


TOOL_NAME = "publish_binance_square"
PROTOCOL_VERSION = "2025-06-18"
MAX_COOKIE_LENGTH = 200_000
MAX_CONTENT_LENGTH = 50_000
MAX_IMAGE_BASE64_LENGTH = 18 * 1024 * 1024
MAX_REQUEST_BODY_BYTES = 20 * 1024 * 1024
COINS_PATTERN = re.compile(r"^[A-Z0-9]{2,20}:(?:future|spot)$", re.IGNORECASE)


def _bounded_env_int(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    return max(minimum, min(value, maximum))


def _resolve_debug_dir(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return Path(__file__).resolve().parents[1] / path


@dataclass(frozen=True)
class SelfHostedMCPSettings:
    auth_token: str
    allow_insecure_public_bind: bool
    default_proxy_url: str
    debug_dir: Path
    timeout_ms: int
    render_wait_ms: int
    publish_wait_ms: int
    browser_headless: bool

    @classmethod
    def from_env(cls) -> "SelfHostedMCPSettings":
        default_proxy = os.getenv("MCP_SERVER_DEFAULT_PROXY", "").strip()
        return cls(
            auth_token=os.getenv("MCP_SERVER_AUTH_TOKEN", "").strip(),
            allow_insecure_public_bind=os.getenv(
                "ALLOW_INSECURE_PUBLIC_BIND", "0"
            )
            .strip()
            .lower()
            not in {"0", "false", "no", "off"},
            default_proxy_url=normalize_proxy_url(default_proxy) if default_proxy else "",
            debug_dir=_resolve_debug_dir(
                os.getenv("MCP_SERVER_DEBUG_DIR", "./data/mcp_debug")
            ),
            timeout_ms=_bounded_env_int(
                "MCP_SERVER_TIMEOUT_MS",
                90_000,
                minimum=10_000,
                maximum=300_000,
            ),
            render_wait_ms=_bounded_env_int(
                "MCP_SERVER_RENDER_WAIT_MS",
                2_000,
                minimum=500,
                maximum=30_000,
            ),
            publish_wait_ms=_bounded_env_int(
                "MCP_SERVER_PUBLISH_WAIT_MS",
                12_000,
                minimum=3_000,
                maximum=120_000,
            ),
            browser_headless=os.getenv("MCP_BROWSER_HEADLESS", "0")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"},
        )


class BrowserLoginStartPayload(BaseModel):
    account_key: str = Field(min_length=1, max_length=64)
    name: str | None = Field(default=None, max_length=120)
    proxy_url: str | None = Field(default=None, max_length=2_048)
    login_url: str | None = Field(default=None, max_length=2_048)


browser_session_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="bn-square-live-browser",
)
_browser_session_manager: LiveBrowserSessionManager | None = None


def _get_browser_session_manager(
    settings: SelfHostedMCPSettings,
) -> LiveBrowserSessionManager:
    global _browser_session_manager
    if _browser_session_manager is None:
        _browser_session_manager = LiveBrowserSessionManager(
            headless=settings.browser_headless,
            timeout_ms=settings.timeout_ms,
        )
    return _browser_session_manager


async def _run_browser_operation(operation, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(browser_session_executor, operation, *args)


def _start_live_browser(
    settings: SelfHostedMCPSettings,
    payload: BrowserLoginStartPayload,
) -> dict[str, object]:
    manager = _get_browser_session_manager(settings)
    return manager.start_login(
        account_key=payload.account_key,
        name=payload.name or payload.account_key,
        proxy_url=payload.proxy_url or "",
        login_url=payload.login_url or DEFAULT_LOGIN_URL,
    )


def _finish_live_browser(
    settings: SelfHostedMCPSettings,
    session_id: str,
) -> dict[str, object]:
    manager = _get_browser_session_manager(settings)
    result = manager.finish_login(session_id)
    cookie_header = str(result.pop("cookie_header"))
    account_key = str(result["account_key"])
    name = str(result.pop("name"))
    proxy_url = str(result.pop("proxy_url"))
    database = AgentSettings.from_env().build_database()
    database.upsert_account(
        account_key=account_key,
        name=name,
        cookie=cookie_header,
        proxy_url=proxy_url,
    )
    database.update_account_check(
        account_key,
        signature_key=result.get("signature_key"),
        status="valid",
        error=None,
    )
    return result


def _live_browser_status(
    settings: SelfHostedMCPSettings,
    account_key: str,
) -> dict[str, object]:
    return _get_browser_session_manager(settings).status(account_key)


def _close_live_browser(
    settings: SelfHostedMCPSettings,
    session_id: str,
) -> bool:
    return _get_browser_session_manager(settings).close(session_id=session_id)


def _publish_with_live_browser(
    settings: SelfHostedMCPSettings,
    publisher: BrowserBinanceSquarePublisher,
    cookie: str,
    content: str,
    account_key: str,
    coins: str,
    image_base64: str,
    proxy_url: str,
) -> BrowserPublishResult:
    manager = _get_browser_session_manager(settings)
    if account_key and manager.has_session(account_key):
        page = manager.get_ready_page(account_key)
        return publisher.publish_in_page(
            page=page,
            content=content,
            coins=coins,
            image_base64=image_base64,
            proxy_url=proxy_url,
        )
    return publisher.publish(
        cookie=cookie,
        content=content,
        account_key=account_key,
        coins=coins,
        image_base64=image_base64,
        proxy_url=proxy_url,
    )


def _jsonrpc_result(
    request_id: object,
    result: object,
    *,
    session_id: str,
) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "result": result},
        headers={"mcp-session-id": session_id},
    )


def _jsonrpc_error(
    request_id: object,
    code: int,
    message: str,
    *,
    session_id: str,
    data: object | None = None,
) -> JSONResponse:
    payload: dict[str, object] = {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": code,
            "message": message,
        },
    }
    if data is not None:
        payload["error"]["data"] = data
    return JSONResponse(payload, headers={"mcp-session-id": session_id})


def _tool_definition() -> dict[str, object]:
    return {
        "name": TOOL_NAME,
        "description": (
            "使用账号的常驻浏览器会话在 Binance Square 发布内容。"
            "Cookie 仅作为旧链路回退；支持独立代理、自定义图片和 coins 参数。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cookie": {
                    "type": "string",
                    "description": "可选，仅在没有常驻浏览器会话时作为兼容回退",
                },
                "content": {
                    "type": "string",
                    "description": "要发布的正文内容",
                },
                "account_key": {
                    "type": "string",
                    "description": "账号标识；优先复用 MCP 内该账号仍在运行的浏览器会话",
                    "maxLength": 128,
                },
                "coins": {
                    "type": "string",
                    "description": "可选，形如 BTC:future；自建浏览器发布器会确保正文包含对应 cashtag",
                    "maxLength": 32,
                },
                "image_base64": {
                    "type": "string",
                    "description": "可选，data URL 或纯 base64 图片",
                },
                "proxy_url": {
                    "type": "string",
                    "description": "可选，账号独立代理，例如 http://user:pass@host:port",
                },
            },
            "required": ["content"],
            "additionalProperties": False,
        },
    }


def _is_loopback_client(host: str | None) -> bool:
    if not host:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() in {"localhost", "testclient"}


def _check_auth(
    auth_header: str | None,
    settings: SelfHostedMCPSettings,
    *,
    client_host: str | None,
) -> None:
    if not settings.auth_token:
        if settings.allow_insecure_public_bind or _is_loopback_client(client_host):
            return
        raise HTTPException(
            status_code=403,
            detail="Public MCP access requires MCP_SERVER_AUTH_TOKEN",
        )
    expected = f"Bearer {settings.auth_token}"
    if not auth_header or not secrets.compare_digest(
        auth_header.encode("utf-8"),
        expected.encode("utf-8"),
    ):
        raise HTTPException(status_code=401, detail="MCP server auth failed")


def _build_publish_payload(
    *,
    success: bool,
    message: str,
    coins: str,
    proxy_url: str,
    post_url: str | None = None,
    debug_artifact: str | None = None,
) -> dict[str, object]:
    structured = {
        "success": success,
        "message": message,
        "coins": coins or None,
        "proxy_url": mask_url_credentials(proxy_url) if proxy_url else None,
        "post_url": post_url,
        "debug_artifact": debug_artifact,
    }
    text = json.dumps(structured, ensure_ascii=False)
    return {
        "success": success,
        "isError": not success,
        "structuredContent": structured,
        "content": [{"type": "text", "text": text}],
    }


app = FastAPI(title="BN Square Self-Hosted MCP")


@app.on_event("shutdown")
async def shutdown_live_browsers() -> None:
    if _browser_session_manager is not None:
        await _run_browser_operation(_browser_session_manager.close_all)


@app.get("/")
def root() -> dict[str, object]:
    settings = SelfHostedMCPSettings.from_env()
    return {
        "ok": True,
        "service": "bn-square-self-hosted-mcp",
        "tool": TOOL_NAME,
        "auth_enabled": bool(settings.auth_token),
    }


@app.get("/healthz")
def healthz() -> dict[str, object]:
    settings = SelfHostedMCPSettings.from_env()
    return {
        "ok": True,
        "tool": TOOL_NAME,
        "auth_enabled": bool(settings.auth_token),
    }


@app.post("/browser-sessions/start")
async def start_browser_session(
    payload: BrowserLoginStartPayload,
    request: Request,
    authorization: str | None = Header(None),
) -> dict[str, object]:
    settings = SelfHostedMCPSettings.from_env()
    _check_auth(
        authorization,
        settings,
        client_host=request.client.host if request.client else None,
    )
    try:
        return await _run_browser_operation(_start_live_browser, settings, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"打开常驻登录浏览器失败: {exc}") from exc


@app.post("/browser-sessions/{session_id}/finish")
async def finish_browser_session(
    session_id: str,
    request: Request,
    authorization: str | None = Header(None),
) -> dict[str, object]:
    settings = SelfHostedMCPSettings.from_env()
    _check_auth(
        authorization,
        settings,
        client_host=request.client.host if request.client else None,
    )
    try:
        return await _run_browser_operation(_finish_live_browser, settings, session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"确认登录失败: {exc}") from exc


@app.get("/browser-sessions/account/{account_key}")
async def browser_session_status(
    account_key: str,
    request: Request,
    authorization: str | None = Header(None),
) -> dict[str, object]:
    settings = SelfHostedMCPSettings.from_env()
    _check_auth(
        authorization,
        settings,
        client_host=request.client.host if request.client else None,
    )
    return await _run_browser_operation(_live_browser_status, settings, account_key)


@app.delete("/browser-sessions/{session_id}")
async def close_browser_session(
    session_id: str,
    request: Request,
    authorization: str | None = Header(None),
) -> dict[str, object]:
    settings = SelfHostedMCPSettings.from_env()
    _check_auth(
        authorization,
        settings,
        client_host=request.client.host if request.client else None,
    )
    closed = await _run_browser_operation(_close_live_browser, settings, session_id)
    return {"ok": True, "closed": closed}


@app.post("/mcp")
async def handle_mcp(
    request: Request,
    authorization: str | None = Header(None),
    mcp_session_id: str | None = Header(None, alias="Mcp-Session-Id"),
) -> JSONResponse:
    settings = SelfHostedMCPSettings.from_env()
    _check_auth(
        authorization,
        settings,
        client_host=request.client.host if request.client else None,
    )
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_REQUEST_BODY_BYTES:
                raise HTTPException(status_code=413, detail="MCP request body too large")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="MCP 请求必须是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="MCP 请求必须是 JSON 对象")
    method = str(payload.get("method") or "")
    request_id = payload.get("id")
    session_id = mcp_session_id or str(uuid.uuid4())

    if method == "notifications/initialized":
        return JSONResponse({"ok": True}, headers={"mcp-session-id": session_id})

    if method == "initialize":
        return _jsonrpc_result(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {
                        "listChanged": False,
                    }
                },
                "serverInfo": {
                    "name": "bn-square-self-hosted-mcp",
                    "version": "0.1.0",
                },
            },
            session_id=session_id,
        )

    if method == "tools/list":
        return _jsonrpc_result(
            request_id,
            {
                "tools": [_tool_definition()],
            },
            session_id=session_id,
        )

    if method == "tools/call":
        params = payload.get("params") or {}
        if not isinstance(params, dict):
            return _jsonrpc_error(
                request_id,
                -32602,
                "params 必须是 JSON 对象",
                session_id=session_id,
            )
        tool_name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _jsonrpc_error(
                request_id,
                -32602,
                "arguments 必须是 JSON 对象",
                session_id=session_id,
            )
        if tool_name != TOOL_NAME:
            return _jsonrpc_error(
                request_id,
                -32601,
                f"unknown tool: {tool_name}",
                session_id=session_id,
            )
        cookie = str(arguments.get("cookie") or "")
        content = str(arguments.get("content") or "")
        account_key = str(arguments.get("account_key") or "").strip()
        coins = str(arguments.get("coins") or "")
        image_base64 = str(arguments.get("image_base64") or "")
        proxy_url = str(arguments.get("proxy_url") or "").strip()
        if len(cookie) > MAX_COOKIE_LENGTH:
            return _jsonrpc_error(
                request_id,
                -32602,
                "cookie 过长",
                session_id=session_id,
            )
        if len(content) > MAX_CONTENT_LENGTH:
            return _jsonrpc_error(
                request_id,
                -32602,
                "content 过长",
                session_id=session_id,
            )
        if len(account_key) > 128:
            return _jsonrpc_error(
                request_id,
                -32602,
                "account_key 过长",
                session_id=session_id,
            )
        if len(image_base64) > MAX_IMAGE_BASE64_LENGTH:
            return _jsonrpc_error(
                request_id,
                -32602,
                "image_base64 过大",
                session_id=session_id,
            )
        if coins and not COINS_PATTERN.fullmatch(coins):
            return _jsonrpc_error(
                request_id,
                -32602,
                "coins 格式必须类似 BTC:future",
                session_id=session_id,
            )
        try:
            effective_proxy = normalize_proxy_url(
                proxy_url or settings.default_proxy_url
            ) if (proxy_url or settings.default_proxy_url) else ""
        except ValueError as exc:
            return _jsonrpc_error(
                request_id,
                -32602,
                str(exc),
                session_id=session_id,
            )
        publisher = BrowserBinanceSquarePublisher(
            timeout_ms=settings.timeout_ms,
            render_wait_ms=settings.render_wait_ms,
            publish_wait_ms=settings.publish_wait_ms,
            debug_dir=settings.debug_dir,
        )
        try:
            result = await _run_browser_operation(
                _publish_with_live_browser,
                settings,
                publisher,
                cookie,
                content,
                account_key,
                coins,
                image_base64,
                effective_proxy,
            )
        except Exception as exc:
            result = BrowserPublishResult(False, f"发布失败: {exc}")
        return _jsonrpc_result(
            request_id,
            _build_publish_payload(
                success=result.success,
                message=result.message,
                coins=coins,
                proxy_url=effective_proxy,
                post_url=result.post_url,
                debug_artifact=result.debug_artifact,
            ),
            session_id=session_id,
        )

    return _jsonrpc_error(
        request_id,
        -32601,
        f"unknown method: {method}",
        session_id=session_id,
    )
