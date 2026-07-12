from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from bn_square_agent.ai.material_tagger import MaterialTagger
from bn_square_agent.core.config import AccountConfig, Settings
from bn_square_agent.core.secret_store import SecretStore
from bn_square_agent.publishing.binance_square_openapi import (
    BinanceSquareOpenAPIClient,
    BinanceSquarePublishResult,
)
from bn_square_agent.publishing.chart_image import ChartTarget
from bn_square_agent.publishing.self_hosted_mcp import (
    SelfHostedMCPSettings,
    _ensure_trading_component,
    _publish,
)
from bn_square_agent.storage.database import Database, PublishRateLimitError
from bn_square_agent.sources.service import MaterialSourceService
from bn_square_agent.sources.models import MaterialArticle
from bn_square_agent.workflows.operator import AccountContentRun, MultiAccountOperator


class PublisherLogicTests(unittest.TestCase):
    def test_mcp_publisher_passes_account_key_but_not_openapi_key(self) -> None:
        from bn_square_agent.publishing.publisher import MCPPublisher

        settings = MagicMock(spec=Settings)
        settings.mcp_publish_tool = "publish_binance_square"
        settings.mcp_url = "http://127.0.0.1:8788/mcp"
        settings.mcp_auth_token = "token"
        settings.validate_for_publish.return_value = None
        publisher = MCPPublisher(settings)
        client = MagicMock()
        client.call_tool.return_value = {"success": True}
        publisher._client_for_account = MagicMock(return_value=client)
        publisher.chart_images.extract_target = MagicMock(return_value=None)
        publisher.chart_images.image_for_text = MagicMock(return_value=None)

        publisher.publish(
            account=AccountConfig(
                key="main",
                name="Main",
                square_openapi_key="secret-key",
            ),
            generated={"content": "hello"},
        )
        arguments = client.call_tool.call_args.args[1]
        self.assertEqual(arguments["account_key"], "main")
        self.assertNotIn("square_openapi_key", arguments)
        self.assertNotIn("cookie", arguments)

    def test_mcp_publisher_preserves_future_marker(self) -> None:
        from bn_square_agent.publishing.publisher import MCPPublisher

        settings = MagicMock(spec=Settings)
        settings.mcp_publish_tool = "publish_binance_square"
        settings.mcp_url = "http://127.0.0.1:8788/mcp"
        settings.mcp_auth_token = "token"
        settings.validate_for_publish.return_value = None
        publisher = MCPPublisher(settings)
        client = MagicMock()
        client.call_tool.return_value = {"success": True}
        publisher._client_for_account = MagicMock(return_value=client)
        publisher.chart_images.extract_target = MagicMock(
            return_value=ChartTarget("BTCUSDT", "future")
        )
        publisher.chart_images.image_for_text = MagicMock(return_value=None)

        publisher.publish(
            account=AccountConfig(
                key="main",
                name="Main",
                square_openapi_key="secret-key",
            ),
            generated={"content": "等待 BTC 突破确认。\n\n{future}(BTCUSDT)"},
        )

        arguments = client.call_tool.call_args.args[1]
        self.assertEqual(
            arguments["content"],
            "等待 BTC 突破确认。\n\n{future}(BTCUSDT)",
        )
        self.assertEqual(arguments["coins"], "BTC:future")

    def test_mcp_boundary_appends_future_marker_from_coins(self) -> None:
        for coin in ("BTC", "ETH", "SOL"):
            with self.subTest(coin=coin):
                self.assertEqual(
                    _ensure_trading_component("等待突破确认。", f"{coin}:future"),
                    f"${coin} 等待突破确认。\n\n{{future}}({coin}USDT)",
                )

    def test_mcp_boundary_marks_bare_primary_coin_mentions_as_cashtags(self) -> None:
        self.assertEqual(
            _ensure_trading_component(
                "LAB 庄家转移 1850 万枚 LAB。\n\n{future}(LABUSDT)",
                "LAB:future",
            ),
            "$LAB 庄家转移 1850 万枚 $LAB。\n\n{future}(LABUSDT)",
        )

    def test_mcp_boundary_does_not_duplicate_existing_cashtags(self) -> None:
        self.assertEqual(
            _ensure_trading_component(
                "$LAB 庄家转移 $LAB。\n\n{future}(LABUSDT)",
                "LAB:future",
            ),
            "$LAB 庄家转移 $LAB。\n\n{future}(LABUSDT)",
        )

    def test_spot_coin_metadata_also_normalizes_bare_mentions(self) -> None:
        self.assertEqual(
            _ensure_trading_component("关注 SOL 和 SOL 生态。", "SOL:spot"),
            "关注 $SOL 和 $SOL 生态。",
        )

    def test_mcp_boundary_marks_secondary_spot_assets_but_not_generic_acronyms(
        self,
    ) -> None:
        self.assertEqual(
            _ensure_trading_component(
                "SOL 链上新增 USDC，AI API 与 DEX 数据同步更新。",
                "SOL:future",
                valid_cashtag_tokens={"SOL", "USDC", "AI"},
            ),
            "$SOL 链上新增 $USDC，AI API 与 DEX 数据同步更新。"
            "\n\n{future}(SOLUSDT)",
        )

    def test_mcp_boundary_rejects_conflicting_future_marker(self) -> None:
        with self.assertRaisesRegex(ValueError, "不一致"):
            _ensure_trading_component(
                "等待突破确认。\n\n{future}(ETHUSDT)",
                "BTC:future",
            )

    def test_material_symbol_uses_news_asset_with_btc_fallback(self) -> None:
        self.assertEqual(
            MultiAccountOperator._symbol_from_material(
                {
                    "tag_json": '{"symbol": "ETHUSDT"}',
                    "title": "以太坊升级",
                    "content": "升级计划已经公布。",
                }
            ),
            "ETHUSDT",
        )
        self.assertEqual(
            MultiAccountOperator._symbol_from_material(
                {
                    "tag_json": '{"symbol": null}',
                    "title": "AI Agent 行业进展",
                    "content": "行业发布了新的智能体框架。",
                }
            ),
            "BTCUSDT",
        )

    def test_openapi_504_is_unknown_not_success(self) -> None:
        client = BinanceSquareOpenAPIClient("secret")
        with patch.object(
            client,
            "_api",
            return_value={
                "id": None,
                "shareLink": None,
                "publishStatus": "success_without_post_id",
            },
        ):
            result = client.publish_text("hello")
        self.assertFalse(result.success)
        self.assertEqual(result.outcome, "unknown")

    def test_openapi_success_exposes_post_evidence(self) -> None:
        client = BinanceSquareOpenAPIClient("secret")
        with patch.object(
            client,
            "_api",
            return_value={"id": "123", "shareLink": "https://www.binance.com/square/post/123"},
        ):
            result = client.publish_text("hello")
        self.assertTrue(result.success)
        self.assertEqual(result.post_id, "123")
        self.assertEqual(result.post_url, "https://www.binance.com/square/post/123")

    def test_self_hosted_mcp_reserves_and_finalizes_account_quota(self) -> None:
        database = MagicMock()
        database.reserve_publish_slot.return_value = "slot-1"
        effective_settings = MagicMock()
        effective_settings.max_posts_per_account_per_hour = 5
        effective_settings.max_posts_per_account_per_day = 80
        account = {"square_openapi_key": "secret", "proxy_url": ""}
        client = MagicMock()
        client.publish_text.return_value = BinanceSquarePublishResult(
            success=True,
            outcome="published",
            message="ok",
            post_id="123",
            post_url="https://www.binance.com/square/post/123",
        )
        with (
            patch(
                "bn_square_agent.publishing.self_hosted_mcp._load_account_context",
                return_value=(database, effective_settings, account),
            ),
            patch(
                "bn_square_agent.publishing.self_hosted_mcp.BinanceSquareOpenAPIClient",
                return_value=client,
            ),
            patch(
                "bn_square_agent.publishing.self_hosted_mcp.spot_asset_catalog.get",
                return_value=frozenset({"ETH"}),
            ),
            patch(
                "bn_square_agent.publishing.self_hosted_mcp.futures_symbol_catalog.get",
                return_value=frozenset({"ETHUSDT"}),
            ),
        ):
            result = _publish(
                SelfHostedMCPSettings(
                    auth_token="token",
                    allow_insecure_public_bind=False,
                    timeout_seconds=90,
                ),
                content="关注 ETH 升级。",
                account_key="main",
                coins="ETH:future",
                image_base64="",
            )

        database.reserve_publish_slot.assert_called_once_with(
            "main",
            hourly_limit=5,
            daily_limit=80,
        )
        database.finalize_publish_slot.assert_called_once_with(
            "slot-1",
            status="published",
            post_id="123",
        )
        client.publish_text.assert_called_once_with(
            "关注 $ETH 升级。\n\n{future}(ETHUSDT)",
            image_base64="",
        )
        self.assertTrue(result["success"])

    def test_publishing_service_preserves_rate_limited_outcome(self) -> None:
        from bn_square_agent.publishing.publisher import PublishingService

        result = {
            "success": False,
            "structuredContent": {
                "success": False,
                "outcome": "rate_limited",
                "message": "账号 main 已达到滚动 1 小时发布上限 5 篇",
            },
        }
        self.assertEqual(
            PublishingService._publish_outcome(result),
            "rate_limited",
        )


class SourceRuntimeTests(unittest.TestCase):
    def test_techflow_redirect_cannot_escape_allowed_domain(self) -> None:
        from bn_square_agent.sources.techflow import TechFlowNewsletterMonitor

        response = MagicMock()
        response.is_redirect = True
        response.url = "https://www.techflowpost.com/newsletter"
        response.headers = {"location": "https://evil.example/redirected"}
        client = MagicMock()
        client.get.return_value = response
        with self.assertRaises(ValueError):
            TechFlowNewsletterMonitor._get_with_safe_redirects(
                client,
                "https://www.techflowpost.com/newsletter",
            )


class DatabaseRuntimeTests(unittest.TestCase):
    def test_migrates_legacy_sources_to_news_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database_path = root / "legacy.db"
            connection = sqlite3.connect(database_path)
            connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE accounts (
                    account_key TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    square_openapi_key TEXT NOT NULL DEFAULT '',
                    proxy_url TEXT NOT NULL DEFAULT '',
                    mcp_url TEXT NOT NULL DEFAULT '',
                    mcp_auth_token TEXT NOT NULL DEFAULT '',
                    signature_key TEXT,
                    check_status TEXT NOT NULL DEFAULT 'unchecked',
                    checked_at TEXT,
                    check_error TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE material_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    source_type TEXT NOT NULL CHECK(
                        source_type IN ('binance_square', 'techflow_newsletter')
                    ),
                    url TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_checked_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_type, url)
                );
                CREATE TABLE material_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id INTEGER,
                    external_id TEXT,
                    author TEXT,
                    title TEXT,
                    content TEXT NOT NULL,
                    url TEXT,
                    source_created_at TEXT,
                    hash TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'new',
                    tag_status TEXT NOT NULL DEFAULT 'pending',
                    tag_json TEXT,
                    tag_error TEXT,
                    tagged_at TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(source_id) REFERENCES material_sources(id)
                );
                CREATE TABLE material_account_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    material_item_id INTEGER NOT NULL,
                    account_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    generated_id INTEGER,
                    publish_json TEXT,
                    error TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_attempted_at TEXT,
                    published_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(material_item_id, account_key),
                    FOREIGN KEY(material_item_id) REFERENCES material_items(id)
                );
                INSERT INTO material_sources VALUES
                    (1, 'TechFlow', 'techflow_newsletter', 'https://www.techflowpost.com/newsletter?is_hot=1', 1, NULL, NULL, 'now', 'now'),
                    (2, 'BN author', 'binance_square', 'https://www.binance.com/zh-CN/square/profile/demo', 1, NULL, NULL, 'now', 'now');
                INSERT INTO material_items (
                    id, source_id, content, hash, status, tag_status, created_at, updated_at
                ) VALUES
                    (1, 1, 'news item', 'news-hash', 'new', 'pending', 'now', 'now'),
                    (2, 2, 'creator item', 'creator-hash', 'new', 'accepted', 'now', 'now');
                INSERT INTO accounts (account_key, name, created_at)
                VALUES ('main', 'Main', 'now');
                INSERT INTO material_account_runs (
                    id, material_item_id, account_key, status,
                    attempt_count, created_at, updated_at
                ) VALUES (1, 2, 'main', 'published', 1, 'now', 'now');
                """
            )
            connection.commit()
            connection.close()

            database = Database(
                database_path,
                secret_store=SecretStore.from_values(
                    app_secret_key="",
                    secret_key_path=root / "secret.key",
                ),
            )

            sources = database.list_material_sources(include_disabled=True)
            self.assertEqual([(row["id"], row["source_type"]) for row in sources], [(1, "news_feed")])
            with database.connect() as migrated:
                creator_item = migrated.execute(
                    "SELECT source_id, status, error FROM material_items WHERE id = 2"
                ).fetchone()
                run_foreign_keys = migrated.execute(
                    "PRAGMA foreign_key_list(material_account_runs)"
                ).fetchall()
                preserved_run = migrated.execute(
                    "SELECT material_item_id, account_key, status FROM material_account_runs WHERE id = 1"
                ).fetchone()
            self.assertIsNone(creator_item["source_id"])
            self.assertEqual(creator_item["status"], "ignored")
            self.assertEqual(creator_item["error"], "binance_square_source_removed")
            self.assertIn("material_items", {row["table"] for row in run_foreign_keys})
            self.assertNotIn("material_items_old", {row["table"] for row in run_foreign_keys})
            self.assertEqual(tuple(preserved_run), (2, "main", "published"))

    @staticmethod
    def _database(root: Path) -> Database:
        store = SecretStore.from_values(
            app_secret_key="",
            secret_key_path=root / "secret.key",
        )
        database = Database(root / "test.db", secret_store=store)
        database.upsert_account(
            account_key="main",
            name="Main",
            square_openapi_key="square-secret-key",
        )
        return database

    def test_publish_rate_limit_blocks_sixth_post_in_rolling_hour(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = self._database(Path(temp_dir))
            now = datetime(2026, 7, 12, tzinfo=timezone.utc)
            for index in range(5):
                slot = database.reserve_publish_slot(
                    "main",
                    hourly_limit=5,
                    daily_limit=80,
                    now=now + timedelta(minutes=index),
                )
                database.finalize_publish_slot(slot, status="published")
            with self.assertRaisesRegex(PublishRateLimitError, "1 小时"):
                database.reserve_publish_slot(
                    "main",
                    hourly_limit=5,
                    daily_limit=80,
                    now=now + timedelta(minutes=5),
                )

    def test_publish_rate_limit_blocks_eighty_first_post_in_rolling_day(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = self._database(Path(temp_dir))
            now = datetime(2026, 7, 12, tzinfo=timezone.utc)
            for index in range(80):
                event_at = now + timedelta(minutes=index * 15)
                slot = database.reserve_publish_slot(
                    "main",
                    hourly_limit=5,
                    daily_limit=80,
                    now=event_at,
                )
                database.finalize_publish_slot(slot, status="published")
            with self.assertRaisesRegex(PublishRateLimitError, "24 小时"):
                database.reserve_publish_slot(
                    "main",
                    hourly_limit=5,
                    daily_limit=80,
                    now=now + timedelta(minutes=80 * 15),
                )

    def test_failed_publish_does_not_consume_quota_but_unknown_does(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = self._database(Path(temp_dir))
            now = datetime(2026, 7, 12, tzinfo=timezone.utc)
            failed_slot = database.reserve_publish_slot(
                "main",
                hourly_limit=1,
                daily_limit=1,
                now=now,
            )
            database.finalize_publish_slot(failed_slot, status="failed")
            unknown_slot = database.reserve_publish_slot(
                "main",
                hourly_limit=1,
                daily_limit=1,
                now=now + timedelta(minutes=1),
            )
            database.finalize_publish_slot(unknown_slot, status="unknown")
            with self.assertRaises(PublishRateLimitError):
                database.reserve_publish_slot(
                    "main",
                    hourly_limit=1,
                    daily_limit=1,
                    now=now + timedelta(minutes=2),
                )

    def test_rate_limit_migration_bootstraps_existing_published_posts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = self._database(root)
            published_at = datetime(2026, 7, 12, tzinfo=timezone.utc)
            with database.connect() as connection:
                source_id = connection.execute(
                    """
                    INSERT INTO source_posts (
                        account_key, author, title, content, url,
                        source_created_at, role, hash, analysis_status, created_at
                    ) VALUES ('main', NULL, 'title', 'source', NULL, NULL,
                        'material', 'bootstrap-source', 'not_required', ?)
                    """,
                    (published_at.isoformat(),),
                ).lastrowid
                connection.execute(
                    """
                    INSERT INTO generated_posts (
                        source_post_id, candidate_index, original_content,
                        content, status, review_json, rewrite_count,
                        created_at, updated_at, account_key, publish_status,
                        publish_json, published_at
                    ) VALUES (?, 0, 'source', 'post', 'approved', NULL, 0,
                        ?, ?, 'main', 'published', NULL, ?)
                    """,
                    (
                        source_id,
                        published_at.isoformat(),
                        published_at.isoformat(),
                        published_at.isoformat(),
                    ),
                )
                connection.execute("DROP TABLE publish_rate_events")

            reloaded = Database(
                root / "test.db",
                secret_store=SecretStore.from_values(
                    app_secret_key="",
                    secret_key_path=root / "secret.key",
                ),
            )
            with self.assertRaises(PublishRateLimitError):
                reloaded.reserve_publish_slot(
                    "main",
                    hourly_limit=1,
                    daily_limit=1,
                    now=published_at + timedelta(minutes=1),
                )

    def test_editorial_queue_does_not_require_long_or_short_direction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = SecretStore.from_values(
                app_secret_key="",
                secret_key_path=root / "secret.key",
            )
            database = Database(root / "test.db", secret_store=store)
            source_id = database.upsert_material_source(
                name="TechFlow",
                source_type="news_feed",
                url="https://www.techflowpost.com/newsletter?is_hot=1",
            )
            item_id, _ = database.add_material_item(
                source_id=source_id,
                title="BTC 矿企公布月度产量",
                content="矿企公布最新比特币产量和持仓数据，但没有给出任何多空判断。",
            )
            tag = MaterialTagger().tag(
                title="BTC 矿企公布月度产量",
                content="矿企公布最新比特币产量和持仓数据，但没有给出任何多空判断。",
            )
            database.save_material_tag(
                item_id,
                tag_status="accepted",
                tag=tag.to_dict(),
            )

            queue = database.list_material_queue_for_account("main", limit=10)
            self.assertEqual([item["id"] for item in queue], [item_id])

    def test_material_source_skips_articles_older_than_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = Database(
                root / "test.db",
                secret_store=SecretStore.from_values(
                    app_secret_key="",
                    secret_key_path=root / "secret.key",
                ),
            )
            source_id = database.upsert_material_source(
                name="测试新闻源",
                source_type="news_feed",
                url="https://www.panewslab.com/rss.xml",
            )
            source = next(
                row for row in database.list_material_sources() if row["id"] == source_id
            )
            service = MaterialSourceService(
                database,
                material_ttl_seconds=86_400,
            )
            service.news_feed.fetch = MagicMock(
                return_value=[
                    MaterialArticle(
                        title="旧闻",
                        content="这是一条两天前发布的加密市场旧闻，不应重新进入自动发布队列。",
                        external_id="old",
                        source_created_at=(
                            datetime.now(timezone.utc) - timedelta(days=2)
                        ).isoformat(),
                    ),
                    MaterialArticle(
                        title="新消息",
                        content="这是一条刚刚发布的加密市场新消息，可以进入后续素材判断。",
                        external_id="fresh",
                        source_created_at=datetime.now(timezone.utc).isoformat(),
                    ),
                ]
            )

            result = service.check_source(source)

            self.assertEqual(result["found"], 2)
            self.assertEqual(result["stale_skipped"], 1)
            self.assertEqual(result["inserted"], 1)
            items = database.list_material_items(status="new")
            self.assertEqual([item["external_id"] for item in items], ["fresh"])

    def test_openapi_key_remains_encrypted_and_can_be_rotated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = SecretStore.from_values(
                app_secret_key="",
                secret_key_path=root / "secret.key",
            )
            database = Database(root / "test.db", secret_store=store)
            database.upsert_account(
                account_key="test",
                name="Test",
                square_openapi_key="square-secret-key",
            )
            with database.connect() as connection:
                raw = connection.execute(
                    "SELECT square_openapi_key FROM accounts WHERE account_key = 'test'"
                ).fetchone()[0]
            self.assertNotEqual(raw, "square-secret-key")
            self.assertEqual(
                database.list_accounts()[0]["square_openapi_key"],
                "square-secret-key",
            )

            database.upsert_account(
                account_key="test",
                name="Test",
                square_openapi_key="square-fresh-key",
            )
            self.assertEqual(
                database.list_accounts()[0]["square_openapi_key"],
                "square-fresh-key",
            )


class OperatorBatchTests(unittest.TestCase):
    @staticmethod
    def _operator(accounts: tuple[AccountConfig, ...]) -> MultiAccountOperator:
        operator = object.__new__(MultiAccountOperator)
        operator.accounts = accounts
        operator.db = MagicMock()
        operator._account_requires_material_run = MagicMock(return_value=True)
        operator.run_material_item_for_account = MagicMock(
            side_effect=lambda material_id, account_key: AccountContentRun(
                account_key=account_key,
                status="published",
            )
        )
        return operator

    def test_single_account_can_consume_five_materials_per_round(self) -> None:
        account = AccountConfig(
            key="main",
            name="Main",
            square_openapi_key="secret",
        )
        operator = self._operator((account,))
        operator.db.list_material_queue_for_account.return_value = [
            {"id": index, "title": f"material-{index}"}
            for index in range(1, 7)
        ]

        result = operator.run_pending_material_queue(
            limit_per_account=5,
            max_total_runs=5,
        )

        self.assertEqual([item["material_item_id"] for item in result], [1, 2, 3, 4, 5])
        self.assertEqual(operator.run_material_item_for_account.call_count, 5)

    def test_multiple_accounts_receive_materials_round_robin(self) -> None:
        accounts = (
            AccountConfig(key="a", name="A", square_openapi_key="a-key"),
            AccountConfig(key="b", name="B", square_openapi_key="b-key"),
        )
        operator = self._operator(accounts)
        materials = [
            {"id": index, "title": f"material-{index}"}
            for index in range(1, 7)
        ]
        operator.db.list_material_queue_for_account.side_effect = (
            lambda account_key, limit: list(materials)
        )

        result = operator.run_pending_material_queue(
            limit_per_account=5,
            max_total_runs=5,
        )

        self.assertEqual(
            [(item["account_key"], item["material_item_id"]) for item in result],
            [("a", 1), ("b", 2), ("a", 3), ("b", 4), ("a", 5)],
        )


if __name__ == "__main__":
    unittest.main()
