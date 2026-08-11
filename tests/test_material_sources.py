from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from bn_square_agent.core.config import Settings
from bn_square_agent.core.secret_store import SecretStore
from bn_square_agent.sources.binance_square import MaterialSourceService
from bn_square_agent.sources.models import MaterialArticle
from bn_square_agent.sources.news_feeds import (
    BITCOIN_CORE_RSS_URL,
    CHAINCATCHER_API_URL,
    ETHEREUM_BLOG_REDIRECT_RSS_URL,
    ETHEREUM_BLOG_RSS_URL,
    WALLSTREETCN_API_URL,
    ChainCatcherMonitor,
    WallStreetCNMonitor,
    parse_rss_or_atom,
    validate_news_source_url,
)
from bn_square_agent.storage.database import Database
from bn_square_agent.webapp import app


def make_database(root: Path) -> Database:
    settings = replace(
        Settings.from_env(),
        app_secret_key="",
        database_path=root / "agent.sqlite3",
        secret_key_path=root / "secret.key",
        chroma_path=root / "chroma",
    )
    return Database(
        settings.database_path,
        secret_store=SecretStore.from_settings(settings),
    )


class NewsFeedParserTests(unittest.TestCase):
    def test_rss_and_atom_are_normalized_to_material_articles(self) -> None:
        rss = b"""
        <rss><channel><item>
          <title>Bitcoin miners expand capacity</title>
          <link>https://www.wublock123.com/news/btc-1</link>
          <pubDate>Tue, 11 Aug 2026 01:00:00 GMT</pubDate>
          <guid>btc-1</guid>
          <description><![CDATA[<p>BTC mining update</p>]]></description>
        </item></channel></rss>
        """
        rss_articles = parse_rss_or_atom(
            rss,
            author="吴说区块链",
            item_domains=("wublock123.com",),
        )
        self.assertEqual(rss_articles[0].external_id, "btc-1")
        self.assertIn("BTC mining update", rss_articles[0].content)

        atom = b"""
        <feed xmlns="http://www.w3.org/2005/Atom"><entry>
          <title>Bitcoin Core 31.0</title>
          <link href="https://bitcoincore.org/en/releases/31.0/" />
          <id>core-31</id>
          <updated>2026-08-11T02:00:00Z</updated>
          <summary>New Bitcoin Core release</summary>
        </entry></feed>
        """
        atom_articles = parse_rss_or_atom(
            atom,
            author="Bitcoin Core",
            item_domains=("bitcoincore.org",),
        )
        self.assertEqual(atom_articles[0].title, "Bitcoin Core 31.0")
        self.assertTrue(atom_articles[0].source_created_at.endswith("+00:00"))

    def test_rss_item_link_cannot_escape_allowed_domain(self) -> None:
        raw = b"""
        <rss><channel><item>
          <title>Injected item</title>
          <link>https://evil.example/private</link>
          <pubDate>Tue, 11 Aug 2026 01:00:00 GMT</pubDate>
        </item></channel></rss>
        """
        with self.assertRaises(ValueError):
            parse_rss_or_atom(
                raw,
                author="Official",
                item_domains=("bitcoincore.org",),
            )

    def test_wallstreetcn_filters_calendar_and_irrelevant_news(self) -> None:
        payload = {
            "data": {
                "items": [
                    {
                        "id": 1,
                        "title": "美国 CPI 数据公布，比特币短线波动",
                        "content_text": "BTC 随美元指数回落上涨",
                        "uri": "https://wallstreetcn.com/livenews/1",
                        "display_time": 1786410000,
                        "is_calendar": False,
                    },
                    {
                        "id": 2,
                        "title": "某地天气晴朗",
                        "content_text": "普通社会新闻",
                        "uri": "https://wallstreetcn.com/livenews/2",
                        "display_time": 1786410001,
                        "is_calendar": False,
                    },
                    {
                        "id": 3,
                        "title": "美联储日程",
                        "content_text": "日历预告",
                        "uri": "https://wallstreetcn.com/livenews/3",
                        "display_time": 1786410002,
                        "is_calendar": True,
                    },
                ]
            }
        }
        articles = WallStreetCNMonitor.parse(json.dumps(payload).encode())
        self.assertEqual([article.external_id for article in articles], ["1"])

    def test_chaincatcher_filters_non_crypto_items(self) -> None:
        payload = {
            "data": {
                "items": [
                    {
                        "id": 10,
                        "title": "Ethereum network upgrade scheduled",
                        "description": "ETH developers confirmed the upgrade window",
                        "url": "https://www.chaincatcher.com/article/10",
                        "releaseTimeStamp": 1786410000000,
                    },
                    {
                        "id": 11,
                        "title": "机器人企业发布 AI solution 并启动 IPO",
                        "description": "人形机器人融资进展，预计下季度交付",
                        "url": "https://www.chaincatcher.com/article/11",
                        "releaseTimeStamp": 1786410001000,
                    },
                ]
            }
        }
        articles = ChainCatcherMonitor.parse(json.dumps(payload).encode())
        self.assertEqual([article.external_id for article in articles], ["10"])

    def test_source_urls_are_exact_allowlisted_presets(self) -> None:
        self.assertEqual(
            validate_news_source_url("rss_feed", ETHEREUM_BLOG_RSS_URL),
            ETHEREUM_BLOG_RSS_URL,
        )
        self.assertEqual(
            validate_news_source_url("rss_feed", ETHEREUM_BLOG_REDIRECT_RSS_URL),
            ETHEREUM_BLOG_REDIRECT_RSS_URL,
        )
        self.assertEqual(
            validate_news_source_url("rss_feed", BITCOIN_CORE_RSS_URL),
            BITCOIN_CORE_RSS_URL,
        )
        self.assertEqual(
            validate_news_source_url("wallstreetcn_live", WALLSTREETCN_API_URL),
            WALLSTREETCN_API_URL,
        )
        self.assertEqual(
            validate_news_source_url("chaincatcher_flash", CHAINCATCHER_API_URL),
            CHAINCATCHER_API_URL,
        )
        with self.assertRaises(ValueError):
            validate_news_source_url("rss_feed", "https://evil.example/feed.xml")


class MaterialSourceServiceTests(unittest.TestCase):
    def test_single_source_failure_does_not_block_later_sources(self) -> None:
        class FakeDatabase:
            def __init__(self) -> None:
                self.checks = []
                self.items = []

            def list_material_sources(self):
                return [
                    {"id": 1, "source_type": "rss_feed", "url": ETHEREUM_BLOG_RSS_URL},
                    {"id": 2, "source_type": "chaincatcher_flash", "url": CHAINCATCHER_API_URL},
                ]

            def update_material_source_check(self, source_id, error=None):
                self.checks.append((source_id, error))

            def add_material_item(self, **values):
                self.items.append(values)
                return len(self.items), True

        class FailingMonitor:
            def fetch(self, _url):
                raise RuntimeError("feed unavailable")

        class HealthyMonitor:
            def fetch(self, _url):
                return [
                    MaterialArticle(
                        title="BTC update",
                        content="BTC market update",
                        url="https://www.chaincatcher.com/article/1",
                    )
                ]

        db = FakeDatabase()
        service = MaterialSourceService(db)
        service.rss_feed = FailingMonitor()
        service.chaincatcher_flash = HealthyMonitor()
        results = service.check_all()
        self.assertEqual(results[0]["error"], "feed unavailable")
        self.assertEqual(results[1]["inserted"], 1)
        self.assertEqual(db.checks[0], (1, "feed unavailable"))
        self.assertEqual(db.checks[1], (2, None))


class MaterialSourceDatabaseTests(unittest.TestCase):
    def test_source_type_filter_and_new_types(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = make_database(Path(temp_dir))
            rss_id = db.upsert_material_source(
                name="Ethereum",
                source_type="rss_feed",
                url=ETHEREUM_BLOG_RSS_URL,
            )
            chain_id = db.upsert_material_source(
                name="ChainCatcher",
                source_type="chaincatcher_flash",
                url=CHAINCATCHER_API_URL,
            )
            rss_item, _ = db.add_material_item(source_id=rss_id, content="Ethereum update")
            db.add_material_item(source_id=chain_id, content="BTC update")
            rows = db.list_material_items(source_type="rss_feed")
            self.assertEqual([row["id"] for row in rows], [rss_item])

    def test_legacy_source_table_migration_preserves_material_foreign_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = make_database(root)
            source_id = db.upsert_material_source(
                name="TechFlow",
                source_type="techflow_newsletter",
                url="https://www.techflowpost.com/newsletter",
            )
            material_id, _ = db.add_material_item(source_id=source_id, content="BTC legacy")
            with sqlite3.connect(db.path) as connection:
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute("PRAGMA legacy_alter_table = ON")
                connection.executescript(
                    """
                    ALTER TABLE material_items RENAME TO material_items_current;
                    ALTER TABLE material_sources RENAME TO material_sources_current;
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
                    INSERT INTO material_sources SELECT * FROM material_sources_current;
                    DROP TABLE material_sources_current;
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
                        status TEXT NOT NULL DEFAULT 'new' CHECK(
                            status IN ('new', 'used', 'ignored', 'failed')
                        ),
                        tag_status TEXT NOT NULL DEFAULT 'pending',
                        tag_json TEXT,
                        tag_error TEXT,
                        tagged_at TEXT,
                        error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY(source_id) REFERENCES material_sources(id)
                    );
                    INSERT INTO material_items SELECT * FROM material_items_current;
                    DROP TABLE material_items_current;
                    """
                )
                connection.execute("PRAGMA legacy_alter_table = OFF")

            migrated = make_database(root)
            migrated.init_schema()
            migrated.upsert_material_source(
                name="ChainCatcher",
                source_type="chaincatcher_flash",
                url=CHAINCATCHER_API_URL,
            )
            with sqlite3.connect(migrated.path) as connection:
                connection.row_factory = sqlite3.Row
                foreign_keys = connection.execute(
                    "PRAGMA foreign_key_list(material_items)"
                ).fetchall()
                violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            source_target = next(
                row["table"] for row in foreign_keys if row["from"] == "source_id"
            )
            self.assertEqual(source_target, "material_sources")
            self.assertEqual(violations, [])
            self.assertEqual(migrated.get_material_item(material_id)["source_id"], source_id)


class MaterialSourceApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def test_api_accepts_allowlisted_feed_and_rejects_arbitrary_feed(self) -> None:
        class FakeDatabase:
            def upsert_material_source(self, **values):
                self.values = values
                return 7

        db = FakeDatabase()
        with patch("bn_square_agent.webapp.get_db", return_value=db):
            accepted = self.client.post(
                "/api/material-sources",
                json={
                    "name": "Ethereum",
                    "source_type": "rss_feed",
                    "url": ETHEREUM_BLOG_RSS_URL,
                },
            )
            rejected = self.client.post(
                "/api/material-sources",
                json={
                    "name": "Evil",
                    "source_type": "rss_feed",
                    "url": "https://evil.example/feed.xml",
                },
            )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(db.values["source_type"], "rss_feed")
        self.assertEqual(rejected.status_code, 400)


if __name__ == "__main__":
    unittest.main()
