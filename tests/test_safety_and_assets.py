from __future__ import annotations

from pathlib import Path
import re
import unittest

from bn_square_agent.ai.material_tagger import MaterialTagger
from bn_square_agent.core.url_policy import (
    validate_binance_url,
    validate_techflow_url,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class UrlPolicyTests(unittest.TestCase):
    def test_official_https_urls_are_allowed(self) -> None:
        self.assertEqual(
            validate_binance_url("https://www.binance.com/zh-CN/login"),
            "https://www.binance.com/zh-CN/login",
        )
        self.assertEqual(
            validate_techflow_url("https://www.techflowpost.com/newsletter?is_hot=1"),
            "https://www.techflowpost.com/newsletter?is_hot=1",
        )

    def test_untrusted_or_insecure_urls_are_rejected(self) -> None:
        for value in (
            "http://www.binance.com/zh-CN/login",
            "https://binance.com.evil.example/login",
            "https://user:pass@www.binance.com/login",
            "file:///etc/passwd",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_binance_url(value)


class MaterialTaggerTests(unittest.TestCase):
    def test_extracts_symbol_and_direction(self) -> None:
        tag = MaterialTagger().tag(title="$BTC 继续看多", content="关注回踩机会")
        self.assertTrue(tag.accepted)
        self.assertEqual(tag.symbol, "BTCUSDT")
        self.assertEqual(tag.direction, "long")


class StaticAssetTests(unittest.TestCase):
    def test_vite_asset_prefix_is_mounted_by_fastapi(self) -> None:
        index_html = (PROJECT_ROOT / "dist" / "index.html").read_text(encoding="utf-8")
        webapp_source = (PROJECT_ROOT / "webapp.py").read_text(encoding="utf-8")
        asset_prefixes = set(re.findall(r'(?:src|href)="(/[^/]+/)', index_html))
        self.assertIn("/assets/", asset_prefixes)
        self.assertIn('app.mount("/assets"', webapp_source)


if __name__ == "__main__":
    unittest.main()
