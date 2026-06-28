from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

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
        self.client = RemoteMCPClient(
            settings.mcp_url,
            auth_token=settings.mcp_auth_token,
        )
        self.chart_images = ChartImageService()
        self._tools: list[MCPTool] | None = None

    def _ensure_tools(self) -> list[MCPTool]:
        if self._tools is None:
            self.client.initialize()
            self._tools = self.client.list_tools()
        return self._tools

    def resolve_publish_tool(self) -> str:
        if self.settings.mcp_publish_tool:
            return self.settings.mcp_publish_tool
        tools = self._ensure_tools()
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
        tool_name = self.resolve_publish_tool()
        arguments = {
            "cookie": account.cookie,
            "content": generated["content"],
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
        try:
            image_base64 = self.chart_images.image_for_text(chart_text)
            if image_base64:
                arguments["image_base64"] = image_base64
        except Exception as exc:
            _ = exc
        return self.client.call_tool(tool_name, arguments)


class PublishingService:
    def __init__(self, db: Database, publisher: MCPPublisher):
        self.db = db
        self.publisher = publisher

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
        try:
            result = self.publisher.publish(account=account, generated=generated)
        except Exception as exc:
            result = {"error": str(exc)}
            self.db.mark_published(generated_id, result=result, success=False)
            return PublishResult(account.key, generated_id, False, result)
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
