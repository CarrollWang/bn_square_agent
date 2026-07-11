from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from bn_square_agent.publishing.account_check import AccountCheckResult
from bn_square_agent.publishing.live_browser_session import (
    LiveBrowserSession,
    LiveBrowserSessionManager,
)
from bn_square_agent.publishing.self_hosted_mcp import _publish_with_live_browser


class FakePage:
    url = "https://www.binance.com/zh-CN/square"

    def __init__(self) -> None:
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed


class FakeContext:
    def __init__(self) -> None:
        self.closed = False

    def cookies(self, _urls=None):
        return [
            {
                "name": "session",
                "value": "secret-value",
                "domain": ".binance.com",
            }
        ]

    def close(self) -> None:
        self.closed = True


class LiveBrowserSessionTests(unittest.TestCase):
    def make_manager(self):
        manager = LiveBrowserSessionManager(headless=False)
        context = FakeContext()
        page = FakePage()
        playwright = MagicMock()
        session = LiveBrowserSession(
            session_id="session-1",
            account_key="main",
            name="Main",
            proxy_url="",
            profile_dir=Path("profile"),
            playwright=playwright,
            context=context,
            page=page,
            created_at="2026-07-11T00:00:00+00:00",
        )
        manager._sessions["main"] = session
        return manager, session, context, playwright

    def test_finish_marks_ready_without_closing_browser(self) -> None:
        manager, session, context, playwright = self.make_manager()
        with patch.object(
            manager,
            "_probe_with_retries",
            return_value=AccountCheckResult(valid=True, signature_key="square-1"),
        ):
            result = manager.finish_login("session-1")

        self.assertTrue(result["valid"])
        self.assertTrue(result["active"])
        self.assertTrue(session.ready)
        self.assertFalse(context.closed)
        playwright.stop.assert_not_called()

    def test_close_explicitly_stops_browser(self) -> None:
        manager, _session, context, playwright = self.make_manager()

        self.assertTrue(manager.close(account_key="main"))
        self.assertTrue(context.closed)
        playwright.stop.assert_called_once()

    def test_mcp_publish_prefers_live_page_over_cookie_relaunch(self) -> None:
        manager, session, _context, _playwright = self.make_manager()
        session.ready = True
        settings = MagicMock()
        publisher = MagicMock()
        publisher.publish_in_page.return_value = MagicMock(success=True)

        with patch(
            "bn_square_agent.publishing.self_hosted_mcp._get_browser_session_manager",
            return_value=manager,
        ):
            result = _publish_with_live_browser(
                settings,
                publisher,
                "legacy-cookie",
                "hello",
                "main",
                "",
                "",
                "",
            )

        self.assertTrue(result.success)
        publisher.publish_in_page.assert_called_once()
        publisher.publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
