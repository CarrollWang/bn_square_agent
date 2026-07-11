from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch
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

    def test_account_profile_path_is_stable_and_isolated(self) -> None:
        from bn_square_agent.publishing.browser_profile import browser_profile_dir

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict("os.environ", {"BINANCE_PROFILE_ROOT": temp_dir}):
                first = browser_profile_dir("main")
                second = browser_profile_dir("main")
                other = browser_profile_dir("secondary")

        self.assertEqual(first, second)
        self.assertNotEqual(first, other)

    def test_mcp_publisher_passes_account_key(self) -> None:
        from bn_square_agent.core.config import AccountConfig, Settings
        from bn_square_agent.publishing.publisher import MCPPublisher

        settings = replace(
            Settings.from_env(),
            auto_publish=True,
            mcp_url="http://127.0.0.1:8788/mcp",
            mcp_publish_tool="publish_binance_square",
        )
        publisher = MCPPublisher(settings)
        client = MagicMock()
        client.call_tool.return_value = {"success": True}
        with (
            patch.object(publisher, "_client_for_account", return_value=client),
            patch.object(publisher.chart_images, "image_for_text", return_value=None),
        ):
            publisher.publish(
                account=AccountConfig(key="main", name="Main", cookie="p20t=x"),
                generated={"content": "测试内容"},
            )

        arguments = client.call_tool.call_args.args[1]
        self.assertEqual(arguments["account_key"], "main")

    def test_mcp_tool_accepts_account_key(self) -> None:
        from bn_square_agent.publishing.self_hosted_mcp import _tool_definition

        schema = _tool_definition()["inputSchema"]
        self.assertIn("account_key", schema["properties"])


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
            first.update_account_check(
                "test",
                signature_key="old-signature",
                status="invalid",
                error="expired",
            )
            first.upsert_account(
                account_key="test",
                name="Test",
                cookie="session=fresh-cookie",
            )
            refreshed = first.list_accounts()[0]
            self.assertEqual(refreshed["check_status"], "unchecked")
            self.assertIsNone(refreshed["checked_at"])
            self.assertIsNone(refreshed["check_error"])
            self.assertIsNone(refreshed["signature_key"])
            with sqlite3.connect(settings.database_path) as connection:
                stored = connection.execute(
                    "SELECT cookie FROM accounts WHERE account_key = 'test'"
                ).fetchone()[0]
            self.assertTrue(stored.startswith("enc:v1:"))
            self.assertEqual(first.list_accounts()[0]["cookie"], "session=fresh-cookie")

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
