from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from bn_square_agent.core.config import AccountConfig, Settings
from bn_square_agent.core.secret_store import SecretStore
from bn_square_agent.publishing.binance_square_openapi import (
    BinanceSquareOpenAPIClient,
)
from bn_square_agent.storage.database import Database


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


if __name__ == "__main__":
    unittest.main()
