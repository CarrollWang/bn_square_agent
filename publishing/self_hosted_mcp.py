from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import uuid

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from ..core.config import mask_url_credentials, normalize_proxy_url
from .browser_square_mcp_publisher import BrowserBinanceSquarePublisher


TOOL_NAME = "publish_binance_square"
PROTOCOL_VERSION = "2025-06-18"


@dataclass(frozen=True)
class SelfHostedMCPSettings:
    auth_token: str
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
            default_proxy_url=normalize_proxy_url(default_proxy) if default_proxy else "",
            debug_dir=Path(os.getenv("MCP_SERVER_DEBUG_DIR", "./data/mcp_debug")),
            timeout_ms=max(10_000, int(os.getenv("MCP_SERVER_TIMEOUT_MS", "90000"))),
            render_wait_ms=max(
                500, int(os.getenv("MCP_SERVER_RENDER_WAIT_MS", "2000"))
            ),
            publish_wait_ms=max(
                3_000, int(os.getenv("MCP_SERVER_PUBLISH_WAIT_MS", "12000"))
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
                "coins": {
                    "type": "string",
                    "description": "可选，形如 BTC:future 的附加参数",
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
            "additionalProperties": True,
        },
    }


def _check_auth(auth_header: str | None, settings: SelfHostedMCPSettings) -> None:
    if not settings.auth_token:
        return
    expected = f"Bearer {settings.auth_token}"
    if auth_header != expected:
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
    _check_auth(authorization, settings)
    payload = await request.json()
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
        tool_name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        if tool_name != TOOL_NAME:
            return _jsonrpc_error(
                request_id,
                -32601,
                f"unknown tool: {tool_name}",
                session_id=session_id,
            )
        cookie = str(arguments.get("cookie") or "")
        content = str(arguments.get("content") or "")
        coins = str(arguments.get("coins") or "")
        image_base64 = str(arguments.get("image_base64") or "")
        proxy_url = str(arguments.get("proxy_url") or "").strip()
        effective_proxy = proxy_url or settings.default_proxy_url
        publisher = BrowserBinanceSquarePublisher(
            timeout_ms=settings.timeout_ms,
            render_wait_ms=settings.render_wait_ms,
            publish_wait_ms=settings.publish_wait_ms,
            debug_dir=settings.debug_dir,
        )
        result = publisher.publish(
            cookie=cookie,
            content=content,
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
