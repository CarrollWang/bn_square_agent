from __future__ import annotations

from dataclasses import dataclass

from .ai.agents import AnalysisAgent, ContentReviewAgent, StyleProfileAgent, WriterAgent
from .ai.llm import StructuredLLM
from .core.config import AccountConfig, Settings
from .knowledge.style_rag import StyleRAG
from .publishing.publisher import MCPPublisher, PublishingService
from .storage.database import Database
from .workflows.graphs import build_content_graph, build_profile_graph
from .workflows.operator import MultiAccountOperator


def _load_accounts_from_db(db: Database) -> tuple[AccountConfig, ...]:
    return tuple(
        AccountConfig(
            key=row["account_key"],
            name=row["name"],
            cookie=row["cookie"],
            proxy_url=row.get("proxy_url") or "",
            mcp_url=row.get("mcp_url") or "",
            mcp_auth_token=row.get("mcp_auth_token") or "",
        )
        for row in db.list_accounts()
    )


@dataclass
class Services:
    settings: Settings
    db: Database
    profile_graph: object
    content_graph: object
    publisher: MCPPublisher | None
    publishing_service: PublishingService | None
    operator: MultiAccountOperator


def build_services(settings: Settings | None = None) -> Services:
    settings = settings or Settings.from_env()
    db = settings.build_database()
    settings = settings.with_overrides(db.get_app_settings())
    llm = StructuredLLM(settings)
    for account in settings.accounts:
        db.upsert_account(
            account_key=account.key,
            name=account.name,
            cookie=account.cookie or None,
            proxy_url=account.proxy_url or None,
            mcp_url=account.mcp_url or None,
            mcp_auth_token=account.mcp_auth_token or None,
        )
    accounts = _load_accounts_from_db(db)
    rag = StyleRAG(settings)
    profile_graph = build_profile_graph(
        db,
        AnalysisAgent(llm),
        StyleProfileAgent(llm),
        rag,
    )
    content_graph = build_content_graph(
        db,
        rag,
        WriterAgent(llm),
        ContentReviewAgent(llm),
    )
    publisher = MCPPublisher(settings) if settings.auto_publish else None
    publishing_service = PublishingService(db, publisher) if publisher else None
    operator = MultiAccountOperator(
        db=db,
        accounts=accounts,
        content_graph=content_graph,
        publishing_service=publishing_service,
        auto_publish=settings.auto_publish,
    )
    return Services(
        settings,
        db,
        profile_graph,
        content_graph,
        publisher,
        publishing_service,
        operator,
    )
