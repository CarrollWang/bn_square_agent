from __future__ import annotations

from pathlib import Path
import re
import unittest
from unittest.mock import patch

from bn_square_agent.publishing.account_check import BinanceAccountChecker
from bn_square_agent.webapp import _cookie_header_from_playwright_cookies


class FakePage:
    def __init__(self, result):
        self.result = result

    def evaluate(self, _script, _argument):
        return self.result


class WebApiBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            from fastapi.testclient import TestClient
            from bn_square_agent.webapp import app
        except ModuleNotFoundError as exc:
            raise unittest.SkipTest(f"runtime dependency is not installed: {exc}")
        cls.client = TestClient(app)

    def test_index_and_built_assets_are_served(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        index_html = (project_root / "dist" / "index.html").read_text(encoding="utf-8")
        asset_path = re.search(r'(?:src|href)="(/assets/[^"]+)', index_html)
        self.assertIsNotNone(asset_path)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get(asset_path.group(1)).status_code, 200)

    def test_basic_auth_is_optional_locally_and_enforced_when_configured(self) -> None:
        with patch.dict(
            "os.environ",
            {"WEB_AUTH_USERNAME": "admin", "WEB_AUTH_PASSWORD": "secret"},
        ):
            self.assertEqual(self.client.get("/").status_code, 401)
            self.assertEqual(
                self.client.get("/", auth=("admin", "secret")).status_code,
                200,
            )
            self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_material_source_rejects_untrusted_host(self) -> None:
        response = self.client.post(
            "/api/material-sources",
            json={
                "name": "bad",
                "url": "https://evil.example/news",
                "source_type": "techflow_newsletter",
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_cookie_export_filters_domains_and_deduplicates_names(self) -> None:
        cookies = [
            {"name": "p20t", "value": "global", "domain": ".binance.com", "path": "/"},
            {"name": "p20t", "value": "account", "domain": "accounts.binance.com", "path": "/"},
            {"name": "p20t", "value": "www", "domain": "www.binance.com", "path": "/"},
            {"name": "shared", "value": "yes", "domain": ".binance.com", "path": "/"},
            {"name": "wrong_path", "value": "no", "domain": ".binance.com", "path": "/zh-CN"},
            {"name": "other", "value": "no", "domain": ".example.com", "path": "/"},
        ]
        header = _cookie_header_from_playwright_cookies(cookies)
        self.assertIn("p20t=www", header)
        self.assertIn("shared=yes", header)
        self.assertNotIn("account", header)
        self.assertNotIn("wrong_path", header)
        self.assertEqual(header.count("p20t="), 1)

    def test_page_session_probe_accepts_private_square_identity(self) -> None:
        result = BinanceAccountChecker.probe_page_session(
            FakePage(
                {
                    "valid": True,
                    "signature_key": "square-user-1",
                    "source": "/bapi/composite/v3/private/pgc/user/client",
                    "attempts": [],
                }
            )
        )
        self.assertTrue(result.valid)
        self.assertEqual(result.signature_key, "square-user-1")

    def test_page_session_probe_preserves_login_error(self) -> None:
        result = BinanceAccountChecker.probe_page_session(
            FakePage(
                {
                    "valid": False,
                    "error": "Please login first",
                    "attempts": [],
                }
            )
        )
        self.assertFalse(result.valid)
        self.assertEqual(result.error, "Please login first")

    def test_account_headers_reuse_binance_csrf_cookie(self) -> None:
        headers = BinanceAccountChecker._headers("cr00=csrf-token; session=abc")
        self.assertEqual(headers["csrftoken"], "csrf-token")

    def test_cookie_login_session_cleanup_removes_temporary_profile(self) -> None:
        from bn_square_agent.webapp import (
            _close_cookie_login_session,
            _cookie_import_profile_dir,
        )

        class Closable:
            def close(self):
                return None

        class PlaywrightHandle:
            def stop(self):
                return None

        profile_dir = _cookie_import_profile_dir("cleanup-test-account")
        _close_cookie_login_session(
            {
                "context": Closable(),
                "browser": Closable(),
                "playwright": PlaywrightHandle(),
                "profile_dir": profile_dir,
            }
        )
        self.assertFalse(profile_dir.exists())


class McpBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            from fastapi.testclient import TestClient
            from bn_square_agent.publishing.self_hosted_mcp import app
        except ModuleNotFoundError as exc:
            raise unittest.SkipTest(f"runtime dependency is not installed: {exc}")
        cls.client = TestClient(app)

    def test_initialize_and_argument_validation(self) -> None:
        initialized = self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        self.assertEqual(initialized.status_code, 200)
        self.assertEqual(
            initialized.json()["result"]["protocolVersion"],
            "2025-06-18",
        )

        invalid = self.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "publish_binance_square",
                    "arguments": {"cookie": "x", "content": "y", "coins": "bad"},
                },
            },
        )
        self.assertEqual(invalid.status_code, 200)
        self.assertIn("coins 格式", invalid.json()["error"]["message"])


if __name__ == "__main__":
    unittest.main()
