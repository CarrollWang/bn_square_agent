from __future__ import annotations

import asyncio
from dataclasses import dataclass
import ipaddress
import json
import os
import re
import secrets
import uuid

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from ..ai.binance_symbols import futures_symbol_catalog, spot_asset_catalog
from ..core.config import Settings as AgentSettings
from ..storage.database import Database, PublishRateLimitError
from .binance_square_openapi import (
    BinanceSquareOpenAPIClient,
    BinanceSquareOpenAPIError,
)


TOOL_NAME = "publish_binance_square"
PROTOCOL_VERSION = "2025-06-18"
MAX_CONTENT_LENGTH = 50_000
MAX_IMAGE_BASE64_LENGTH = 18 * 1024 * 1024
MAX_REQUEST_BODY_BYTES = 20 * 1024 * 1024
COINS_PATTERN = re.compile(r"^[A-Z0-9]{2,20}:(?:future|spot)$", re.IGNORECASE)
FUTURE_MARKER_PATTERN = re.compile(
    r"\{future\}\(([A-Z0-9]{2,30}USDT)\)",
    re.IGNORECASE,
)
BARE_CASHTAG_TOKEN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9$])([A-Z][A-Z0-9]{1,19})(?![A-Za-z0-9])"
)
CASHTAG_STOPWORDS = frozenset(
    {"AI", "API", "CEO", "CFO", "DEX", "ETF", "SEC", "USD", "USDT"}
)


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


@dataclass(frozen=True)
class SelfHostedMCPSettings:
    auth_token: str
    allow_insecure_public_bind: bool
    timeout_seconds: float

    @classmethod
    def from_env(cls) -> "SelfHostedMCPSettings":
        timeout_ms = _bounded_env_int(
            "MCP_SERVER_TIMEOUT_MS",
            90_000,
            minimum=10_000,
            maximum=300_000,
        )
        return cls(
            auth_token=os.getenv("MCP_SERVER_AUTH_TOKEN", "").strip(),
            allow_insecure_public_bind=os.getenv(
                "ALLOW_INSECURE_PUBLIC_BIND", "0"
            )
            .strip()
            .lower()
            not in {"0", "false", "no", "off"},
            timeout_seconds=timeout_ms / 1000,
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
        "error": {"code": code, "message": message},
    }
    if data is not None:
        payload["error"]["data"] = data
    return JSONResponse(payload, headers={"mcp-session-id": session_id})


def _tool_definition() -> dict[str, object]:
    return {
        "name": TOOL_NAME,
        "description": (
            "使用账号已加密保存的 Binance Square OpenAPI Key 发布内容。"
            "Key 不通过 MCP 参数传输；支持账号轮转、独立代理和一张可选图片。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "要发布的正文内容",
                },
                "account_key": {
                    "type": "string",
                    "description": "账号标识；MCP 从同机加密数据库读取该账号的 OpenAPI Key",
                    "maxLength": 128,
                },
                "coins": {
                    "type": "string",
                    "description": (
                        "可选，形如 SOL:future；用于选择主合约，正文其他有效币种"
                        "会按 Binance 现货目录规范为 $TOKEN"
                    ),
                    "maxLength": 32,
                },
                "image_base64": {
                    "type": "string",
                    "description": "可选，data URL 或纯 base64 图片",
                },
            },
            "required": ["content", "account_key"],
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
        auth_header.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="MCP server auth failed")


def _load_account_context(
    account_key: str,
) -> tuple[Database, AgentSettings, dict[str, object]]:
    base_settings = AgentSettings.from_env()
    database = base_settings.build_database()
    effective_settings = base_settings.with_overrides(database.get_app_settings())
    account = next(
        (
            row
            for row in database.list_accounts()
            if row["account_key"] == account_key
        ),
        None,
    )
    if not account:
        raise KeyError(f"账号不存在或已禁用: {account_key}")
    if not str(account.get("square_openapi_key") or "").strip():
        raise ValueError(f"账号 {account_key} 缺少 Binance Square OpenAPI Key")
    return database, effective_settings, account


def _ensure_trading_component(
    content: str,
    coins: str,
    *,
    valid_cashtag_tokens: set[str] | frozenset[str] | None = None,
) -> str:
    """Keep cashtags and trading components explicit at the OpenAPI boundary.

    ``coins`` used to be consumed by the remote browser publisher.  The
    self-hosted publisher calls Square OpenAPI directly, so it must translate
    that metadata into the visible ``$TOKEN`` cashtag and, for futures, the
    bodyTextOnly marker. Existing explicit markers are preserved and
    conflicting symbols are rejected to avoid publishing a BTC article with
    an ETH trading component.
    """

    text = content.strip()
    if not coins:
        return text

    coin, market = coins.split(":", 1)
    coin = coin.upper()
    market = market.lower()
    cashtag = coin.removesuffix("USDT")
    bare_token = re.compile(
        rf"(?<![A-Za-z0-9$]){re.escape(cashtag)}(?![A-Za-z0-9])",
        re.IGNORECASE,
    )
    text, _ = bare_token.subn(f"${cashtag}", text)
    valid_tokens = {
        str(token).strip().upper()
        for token in (valid_cashtag_tokens or ())
        if str(token).strip()
    }

    def normalize_secondary(match: re.Match[str]) -> str:
        token = match.group(1).upper()
        if token == cashtag:
            return f"${token}"
        if token in valid_tokens and token not in CASHTAG_STOPWORDS:
            return f"${token}"
        return match.group(0)

    text = BARE_CASHTAG_TOKEN_PATTERN.sub(normalize_secondary, text)
    if not re.search(rf"\${re.escape(cashtag)}\b", text, re.IGNORECASE):
        text = f"${cashtag} {text}".strip()

    if market == "future":
        symbol = coin if coin.endswith("USDT") else f"{coin}USDT"
        existing = [match.upper() for match in FUTURE_MARKER_PATTERN.findall(text)]
        if existing:
            if symbol not in existing:
                raise ValueError(
                    f"正文合约组件 {existing[0]} 与发布币种 {symbol} 不一致"
                )
            return text
        return f"{text}\n\n{{future}}({symbol})"

    return text


def _publish(
    settings: SelfHostedMCPSettings,
    *,
    content: str,
    account_key: str,
    coins: str,
    image_base64: str,
) -> dict[str, object]:
    database, agent_settings, account = _load_account_context(account_key)
    valid_cashtag_tokens = set(spot_asset_catalog.get())
    valid_cashtag_tokens.update(
        symbol.removesuffix("USDT") for symbol in futures_symbol_catalog.get()
    )
    publish_content = _ensure_trading_component(
        content,
        coins,
        valid_cashtag_tokens=valid_cashtag_tokens,
    )
    if len(publish_content) > MAX_CONTENT_LENGTH:
        raise ValueError("补充交易组件后 content 过长")
    client = BinanceSquareOpenAPIClient(
        str(account["square_openapi_key"]),
        proxy_url=str(account.get("proxy_url") or ""),
        timeout_seconds=settings.timeout_seconds,
    )
    reservation_id = database.reserve_publish_slot(
        account_key,
        hourly_limit=agent_settings.max_posts_per_account_per_hour,
        daily_limit=agent_settings.max_posts_per_account_per_day,
    )
    try:
        result = client.publish_text(
            publish_content,
            image_base64=image_base64,
        )
    except (BinanceSquareOpenAPIError, ValueError):
        database.finalize_publish_slot(reservation_id, status="failed")
        raise
    except Exception:
        database.finalize_publish_slot(reservation_id, status="unknown")
        raise

    rate_status = (
        "published"
        if result.success
        else "unknown"
        if result.outcome == "unknown"
        else "failed"
    )
    database.finalize_publish_slot(
        reservation_id,
        status=rate_status,
        post_id=result.post_id,
    )
    return result.as_dict()


def _build_publish_payload(
    result: dict[str, object],
    *,
    account_key: str,
    coins: str,
) -> dict[str, object]:
    structured = {
        **result,
        "account_key": account_key,
        "coins": coins or None,
    }
    text = json.dumps(structured, ensure_ascii=False)
    success = structured.get("success") is True
    return {
        "success": success,
        "isError": structured.get("outcome") == "failed",
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
        "publisher": "binance-square-openapi",
        "tool": TOOL_NAME,
        "auth_enabled": bool(settings.auth_token),
    }


@app.get("/healthz")
def healthz() -> dict[str, object]:
    settings = SelfHostedMCPSettings.from_env()
    return {
        "ok": True,
        "publisher": "binance-square-openapi",
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
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": "bn-square-self-hosted-mcp",
                    "version": "0.2.0",
                },
            },
            session_id=session_id,
        )
    if method == "tools/list":
        return _jsonrpc_result(
            request_id,
            {"tools": [_tool_definition()]},
            session_id=session_id,
        )
    if method == "tools/call":
        params = payload.get("params") or {}
        if not isinstance(params, dict):
            return _jsonrpc_error(
                request_id, -32602, "params 必须是 JSON 对象", session_id=session_id
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
        content = str(arguments.get("content") or "")
        account_key = str(arguments.get("account_key") or "").strip()
        coins = str(arguments.get("coins") or "")
        image_base64 = str(arguments.get("image_base64") or "")
        if not content.strip():
            return _jsonrpc_error(
                request_id, -32602, "content 不能为空", session_id=session_id
            )
        if not account_key:
            return _jsonrpc_error(
                request_id, -32602, "account_key 不能为空", session_id=session_id
            )
        if len(content) > MAX_CONTENT_LENGTH:
            return _jsonrpc_error(
                request_id, -32602, "content 过长", session_id=session_id
            )
        if len(account_key) > 128:
            return _jsonrpc_error(
                request_id, -32602, "account_key 过长", session_id=session_id
            )
        if len(image_base64) > MAX_IMAGE_BASE64_LENGTH:
            return _jsonrpc_error(
                request_id, -32602, "image_base64 过大", session_id=session_id
            )
        if coins and not COINS_PATTERN.fullmatch(coins):
            return _jsonrpc_error(
                request_id,
                -32602,
                "coins 格式必须类似 BTC:future",
                session_id=session_id,
            )
        try:
            result = await asyncio.to_thread(
                _publish,
                settings,
                content=content,
                account_key=account_key,
                coins=coins,
                image_base64=image_base64,
            )
        except PublishRateLimitError as exc:
            result = {
                "success": False,
                "outcome": "rate_limited",
                "message": str(exc),
                "post_id": None,
                "post_url": None,
            }
        except (KeyError, ValueError) as exc:
            result = {
                "success": False,
                "outcome": "failed",
                "message": str(exc),
                "post_id": None,
                "post_url": None,
            }
        except BinanceSquareOpenAPIError as exc:
            result = {
                "success": False,
                "outcome": "failed",
                "message": str(exc),
                "api_code": exc.code,
                "post_id": None,
                "post_url": None,
            }
        except Exception as exc:
            result = {
                "success": False,
                "outcome": "unknown",
                "message": f"发布请求状态未知: {exc}",
                "post_id": None,
                "post_url": None,
            }
        return _jsonrpc_result(
            request_id,
            _build_publish_payload(
                result,
                account_key=account_key,
                coins=coins,
            ),
            session_id=session_id,
        )
    return _jsonrpc_error(
        request_id,
        -32601,
        f"unknown method: {method}",
        session_id=session_id,
    )
