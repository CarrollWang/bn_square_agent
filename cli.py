from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Sequence

from .core.config import Settings
from .publishing.mcp_client import RemoteMCPClient


LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _is_public_bind(host: str) -> bool:
    return host.strip().lower() not in LOOPBACK_HOSTS


def _resolve_mcp_target(settings: Settings) -> tuple[str, str, str | None]:
    if settings.mcp_url:
        return settings.mcp_url, settings.mcp_auth_token, None
    for account in settings.accounts:
        if account.mcp_url:
            return (
                account.mcp_url,
                account.mcp_auth_token or settings.mcp_auth_token,
                account.key,
            )
    raise SystemExit("未配置全局 MCP_URL，也没有账号配置独立 MCP 地址")


def _read_content(args: argparse.Namespace) -> str:
    if args.file:
        return Path(args.file).read_text(encoding="utf-8")
    if args.content:
        return args.content
    raise SystemExit("请通过 --content 或 --file 提供素材")


def check_config(_: argparse.Namespace) -> int:
    settings = Settings.from_env()
    print("配置检查")
    print(f"- MCP_URL: {settings.mcp_url or '(未配置，允许账号单独配置)'}")
    print(f"- AUTO_PUBLISH: {settings.auto_publish}")
    print(f"- accounts: {len(settings.accounts)}")
    for account in settings.accounts:
        cookie_state = "已配置" if account.cookie else "缺失"
        proxy_state = account.proxy_url or "默认出口"
        mcp_state = account.mcp_url or "沿用全局"
        print(
            f"  - {account.key} ({account.name}): cookie {cookie_state}; "
            f"proxy {proxy_state}; mcp {mcp_state}"
        )
    print(f"- LLM_API_KEY: {'已配置' if settings.llm_api_key else '缺失'}")
    print(f"- LLM_BASE_URL: {'已配置' if settings.llm_base_url else '缺失'}")
    print(f"- LLM_MODEL: {'已配置' if settings.llm_model else '缺失'}")
    print(f"- DASHSCOPE_API_KEY: {'已配置' if settings.dashscope_api_key else '缺失'}")
    print(
        "- WEB_AUTH: "
        f"{'已配置' if settings.web_auth_username and settings.web_auth_password else '未配置'}"
    )
    return 0


def list_tools(_: argparse.Namespace) -> int:
    settings = Settings.from_env()
    mcp_url, auth_token, account_key = _resolve_mcp_target(settings)
    client = RemoteMCPClient(mcp_url, auth_token=auth_token)
    client.initialize()
    tools = client.list_tools()
    target = f"{mcp_url} (account={account_key})" if account_key else mcp_url
    print(f"远程 MCP 工具: {target}")
    for tool in tools:
        required = tool.input_schema.get("required", []) if tool.input_schema else []
        print(f"- {tool.name}")
        if required:
            print(f"  required: {', '.join(required)}")
        if tool.description:
            print(f"  {tool.description.splitlines()[0]}")
    return 0


def run_content(args: argparse.Namespace) -> int:
    content = _read_content(args)
    from .services import build_services

    services = build_services()
    if args.no_publish:
        services.operator.auto_publish = False
    runs = services.operator.generate_for_all_accounts(
        content=content,
        title=args.title,
        url=args.url,
    )
    for run in runs:
        print(f"[{run.account_key}]")
        if run.error:
            print(f"  error: {run.error}")
            continue
        print(f"  generated_ids: {run.generated_ids}")
        print(f"  approved_generated_id: {run.approved_generated_id}")
        if run.publish_result:
            print(f"  publish_success: {run.publish_result.success}")
            print(f"  publish_result: {run.publish_result.result}")
        else:
            print("  publish: skipped")
    return 0


def serve(args: argparse.Namespace) -> int:
    import uvicorn

    settings = Settings.from_env()
    web_auth_ready = bool(
        settings.web_auth_username and settings.web_auth_password
    )
    if (
        _is_public_bind(args.host)
        and not web_auth_ready
        and not settings.allow_insecure_public_bind
    ):
        raise SystemExit(
            "拒绝在未启用认证时监听公网地址。请配置 WEB_AUTH_USERNAME / "
            "WEB_AUTH_PASSWORD，或仅监听 127.0.0.1。"
        )
    uvicorn.run(
        "bn_square_agent.webapp:app",
        host=args.host,
        port=args.port,
        reload=False,
    )
    return 0


def serve_mcp(args: argparse.Namespace) -> int:
    import uvicorn

    auth_token = os.getenv("MCP_SERVER_AUTH_TOKEN", "").strip()
    allow_insecure = os.getenv("ALLOW_INSECURE_PUBLIC_BIND", "0").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    if _is_public_bind(args.host) and not auth_token and not allow_insecure:
        raise SystemExit(
            "拒绝在未启用认证时公开 MCP。请配置 MCP_SERVER_AUTH_TOKEN，"
            "或仅监听 127.0.0.1。"
        )
    uvicorn.run(
        "bn_square_agent.publishing.self_hosted_mcp:app",
        host=args.host,
        port=args.port,
        reload=False,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bn-square-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="检查本地配置")
    check.set_defaults(func=check_config)

    tools = subparsers.add_parser("tools", help="列出远程 MCP 工具")
    tools.set_defaults(func=list_tools)

    run = subparsers.add_parser("run", help="多账号生成终稿并按配置自动发布")
    run.add_argument("--content", help="直接传入素材文本")
    run.add_argument("--file", help="从 UTF-8 文本文件读取素材")
    run.add_argument("--title", default=None, help="素材标题")
    run.add_argument("--url", default=None, help="素材来源链接")
    run.add_argument("--no-publish", action="store_true", help="只生成不发布")
    run.set_defaults(func=run_content)

    server = subparsers.add_parser("serve", help="启动本地 Web 管理台")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8787)
    server.set_defaults(func=serve)

    mcp_server = subparsers.add_parser(
        "serve-mcp",
        help="启动自建 Binance Square 发布 MCP 服务",
    )
    mcp_server.add_argument("--host", default="127.0.0.1")
    mcp_server.add_argument("--port", type=int, default=8788)
    mcp_server.set_defaults(func=serve_mcp)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
