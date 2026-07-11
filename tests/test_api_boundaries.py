from __future__ import annotations

from pathlib import Path
import re
import unittest
from unittest.mock import MagicMock, patch

from bn_square_agent.publishing.account_check import BinanceAccountChecker
from bn_square_agent.webapp import (
    _binance_login_status_from_page,
    _cookie_header_from_playwright_cookies,
    _cookie_import_launch_options,
)


class FakePage:
    def __init__(self, result):
        self.result = result

    def evaluate(self, _script, _argument):
        return self.result


class NavigatingFakePage:
    url = "https://www.binance.com/zh-CN/square"

    def __init__(self, evaluate_results):
        self.evaluate_results = iter(evaluate_results)
        self.wait_timeouts = []

    def goto(self, _url, **_kwargs):
        return None

    def wait_for_load_state(self, _state, **_kwargs):
        return None

    def wait_for_timeout(self, timeout):
        self.wait_timeouts.append(timeout)

    def evaluate(self, _script, _argument):
        result = next(self.evaluate_results)
        if isinstance(result, Exception):
            raise result
        return result


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

    def test_cookie_export_preserves_all_binance_cookies_in_browser_order(self) -> None:
        cookies = [
            {"name": "p20t", "value": "global", "domain": ".binance.com", "path": "/"},
            {"name": "p20t", "value": "account", "domain": "accounts.binance.com", "path": "/"},
            {"name": "p20t", "value": "www", "domain": "www.binance.com", "path": "/"},
            {"name": "shared", "value": "yes", "domain": ".binance.com", "path": "/"},
            {"name": "wrong_path", "value": "no", "domain": ".binance.com", "path": "/zh-CN"},
            {"name": "other", "value": "no", "domain": ".example.com", "path": "/"},
            {"name": "lookalike", "value": "no", "domain": ".notbinance.com", "path": "/"},
        ]
        header = _cookie_header_from_playwright_cookies(cookies)
        self.assertIn("p20t=global", header)
        self.assertIn("p20t=account", header)
        self.assertIn("p20t=www", header)
        self.assertIn("shared=yes", header)
        self.assertIn("wrong_path=no", header)
        self.assertNotIn("other=no", header)
        self.assertNotIn("lookalike=no", header)
        self.assertEqual(header.count("p20t="), 3)
        self.assertLess(header.index("p20t=global"), header.index("p20t=account"))
        self.assertLess(header.index("p20t=account"), header.index("p20t=www"))

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

    def test_login_status_retries_when_navigation_destroys_context(self) -> None:
        page = NavigatingFakePage(
            [
                RuntimeError(
                    "Page.evaluate: Execution context was destroyed, "
                    "most likely because of a navigation."
                ),
                {
                    "valid": True,
                    "signature_key": "square-user-1",
                    "attempts": [],
                },
            ]
        )

        valid, error = _binance_login_status_from_page(page)

        self.assertTrue(valid)
        self.assertIsNone(error)
        self.assertEqual(page.wait_timeouts, [500])

    def test_login_status_returns_retryable_error_after_navigation_retries(self) -> None:
        page = NavigatingFakePage(
            [RuntimeError("Cannot find context with specified id") for _ in range(5)]
        )

        valid, error = _binance_login_status_from_page(page)

        self.assertFalse(valid)
        self.assertIn("页面仍在跳转", error)
        self.assertEqual(page.wait_timeouts, [500, 1000, 1500, 2000])

    def test_account_headers_reuse_binance_csrf_cookie(self) -> None:
        headers = BinanceAccountChecker._headers("cr00=csrf-token; session=abc")
        self.assertEqual(headers["csrftoken"], "csrf-token")

    def test_cookie_import_launch_options_include_proxy(self) -> None:
        options = _cookie_import_launch_options("socks5://127.0.0.1:18789")
        self.assertEqual(
            options["proxy"],
            {"server": "socks5://127.0.0.1:18789"},
        )

    def test_cookie_import_finish_keeps_cookie_inside_mcp(self) -> None:
        from bn_square_agent.webapp import (
            AccountCookieImportFinishPayload,
            cookie_login_sessions,
            cookie_login_sessions_lock,
            finish_account_cookie_import,
        )

        session_id = "finish-without-probe"
        with cookie_login_sessions_lock:
            cookie_login_sessions[session_id] = {
                "account_key": "test-account",
                "name": "Test Account",
                "base_url": "http://127.0.0.1:8788",
                "auth_token": "test-token",
            }

        with patch(
            "bn_square_agent.webapp._mcp_control_request",
            return_value={
                "account_key": "test-account",
                "active": True,
                "ready": True,
                "valid": True,
                "cookie_length": 123,
                "cookie_names": ["session"],
            },
        ) as request:
            result = finish_account_cookie_import(
                AccountCookieImportFinishPayload(session_id=session_id)
            )

        self.assertTrue(result["ok"])
        self.assertNotIn("cookie", result)
        self.assertEqual(result["cookie_length"], 123)
        request.assert_called_once()
        with cookie_login_sessions_lock:
            self.assertNotIn(session_id, cookie_login_sessions)

    def test_cookie_import_is_disabled_without_a_graphical_session(self) -> None:
        from fastapi import HTTPException
        from bn_square_agent.webapp import (
            AccountCookieImportStartPayload,
            start_account_cookie_import,
        )

        reason = "当前服务运行在无图形界面的服务器上"
        with patch(
            "bn_square_agent.webapp._cookie_import_browser_capability",
            return_value=(False, reason),
        ):
            with self.assertRaises(HTTPException) as raised:
                start_account_cookie_import(
                    AccountCookieImportStartPayload(
                        account_key="server-account",
                        name="Server",
                    )
                )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail, reason)

    def test_cookie_import_is_available_when_self_hosted_mcp_is_configured(self) -> None:
        from bn_square_agent.webapp import _cookie_import_browser_capability

        settings = MagicMock(mcp_url="http://127.0.0.1:8788/mcp")
        with patch("bn_square_agent.webapp.get_settings", return_value=settings):
            available, reason = _cookie_import_browser_capability()

        self.assertTrue(available)
        self.assertEqual(reason, "")


class McpBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        try:
            import httpx
            from bn_square_agent.publishing.self_hosted_mcp import app
        except ModuleNotFoundError as exc:
            raise unittest.SkipTest(f"runtime dependency is not installed: {exc}")
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    async def test_initialize_and_argument_validation(self) -> None:
        initialized = await self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        self.assertEqual(initialized.status_code, 200)
        self.assertEqual(
            initialized.json()["result"]["protocolVersion"],
            "2025-06-18",
        )

        invalid = await self.client.post(
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
