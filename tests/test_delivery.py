from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import httpx

from bn_square_agent.core.config import AccountConfig, Settings
from bn_square_agent.core.delivery import (
    PUBLISH_FAILED_RETRYABLE,
    PUBLISH_PUBLISHED,
    PUBLISH_QUEUED,
    PUBLISH_UNKNOWN_MANUAL_RECOVERY,
    classify_publish_outcome,
    content_fingerprint,
)
from bn_square_agent.core.secret_store import SecretStore
from bn_square_agent.models.schemas import ContentReview, ReviewScores
from bn_square_agent.publishing.publisher import PublishingService
from bn_square_agent.storage.database import Database
from bn_square_agent.webapp import _update_publish_failure_guard, monitor_state


def make_review() -> ContentReview:
    return ContentReview(
        passed=True,
        scores=ReviewScores(
            factual_fidelity=10,
            style_match=8,
            originality=8,
            expression_quality=8,
        ),
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


def seed_delivery(db: Database) -> tuple[int, int, int]:
    db.upsert_account(account_key="writer", name="Writer", cookie="session=test")
    source_id = db.upsert_material_source(
        name="Source",
        source_type="techflow_newsletter",
        url="https://www.techflowpost.com/newsletter",
    )
    material_id, _ = db.add_material_item(
        source_id=source_id,
        content="$BTC 继续看多，关注回踩机会",
    )
    db.save_material_tag(
        material_id,
        tag_status="accepted",
        tag={"symbol": "BTCUSDT", "direction": "long", "strategy": "directional_v1"},
    )
    source_post_id, _ = db.add_source_post(
        account_key="writer",
        role="material",
        content="$BTC 继续看多，关注回踩机会",
    )
    generated_id = db.save_generated(
        source_post_id=source_post_id,
        candidate_index=1,
        original_content="$BTC 继续看多",
        content="$BTC 继续看多，关注回踩",
        status="approved",
        review=make_review(),
        rewrite_count=0,
        account_key="writer",
    )
    return source_id, material_id, generated_id


class DeliveryClassificationTests(unittest.TestCase):
    def test_classify_publish_outcome_three_states(self) -> None:
        self.assertEqual(
            classify_publish_outcome({"outcome": "published"}),
            PUBLISH_PUBLISHED,
        )
        self.assertEqual(
            classify_publish_outcome({"outcome": "failed"}),
            PUBLISH_FAILED_RETRYABLE,
        )
        self.assertEqual(
            classify_publish_outcome({"outcome": "unknown"}),
            PUBLISH_UNKNOWN_MANUAL_RECOVERY,
        )

    def test_unrecognized_response_fails_closed_to_unknown(self) -> None:
        self.assertEqual(
            classify_publish_outcome({"message": "request accepted"}),
            PUBLISH_UNKNOWN_MANUAL_RECOVERY,
        )

    def test_unknown_counts_toward_failure_guard_even_with_a_success(self) -> None:
        settings = replace(Settings.from_env(), publish_failure_alert_threshold=99)
        monitor_state["consecutive_publish_failures"] = 8
        _update_publish_failure_guard(
            settings,
            object(),
            [
                {
                    "runs": [
                        {"status": "published", "publish_success": True, "error": None},
                        {
                            "status": "unknown",
                            "publish_success": False,
                            "error": "response timed out",
                        },
                    ]
                }
            ],
        )
        self.assertEqual(monitor_state["consecutive_publish_failures"], 1)


class PublishingServiceTests(unittest.TestCase):
    def test_transport_error_is_saved_as_unknown(self) -> None:
        class FakeDatabase:
            def get_generated(self, generated_id):
                content = "$BTC 继续看多"
                return {
                    "id": generated_id,
                    "account_key": "writer",
                    "status": "approved",
                    "publish_status": "not_published",
                    "content": content,
                    "approval_hash": content_fingerprint("writer", content),
                }

            def mark_published(self, generated_id, *, result, publish_status):
                self.saved = (generated_id, result, publish_status)

        class FakePublisher:
            def publish(self, **_kwargs):
                raise httpx.ReadTimeout("response timed out")

        db = FakeDatabase()
        result = PublishingService(db, FakePublisher()).publish_generated(
            account=AccountConfig(key="writer", name="Writer", cookie="session=test"),
            generated_id=7,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.publish_status, PUBLISH_UNKNOWN_MANUAL_RECOVERY)
        self.assertEqual(db.saved[2], PUBLISH_UNKNOWN_MANUAL_RECOVERY)

    def test_unknown_terminal_state_is_idempotent_and_not_republished(self) -> None:
        class FakeDatabase:
            def get_generated(self, generated_id):
                return {
                    "id": generated_id,
                    "account_key": "writer",
                    "status": "approved",
                    "publish_status": PUBLISH_UNKNOWN_MANUAL_RECOVERY,
                    "publish_json": json.dumps({"outcome": "unknown"}),
                }

        class FakePublisher:
            def __init__(self):
                self.calls = 0

            def publish(self, **_kwargs):
                self.calls += 1
                return {"success": True}

        publisher = FakePublisher()
        result = PublishingService(FakeDatabase(), publisher).publish_generated(
            account=AccountConfig(key="writer", name="Writer", cookie="session=test"),
            generated_id=7,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.publish_status, PUBLISH_UNKNOWN_MANUAL_RECOVERY)
        self.assertEqual(publisher.calls, 0)

    def test_queued_terminal_state_is_not_published_by_normal_call(self) -> None:
        class FakeDatabase:
            def get_generated(self, generated_id):
                return {
                    "id": generated_id,
                    "account_key": "writer",
                    "status": "approved",
                    "publish_status": PUBLISH_QUEUED,
                    "publish_json": None,
                }

        class FakePublisher:
            def __init__(self):
                self.calls = 0

            def publish(self, **_kwargs):
                self.calls += 1
                return {"success": True}

        publisher = FakePublisher()
        result = PublishingService(FakeDatabase(), publisher).publish_generated(
            account=AccountConfig(key="writer", name="Writer", cookie="session=test"),
            generated_id=8,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.publish_status, PUBLISH_QUEUED)
        self.assertEqual(publisher.calls, 0)


class DeliveryDatabaseTests(unittest.TestCase):
    def test_unknown_run_is_excluded_from_automatic_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = make_database(Path(temp_dir))
            _, material_id, generated_id = seed_delivery(db)
            db.save_material_account_run(
                material_id,
                account_key="writer",
                status="unknown",
                generated_id=generated_id,
                publish_result={"outcome": "unknown"},
                error="response timed out",
                increment_attempts=True,
            )
            self.assertEqual(db.list_material_queue_for_account("writer"), [])

    def test_manual_resolution_updates_run_and_generated_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = make_database(Path(temp_dir))
            _, material_id, generated_id = seed_delivery(db)
            db.mark_published(
                generated_id,
                result={"outcome": "unknown", "error": "response timed out"},
                publish_status=PUBLISH_UNKNOWN_MANUAL_RECOVERY,
            )
            db.save_material_account_run(
                material_id,
                account_key="writer",
                status="unknown",
                generated_id=generated_id,
                publish_result={"outcome": "unknown", "error": "response timed out"},
                error="response timed out",
                increment_attempts=True,
            )
            run_id = db.get_material_account_run(material_id, "writer")["id"]

            resolved = db.resolve_unknown_material_run(run_id, resolution="published")
            self.assertTrue(resolved["changed"])
            self.assertEqual(
                db.get_material_account_run(material_id, "writer")["status"],
                "published",
            )
            self.assertEqual(db.get_generated(generated_id)["publish_status"], PUBLISH_PUBLISHED)

            replayed = db.resolve_unknown_material_run(run_id, resolution="published")
            self.assertFalse(replayed["changed"])

    def test_legacy_unknown_prefix_migration_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = make_database(root)
            _, material_id, generated_id = seed_delivery(db)
            db.save_material_account_run(
                material_id,
                account_key="writer",
                status="failed",
                generated_id=generated_id,
                publish_result={"outcome": "unknown"},
                error="publish_outcome_unknown: response timed out",
                increment_attempts=True,
            )
            with sqlite3.connect(db.path) as connection:
                connection.execute(
                    "UPDATE generated_posts SET publish_status = 'publish_failed', publish_json = ? WHERE id = ?",
                    (json.dumps({"outcome": "unknown"}), generated_id),
                )
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.executescript(
                    """
                    ALTER TABLE material_account_runs RENAME TO material_account_runs_modern;
                    CREATE TABLE material_account_runs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        material_item_id INTEGER NOT NULL,
                        account_key TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('published', 'failed', 'skipped')),
                        generated_id INTEGER,
                        publish_json TEXT,
                        error TEXT,
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        last_attempted_at TEXT,
                        published_at TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(material_item_id, account_key)
                    );
                    INSERT INTO material_account_runs SELECT * FROM material_account_runs_modern;
                    DROP TABLE material_account_runs_modern;
                    """
                )

            migrated = make_database(root)
            run = migrated.get_material_account_run(material_id, "writer")
            self.assertEqual(run["status"], "unknown")
            self.assertEqual(run["error"], "response timed out")
            self.assertEqual(
                migrated.get_generated(generated_id)["publish_status"],
                PUBLISH_UNKNOWN_MANUAL_RECOVERY,
            )

            migrated.init_schema()
            self.assertEqual(
                migrated.get_material_account_run(material_id, "writer")["status"],
                "unknown",
            )

    def test_manual_failed_resolution_allows_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = make_database(Path(temp_dir))
            _, material_id, generated_id = seed_delivery(db)
            db.mark_published(
                generated_id,
                result={"outcome": "unknown"},
                publish_status=PUBLISH_UNKNOWN_MANUAL_RECOVERY,
            )
            db.save_material_account_run(
                material_id,
                account_key="writer",
                status="unknown",
                generated_id=generated_id,
                publish_result={"outcome": "unknown"},
                error="response timed out",
                increment_attempts=True,
            )
            run_id = db.get_material_account_run(material_id, "writer")["id"]
            db.resolve_unknown_material_run(run_id, resolution="failed")

            self.assertEqual(
                db.get_material_account_run(material_id, "writer")["status"],
                "failed",
            )
            self.assertEqual(
                db.get_generated(generated_id)["publish_status"],
                PUBLISH_FAILED_RETRYABLE,
            )
            self.assertEqual(
                db.list_material_queue_for_account("writer")[0]["id"],
                material_id,
            )


if __name__ == "__main__":
    unittest.main()
