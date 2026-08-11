from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import sqlite3
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from bn_square_agent.core.config import AccountConfig, Settings
from bn_square_agent.core.delivery import (
    PUBLISH_PUBLISHED,
    PUBLISH_QUEUED,
    content_fingerprint,
    parse_iso_datetime,
)
from bn_square_agent.core.secret_store import SecretStore
from bn_square_agent.models.schemas import (
    Candidate,
    ContentReview,
    ReviewScores,
    StyleProfile,
)
from bn_square_agent.publishing.publisher import MCPPublisher, PublishingService
from bn_square_agent.storage.database import Database
from bn_square_agent.webapp import _process_due_publish_queue, _publish_queue_item, app
from bn_square_agent.workflows.graphs import build_content_graph, classify_gate_status


def make_review(*, passed: bool = True) -> ContentReview:
    return ContentReview(
        passed=passed,
        scores=ReviewScores(
            factual_fidelity=10 if passed else 8,
            style_match=8,
            originality=8,
            expression_quality=8,
        ),
    )


def make_profile() -> StyleProfile:
    return StyleProfile(
        persona="方向交易员",
        risk_level="中",
        favorite_topics=["BTC"],
        favorite_words=["关注"],
        opening_style="先给方向",
        tone="直接",
        beliefs=["不追高"],
        structure_patterns=["结论后补风险"],
    )


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


def save_generated(
    db: Database,
    *,
    account_key: str = "writer",
    content: str = "$BTC 继续看多",
    status: str = "pending_review",
    material_item_id: int | None = None,
) -> int:
    source_post_id, _ = db.add_source_post(
        account_key=account_key,
        role="material",
        content=f"素材：{content}",
    )
    return db.save_generated(
        source_post_id=source_post_id,
        candidate_index=1,
        original_content=content,
        content=content,
        status=status,
        review=make_review(),
        rewrite_count=0,
        account_key=account_key,
        approval_hash=(
            content_fingerprint(account_key, content) if status == "approved" else None
        ),
        material_item_id=material_item_id,
    )


def seed_material(db: Database) -> int:
    db.upsert_account(account_key="writer", name="Writer", cookie="session=test")
    source_id = db.upsert_material_source(
        name="Source",
        source_type="techflow_newsletter",
        url="https://www.techflowpost.com/newsletter",
    )
    material_id, _ = db.add_material_item(
        source_id=source_id,
        title="BTC 方向交易",
        content="$BTC 继续看多，关注回踩机会",
        url="https://example.com/btc",
    )
    db.save_material_tag(
        material_id,
        tag_status="accepted",
        tag={"symbol": "BTCUSDT", "direction": "long", "strategy": "directional_v1"},
    )
    return material_id


class GateTests(unittest.TestCase):
    def test_gate_classification_covers_ok_manual_review_and_blocked(self) -> None:
        ok = classify_gate_status(
            review=make_review(),
            source_url="https://example.com/source",
            direction="long",
            source_enabled=True,
            chart_image_failed=False,
        )
        self.assertEqual(ok.status, "ok")
        self.assertEqual(ok.reasons, [])

        cases = (
            ({"source_url": None}, "source_url_missing"),
            ({"direction": None}, "direction_untagged"),
            ({"source_enabled": False}, "source_disabled"),
            ({"chart_image_failed": True}, "chart_image_failed"),
        )
        defaults = {
            "source_url": "https://example.com/source",
            "direction": "short",
            "source_enabled": True,
            "chart_image_failed": False,
        }
        for override, reason in cases:
            with self.subTest(reason=reason):
                decision = classify_gate_status(
                    review=make_review(),
                    **{**defaults, **override},
                )
                self.assertEqual(decision.status, "manual_review")
                self.assertIn(reason, decision.reasons)

        blocked = classify_gate_status(
            review=make_review(passed=False),
            **defaults,
        )
        self.assertEqual(blocked.status, "blocked")
        self.assertEqual(blocked.reasons, ["review_threshold_failed"])

    def test_default_manual_review_and_auto_approval(self) -> None:
        class FakeDatabase:
            def __init__(self) -> None:
                self.saved: list[dict] = []

            def get_profile(self, _account_key):
                return make_profile()

            def add_source_post(self, **_kwargs):
                return 11, True

            def successful_analyses(self, _account_key):
                return []

            def save_generated(self, **values):
                self.saved.append(values)
                return len(self.saved)

        class FakeRag:
            def search(self, *_args, **_kwargs):
                return []

        class FakeWriter:
            def generate(self, **_kwargs):
                return [Candidate(candidate_index=1, content="$BTC 看多，留意回踩风险")]

            def rewrite(self, **_kwargs):
                raise AssertionError("通过审核的候选不应重写")

        class FakeReviewer:
            def review(self, **_kwargs):
                return make_review()

        for require_manual_review, expected_status in ((True, "pending_review"), (False, "approved")):
            with self.subTest(require_manual_review=require_manual_review):
                db = FakeDatabase()
                result = build_content_graph(
                    db,
                    FakeRag(),
                    FakeWriter(),
                    FakeReviewer(),
                ).invoke(
                    {
                        "account_key": "writer",
                        "content": "$BTC 原始素材",
                        "url": "https://example.com/source",
                        "direction": "long",
                        "source_enabled": True,
                        "require_manual_review": require_manual_review,
                    }
                )
                saved = db.saved[0]
                self.assertEqual(saved["status"], expected_status)
                self.assertEqual(saved["review"].gate.status, "ok")
                if expected_status == "approved":
                    self.assertEqual(
                        saved["approval_hash"],
                        content_fingerprint("writer", saved["content"]),
                    )
                else:
                    self.assertIsNone(saved["approval_hash"])
                self.assertEqual(result["approved_generated_id"], 1)


class ApprovalAndQueueTests(unittest.TestCase):
    def test_manual_publish_mode_can_start_before_account_cookie_is_configured(self) -> None:
        settings = replace(
            Settings.from_env(),
            auto_publish=False,
            accounts=(AccountConfig(key="writer", name="Writer", cookie=""),),
        )
        publisher = MCPPublisher(settings, validate_accounts=settings.auto_publish)
        self.assertIs(publisher.settings, settings)

    def test_content_fingerprint_normalizes_newlines_and_outer_whitespace(self) -> None:
        self.assertEqual(
            content_fingerprint("writer", "  第一行\r\n第二行\r\n  "),
            content_fingerprint("writer", "第一行\n第二行"),
        )
        self.assertNotEqual(
            content_fingerprint("writer", "第一行\n第二行"),
            content_fingerprint("other", "第一行\n第二行"),
        )

    def test_tampered_content_is_rejected_before_publisher_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = make_database(Path(temp_dir))
            db.upsert_account(account_key="writer", name="Writer", cookie="session=test")
            generated_id = save_generated(db)
            db.approve_generated_for_queue(
                generated_id,
                min_interval_minutes=20,
                jitter_minutes=0,
            )
            with sqlite3.connect(db.path) as connection:
                connection.execute(
                    "UPDATE generated_posts SET content = ? WHERE id = ?",
                    ("$BTC 已被批准后篡改", generated_id),
                )

            class FakePublisher:
                def __init__(self) -> None:
                    self.calls = 0

                def publish(self, **_kwargs):
                    self.calls += 1
                    return {"success": True, "outcome": "published"}

            publisher = FakePublisher()
            result = PublishingService(db, publisher).publish_generated(
                account=SimpleNamespace(key="writer"),
                generated_id=generated_id,
                allow_queued=True,
            )
            self.assertFalse(result.success)
            self.assertEqual(result.result["error"], "content_modified_after_approval")
            self.assertEqual(publisher.calls, 0)

    def test_approval_schedules_each_account_with_minimum_interval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = make_database(Path(temp_dir))
            db.upsert_account(account_key="writer", name="Writer", cookie="session=test")
            first_id = save_generated(db, content="$BTC 第一篇")
            second_id = save_generated(db, content="$BTC 第二篇")
            now = datetime(2026, 8, 11, 9, 0, tzinfo=timezone(timedelta(hours=8)))

            first = db.approve_generated_for_queue(
                first_id,
                min_interval_minutes=20,
                jitter_minutes=0,
                now=now,
            )
            second = db.approve_generated_for_queue(
                second_id,
                min_interval_minutes=20,
                jitter_minutes=0,
                now=now,
            )
            first_at = parse_iso_datetime(first["scheduled_at"])
            second_at = parse_iso_datetime(second["scheduled_at"])
            self.assertIsNotNone(first_at)
            self.assertIsNotNone(second_at)
            self.assertGreaterEqual(second_at - first_at, timedelta(minutes=20))
            self.assertEqual(first["publish_status"], PUBLISH_QUEUED)
            self.assertEqual(second["publish_status"], PUBLISH_QUEUED)

    def test_daily_quota_keeps_item_queued_and_manual_override_can_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = make_database(Path(temp_dir))
            db.upsert_account(account_key="writer", name="Writer", cookie="session=test")
            published_id = save_generated(db, content="$BTC 今日第一篇", status="approved")
            db.mark_published(
                published_id,
                result={"success": True, "outcome": "published"},
                publish_status=PUBLISH_PUBLISHED,
            )
            queued_id = save_generated(db, content="$BTC 今日第二篇")
            db.approve_generated_for_queue(
                queued_id,
                min_interval_minutes=0,
                jitter_minutes=0,
            )

            class FakePublisher:
                def __init__(self) -> None:
                    self.calls = 0

                def publish(self, **_kwargs):
                    self.calls += 1
                    return {"success": True, "outcome": "published"}

            publisher = FakePublisher()
            services = SimpleNamespace(
                db=db,
                settings=replace(Settings.from_env(), publish_daily_quota_per_account=1),
                publishing_service=PublishingService(db, publisher),
                operator=SimpleNamespace(finalize_material_item=lambda _material_id: None),
            )
            skipped = _publish_queue_item(services, queued_id)
            self.assertTrue(skipped["skipped"])
            self.assertEqual(skipped["reason"], "daily_quota_reached")
            self.assertEqual(db.get_generated(queued_id)["publish_status"], PUBLISH_QUEUED)
            self.assertEqual(publisher.calls, 0)

            forced = _publish_queue_item(services, queued_id, ignore_quota=True)
            self.assertTrue(forced["success"])
            self.assertEqual(db.get_generated(queued_id)["publish_status"], PUBLISH_PUBLISHED)
            self.assertEqual(publisher.calls, 1)


class ReviewApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def test_pending_delivery_is_not_regenerated_and_reject_returns_material_to_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = make_database(Path(temp_dir))
            material_id = seed_material(db)
            generated_id = save_generated(db, material_item_id=material_id)
            self.assertEqual(db.list_material_queue_for_account("writer"), [])

            with patch("bn_square_agent.webapp.get_db", return_value=db):
                response = self.client.post(
                    f"/api/review/items/{generated_id}/reject",
                    json={"comment": "方向证据不足"},
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                db.list_material_queue_for_account("writer")[0]["id"],
                material_id,
            )
            self.assertIsNone(db.get_material_item(material_id)["error"])

    def test_batch_approve_continues_after_one_item_fails(self) -> None:
        class FakeDatabase:
            def __init__(self) -> None:
                self.calls: list[int] = []

            def approve_generated_for_queue(self, generated_id, *, min_interval_minutes):
                self.calls.append(generated_id)
                if generated_id == 2:
                    raise ValueError("invalid item")
                return {"scheduled_at": f"2026-08-11T10:0{generated_id}:00+08:00"}

        db = FakeDatabase()
        settings = replace(Settings.from_env(), publish_min_interval_minutes=20)
        with patch("bn_square_agent.webapp.get_db", return_value=db), patch(
            "bn_square_agent.webapp.get_settings",
            return_value=settings,
        ):
            response = self.client.post(
                "/api/review/batch-approve",
                json={"generated_ids": [1, 2, 3]},
            )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["approved"], 2)
        self.assertEqual(payload["failed"], 1)
        self.assertEqual(db.calls, [1, 2, 3])

    def test_settings_api_preserves_zero_publish_interval(self) -> None:
        class FakeDatabase:
            def set_app_settings(self, values):
                self.values = values

        db = FakeDatabase()
        settings = Settings.from_env()
        with patch("bn_square_agent.webapp.get_db", return_value=db), patch(
            "bn_square_agent.webapp.get_settings",
            return_value=settings,
        ):
            response = self.client.post(
                "/api/settings",
                json={"publish_min_interval_minutes": 0},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(db.values["PUBLISH_MIN_INTERVAL_MINUTES"], "0")


class QueueWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_queue_exception_does_not_block_later_items(self) -> None:
        class FakeDatabase:
            def list_due_publish_queue(self, *, limit):
                self.limit = limit
                return [
                    {"id": 1, "account_key": "first"},
                    {"id": 2, "account_key": "second"},
                ]

        def publish_one(_services, generated_id):
            if generated_id == 1:
                raise RuntimeError("first failed")
            return {"generated_id": generated_id, "success": True}

        db = FakeDatabase()
        with patch("bn_square_agent.webapp._publish_queue_item", side_effect=publish_one):
            results = await _process_due_publish_queue(db, SimpleNamespace())
        self.assertEqual(db.limit, 50)
        self.assertEqual(results[0]["error"], "first failed")
        self.assertTrue(results[1]["success"])


class GeneratedPostMigrationTests(unittest.TestCase):
    def test_generated_post_status_migration_preserves_run_foreign_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = make_database(root)
            material_id = seed_material(db)
            generated_id = save_generated(
                db,
                status="approved",
                material_item_id=material_id,
            )
            db.save_material_account_run(
                material_id,
                account_key="writer",
                status="failed",
                generated_id=generated_id,
                error="明确失败",
            )

            with sqlite3.connect(db.path) as connection:
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute("PRAGMA legacy_alter_table = ON")
                connection.executescript(
                    """
                    ALTER TABLE material_account_runs RENAME TO material_account_runs_current;
                    ALTER TABLE generated_posts RENAME TO generated_posts_current;
                    CREATE TABLE generated_posts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source_post_id INTEGER NOT NULL,
                        candidate_index INTEGER NOT NULL,
                        original_content TEXT NOT NULL,
                        content TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(
                            status IN ('pending', 'approved', 'rejected', 'failed')
                        ),
                        review_json TEXT,
                        rewrite_count INTEGER NOT NULL DEFAULT 0,
                        account_key TEXT NOT NULL DEFAULT 'default',
                        material_item_id INTEGER,
                        publish_status TEXT NOT NULL DEFAULT 'not_published',
                        publish_json TEXT,
                        published_at TEXT,
                        approval_hash TEXT,
                        scheduled_at TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(source_post_id, candidate_index),
                        FOREIGN KEY(source_post_id) REFERENCES source_posts(id),
                        FOREIGN KEY(material_item_id) REFERENCES material_items(id)
                    );
                    INSERT INTO generated_posts SELECT * FROM generated_posts_current;
                    DROP TABLE generated_posts_current;
                    CREATE TABLE material_account_runs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        material_item_id INTEGER NOT NULL,
                        account_key TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(
                            status IN ('published', 'failed', 'skipped', 'unknown')
                        ),
                        generated_id INTEGER,
                        publish_json TEXT,
                        error TEXT,
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        last_attempted_at TEXT,
                        published_at TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(material_item_id, account_key),
                        FOREIGN KEY(material_item_id) REFERENCES material_items(id),
                        FOREIGN KEY(account_key) REFERENCES accounts(account_key),
                        FOREIGN KEY(generated_id) REFERENCES generated_posts(id)
                    );
                    INSERT INTO material_account_runs SELECT * FROM material_account_runs_current;
                    DROP TABLE material_account_runs_current;
                    """
                )
                connection.execute("PRAGMA legacy_alter_table = OFF")

            migrated = make_database(root)
            migrated.init_schema()
            with sqlite3.connect(migrated.path) as connection:
                connection.row_factory = sqlite3.Row
                foreign_keys = connection.execute(
                    "PRAGMA foreign_key_list(material_account_runs)"
                ).fetchall()
                violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            generated_target = next(
                row["table"] for row in foreign_keys if row["from"] == "generated_id"
            )
            self.assertEqual(generated_target, "generated_posts")
            self.assertEqual(violations, [])
            self.assertEqual(
                migrated.get_material_account_run(material_id, "writer")["generated_id"],
                generated_id,
            )


if __name__ == "__main__":
    unittest.main()
