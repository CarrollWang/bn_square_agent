from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

import httpx

from ..core.config import AccountConfig, Settings
from ..storage.database import Database
from .chart_image import ChartImageService
from .mcp_client import MCPTool, RemoteMCPClient


DEFAULT_PUBLISH_TOOL = "publish_binance_square"
PUBLISH_KEYWORDS = ("publish", "post", "square", "article", "binance")


@dataclass(frozen=True)
class PublishResult:
    account_key: str
    generated_id: int
    success: bool
    result: dict[str, Any]


class MCPPublisher:
    def __init__(self, settings: Settings):
        settings.validate_for_publish()
        self.settings = settings
        self.chart_images = ChartImageService()
        self._tools_by_target: dict[tuple[str, str], list[MCPTool]] = {}

    def _resolve_mcp_url(self, account: AccountConfig) -> str:
        url = account.mcp_url or self.settings.mcp_url
        if not url:
            raise RuntimeError(
                f"账号 {account.key} 未配置独立 MCP 地址，且全局 MCP_URL 为空"
            )
        return url

    def _resolve_mcp_auth_token(self, account: AccountConfig) -> str:
        return account.mcp_auth_token or self.settings.mcp_auth_token

    def _client_for_account(self, account: AccountConfig) -> RemoteMCPClient:
        return RemoteMCPClient(
            self._resolve_mcp_url(account),
            auth_token=self._resolve_mcp_auth_token(account),
        )

    def _ensure_tools(self, account: AccountConfig) -> list[MCPTool]:
        key = (
            self._resolve_mcp_url(account),
            self._resolve_mcp_auth_token(account),
        )
        tools = self._tools_by_target.get(key)
        if tools is None:
            client = self._client_for_account(account)
            client.initialize()
            tools = client.list_tools()
            self._tools_by_target[key] = tools
        return tools

    def resolve_publish_tool(self, account: AccountConfig) -> str:
        if self.settings.mcp_publish_tool:
            return self.settings.mcp_publish_tool
        tools = self._ensure_tools(account)
        if any(tool.name == DEFAULT_PUBLISH_TOOL for tool in tools):
            return DEFAULT_PUBLISH_TOOL
        for tool in tools:
            lowered = f"{tool.name} {tool.description}".lower()
            if any(keyword in lowered for keyword in PUBLISH_KEYWORDS):
                return tool.name
        names = ", ".join(tool.name for tool in tools) or "无可用工具"
        raise RuntimeError(f"无法自动识别发布工具，请配置 MCP_PUBLISH_TOOL。可用工具: {names}")

    def publish(
        self,
        *,
        account: AccountConfig,
        generated: dict[str, Any],
    ) -> dict[str, Any]:
        if not account.cookie:
            raise RuntimeError(f"账号 {account.key} 缺少 Cookie，无法发布")
        tool_name = self.resolve_publish_tool(account)
        client = self._client_for_account(account)
        client.initialize()
        content = re.sub(
            r"\n*\{future\}\([A-Z0-9]{2,30}USDT\)\s*",
            "",
            generated["content"],
            flags=re.IGNORECASE,
        ).rstrip()
        arguments = {
            "cookie": account.cookie,
            "content": content,
            "account_key": account.key,
        }
        chart_text = "\n".join(
            item
            for item in (
                generated.get("source_title"),
                generated.get("source_content"),
                generated.get("content"),
            )
            if item
        )
        target = self.chart_images.extract_target(chart_text)
        if target:
            coin = target.symbol.removesuffix("USDT")
            arguments["coins"] = f"{coin}:{target.market}"
        if account.proxy_url:
            arguments["proxy_url"] = account.proxy_url
        try:
            image_base64 = self.chart_images.image_for_text(
                chart_text,
                proxy_url=account.proxy_url,
            )
            if image_base64:
                arguments["image_base64"] = image_base64
        except Exception as exc:
            _ = exc
        return client.call_tool(tool_name, arguments)


class PublishingService:
    def __init__(self, db: Database, publisher: MCPPublisher):
        self.db = db
        self.publisher = publisher

    @staticmethod
    def _decode_publish_result(raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str) and raw.strip():
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = {}
            if isinstance(payload, dict):
                return payload
        return {}

    def publish_generated(
        self,
        *,
        account: AccountConfig,
        generated_id: int,
    ) -> PublishResult:
        generated = self.db.get_generated(generated_id)
        if generated["account_key"] != account.key:
            raise ValueError(
                f"生成稿 {generated_id} 属于 {generated['account_key']}，不是 {account.key}"
            )
        if generated["status"] != "approved":
            raise ValueError(f"只有 approved 终稿可以发布，当前状态: {generated['status']}")
        if generated.get("publish_status") == "published":
            result = self._decode_publish_result(generated.get("publish_json"))
            if not result:
                result = {"success": True, "message": "终稿已发布，已跳过重复发布"}
            return PublishResult(account.key, generated_id, True, result)
        try:
            result = self.publisher.publish(account=account, generated=generated)
        except Exception as exc:
            outcome = (
                "unknown"
                if isinstance(exc, httpx.TransportError)
                else "failed"
            )
            result = {"success": False, "outcome": outcome, "error": str(exc)}
            self.db.mark_published(generated_id, result=result, success=False)
            return PublishResult(account.key, generated_id, False, result)
        if "outcome" not in result:
            result["outcome"] = "published" if self._is_publish_success(result) else "failed"
        success = self._is_publish_success(result)
        self.db.mark_published(generated_id, result=result, success=success)
        return PublishResult(account.key, generated_id, success, result)

    @staticmethod
    def _is_publish_success(result: dict[str, Any]) -> bool:
        if result.get("isError") is True:
            return False

        structured = result.get("structuredContent")
        if isinstance(structured, dict) and "success" in structured:
            return structured.get("success") is True

        if "success" in result:
            return result.get("success") is True

        content = result.get("content")
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                text = item.get("text")
                if not isinstance(text, str):
                    continue
                try:
                    payload = json.loads(text)
                except ValueError:
                    continue
                if isinstance(payload, dict) and "success" in payload:
                    return payload.get("success") is True

        return False
