from __future__ import annotations

import base64
from pathlib import Path
import re
import unittest

from bn_square_agent.publishing.self_hosted_mcp import _tool_definition
from bn_square_agent.webapp import _basic_auth_matches


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


class McpBoundaryTests(unittest.TestCase):
    def test_tool_schema_uses_account_key_and_never_accepts_secrets(self) -> None:
        schema = _tool_definition()["inputSchema"]
        self.assertEqual(schema["required"], ["content", "account_key"])
        self.assertNotIn("cookie", schema["properties"])
        self.assertNotIn("square_openapi_key", schema["properties"])
        self.assertNotIn("proxy_url", schema["properties"])


if __name__ == "__main__":
    unittest.main()
