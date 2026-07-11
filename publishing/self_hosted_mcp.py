from __future__ import annotations

import asyncio
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

from ..core.config import mask_url_credentials, normalize_proxy_url
from .browser_square_mcp_publisher import BrowserBinanceSquarePublisher


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
            "使用 Binance Cookie 在 Binance Square 发布内容。"
            "支持可选代理、自定义图片上传和 coins 参数透传。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cookie": {
                    "type": "string",
                    "description": "浏览器 Cookie 原文",
                },
                "content": {
                    "type": "string",
                    "description": "要发布的正文内容",
                },
                "account_key": {
                    "type": "string",
                    "description": "可选，复用该账号在本机保存的独立浏览器 Profile",
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
            "required": ["cookie", "content"],
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
        result = await asyncio.to_thread(
            publisher.publish,
            cookie=cookie,
            content=content,
            account_key=account_key,
            coins=coins,
            image_base64=image_base64,
            proxy_url=effective_proxy,
        )
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
