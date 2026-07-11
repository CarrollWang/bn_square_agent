from __future__ import annotations

import base64
from pathlib import Path
import re
import unittest

from bn_square_agent.publishing.self_hosted_mcp import _tool_definition
from bn_square_agent.webapp import (
    _basic_auth_matches,
    _consume_results_failure_count,
    _consume_results_have_failure,
    _next_monitor_delay,
    publish_evidence,
)


class WebBoundaryTests(unittest.TestCase):
    def test_index_references_existing_built_assets(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        index_html = (project_root / "dist" / "index.html").read_text(encoding="utf-8")
        asset_paths = re.findall(r'(?:src|href)="(/assets/[^"]+)', index_html)
        self.assertTrue(asset_paths)
        for asset_path in asset_paths:
            self.assertTrue(
                (project_root / "dist" / "assets" / asset_path.removeprefix("/assets/")).is_file()
            )

    def test_basic_auth_match_is_exact(self) -> None:
        token = base64.b64encode(b"admin:secret").decode("ascii")
        self.assertTrue(_basic_auth_matches(f"Basic {token}", "admin", "secret"))
        self.assertFalse(_basic_auth_matches(f"Basic {token}", "admin", "wrong"))

    def test_publish_history_extracts_canonical_post_evidence(self) -> None:
        post_id, post_url = publish_evidence(
            {
                "structuredContent": {
                    "post_id": "343615300021041",
                    "post_url": "https://app.binance.com/uni-qr/cpos/343615300021041",
                }
            }
        )
        self.assertEqual(post_id, "343615300021041")
        self.assertEqual(
            post_url,
            "https://www.binance.com/zh-CN/square/post/343615300021041",
        )

    def test_rate_limit_is_not_counted_as_publish_failure(self) -> None:
        consume_results = [
            {
                "runs": [
                    {
                        "error": "publish_rate_limited: hourly limit",
                        "publish_success": False,
                        "publish_result": {"outcome": "rate_limited"},
                    }
                ]
            }
        ]
        self.assertFalse(_consume_results_have_failure(consume_results))
        self.assertEqual(_consume_results_failure_count(consume_results), 0)

        settings = type(
            "SettingsStub",
            (),
            {
                "material_failure_interval_seconds": 900,
                "material_success_interval_seconds": 7200,
                "material_poll_interval_seconds": 900,
            },
        )()
        self.assertEqual(
            _next_monitor_delay(
                settings,
                {"consume_results": consume_results, "results": []},
            ),
            (3600, "rate_limited"),
        )


class McpBoundaryTests(unittest.TestCase):
    def test_tool_schema_uses_account_key_and_never_accepts_secrets(self) -> None:
        schema = _tool_definition()["inputSchema"]
        self.assertEqual(schema["required"], ["content", "account_key"])
        self.assertNotIn("cookie", schema["properties"])
        self.assertNotIn("square_openapi_key", schema["properties"])
        self.assertNotIn("proxy_url", schema["properties"])


if __name__ == "__main__":
    unittest.main()
