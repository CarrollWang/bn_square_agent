from __future__ import annotations

from pathlib import Path
import re
import unittest
from unittest.mock import patch


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
