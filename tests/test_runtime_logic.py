from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest


class BrowserPublisherLogicTests(unittest.TestCase):
    def test_coins_parameter_adds_missing_cashtag(self) -> None:
        try:
            from bn_square_agent.publishing.browser_square_mcp_publisher import (
                BrowserBinanceSquarePublisher,
            )
        except ModuleNotFoundError as exc:
            self.skipTest(f"runtime dependency is not installed: {exc}")

        content = BrowserBinanceSquarePublisher._ensure_coin_reference(
            "继续关注回踩位置",
            "BTC:future",
        )
        self.assertTrue(content.endswith("$BTC"))
        self.assertEqual(
            BrowserBinanceSquarePublisher._ensure_coin_reference(
                "$BTC 继续关注回踩位置",
                "BTC:future",
            ),
            "$BTC 继续关注回踩位置",
        )


class SourceRuntimeTests(unittest.TestCase):
    def test_techflow_redirect_cannot_escape_allowed_domain(self) -> None:
        try:
            import httpx
            from bn_square_agent.sources.techflow import TechFlowNewsletterMonitor
        except ModuleNotFoundError as exc:
            self.skipTest(f"runtime dependency is not installed: {exc}")

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(ValueError):
                TechFlowNewsletterMonitor._get_with_safe_redirects(
                    client,
                    "https://www.techflowpost.com/newsletter",
                )


class DatabaseRuntimeTests(unittest.TestCase):
    def test_database_instance_is_reused_and_secrets_remain_encrypted(self) -> None:
        try:
            from bn_square_agent.core.config import Settings
        except ModuleNotFoundError as exc:
            self.skipTest(f"runtime dependency is not installed: {exc}")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = replace(
                Settings.from_env(),
                app_secret_key="",
                database_path=root / "agent.sqlite3",
                secret_key_path=root / "secret.key",
                chroma_path=root / "chroma",
            )
            first = settings.build_database()
            second = settings.build_database()
            self.assertIs(first, second)

            first.upsert_account(
                account_key="test",
                name="Test",
                cookie="session=secret-cookie",
            )
            with sqlite3.connect(settings.database_path) as connection:
                stored = connection.execute(
                    "SELECT cookie FROM accounts WHERE account_key = 'test'"
                ).fetchone()[0]
            self.assertTrue(stored.startswith("enc:v1:"))
            self.assertEqual(first.list_accounts()[0]["cookie"], "session=secret-cookie")

            source_id = first.upsert_material_source(
                name="Source",
                source_type="techflow_newsletter",
                url="https://www.techflowpost.com/newsletter",
            )
            material_id, _ = first.add_material_item(
                source_id=source_id,
                content="$BTC 继续看多，关注回踩机会",
            )
            first.save_material_tag(
                material_id,
                tag_status="accepted",
                tag={"symbol": "BTCUSDT", "direction": "unknown"},
            )
            self.assertEqual(first.list_material_queue_for_account("test"), [])
            self.assertEqual(
                first.pending_material_items_for_tagging(
                    strategy="directional_v1",
                )[0]["id"],
                material_id,
            )
            first.save_material_tag(
                material_id,
                tag_status="accepted",
                tag={
                    "symbol": "BTCUSDT",
                    "direction": "long",
                    "strategy": "directional_v1",
                },
            )
            first.save_material_account_run(
                material_id,
                account_key="test",
                status="failed",
                error="publish_outcome_unknown: request timed out",
                increment_attempts=True,
            )
            self.assertEqual(first.list_material_queue_for_account("test"), [])

            self.assertTrue(
                first.try_acquire_job_lock(
                    "test-job",
                    owner_id="owner",
                    lease_seconds=30,
                )
            )
            self.assertTrue(
                first.renew_job_lock(
                    "test-job",
                    owner_id="owner",
                    lease_seconds=60,
                )
            )
            first.release_job_lock("test-job", owner_id="owner")


if __name__ == "__main__":
    unittest.main()
