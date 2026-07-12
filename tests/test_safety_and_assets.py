from __future__ import annotations

from pathlib import Path
import re
import unittest

from bn_square_agent.ai.binance_symbols import (
    BinanceFuturesSymbolCatalog,
    BinanceSpotAssetCatalog,
)
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

    def test_accepts_crypto_news_without_inventing_direction(self) -> None:
        tag = MaterialTagger().tag(
            title="BitFuFu 6 月产出 125 枚 BTC",
            content="公司公布最新运营数据和比特币持仓量。",
        )
        self.assertTrue(tag.accepted)
        self.assertEqual(tag.symbol, "BTCUSDT")
        self.assertEqual(tag.direction, "unknown")
        self.assertIn("direction_not_explicit", tag.reasons)
        self.assertIn("crypto", tag.topics)

    def test_supports_direction_variants_without_requiring_direction(self) -> None:
        short_tag = MaterialTagger().tag(
            title="ETH 偏空",
            content="反弹做空，继续关注上方压力。",
        )
        self.assertTrue(short_tag.accepted)
        self.assertEqual(short_tag.direction, "short")

        conflict_tag = MaterialTagger().tag(
            title="BTC 多空都有机会",
            content="多头等待关键位置突破，空头关注支撑跌破，目前行情还没有选择明确方向。",
        )
        self.assertTrue(conflict_tag.accepted)
        self.assertEqual(conflict_tag.direction, "unknown")
        self.assertIn("conflicting_direction", conflict_tag.reasons)

    def test_accepts_ai_news_and_rejects_unrelated_market_news(self) -> None:
        ai_tag = MaterialTagger().tag(
            title="a16z 联创加入美联储 AI 工作组",
            content="工作组将研究人工智能对生产力与就业的影响。",
        )
        self.assertTrue(ai_tag.accepted)
        self.assertIn("ai", ai_tag.topics)
        self.assertIsNone(ai_tag.symbol)

        unrelated = MaterialTagger().tag(
            title="交易员加仓标普 500 空单",
            content="该仓位累计亏损达到 2200 万美元，市场仍在观察后续变化。",
        )
        self.assertFalse(unrelated.accepted)
        self.assertIn("missing_relevant_topic", unrelated.reasons)

    def test_uses_the_asset_from_each_news_item(self) -> None:
        cases = (
            ("以太坊升级临近", "Ethereum 开发者公布升级计划。", "ETHUSDT"),
            ("Solana 链上活跃回升", "SOL 生态交易量有所增加。", "SOLUSDT"),
            (
                "BNB Chain 公布路线图",
                "新路线图聚焦网络性能、验证节点效率和开发者工具升级。",
                "BNBUSDT",
            ),
        )
        for title, content, expected in cases:
            with self.subTest(title=title):
                tag = MaterialTagger().tag(title=title, content=content)
                self.assertTrue(tag.accepted)
                self.assertEqual(tag.symbol, expected)

    def test_recognizes_bare_lab_only_when_binance_lists_the_contract(self) -> None:
        tag = MaterialTagger(
            valid_futures_symbols={"LABUSDT", "BTCUSDT"},
        ).tag(
            title="LAB 庄家向 Aster 转移 1850 万枚 LAB",
            content="市场同时关注比特币走势，但本条核心交易对象是 LAB。",
        )
        self.assertTrue(tag.accepted)
        self.assertEqual(tag.token, "LAB")
        self.assertEqual(tag.symbol, "LABUSDT")

    def test_rejects_uppercase_words_that_are_not_listed_contracts(self) -> None:
        tag = MaterialTagger(
            valid_futures_symbols={"BTCUSDT", "ETHUSDT"},
        ).tag(
            title="AI API 与 ETF 行业进展",
            content="新的人工智能接口和行业基金研究已经公布。",
        )
        self.assertTrue(tag.accepted)
        self.assertIsNone(tag.symbol)

    def test_rejects_explicit_but_unlisted_symbol(self) -> None:
        tag = MaterialTagger(
            valid_futures_symbols={"BTCUSDT"},
        ).tag(
            title="$FAKE 项目更新",
            content="该项目发布了新的加密市场路线图和产品计划。",
        )
        self.assertTrue(tag.accepted)
        self.assertIsNone(tag.symbol)


class BinanceFuturesSymbolCatalogTests(unittest.TestCase):
    def test_parses_only_trading_usdt_perpetual_symbols(self) -> None:
        symbols = BinanceFuturesSymbolCatalog.parse(
            {
                "symbols": [
                    {
                        "symbol": "LABUSDT",
                        "status": "TRADING",
                        "contractType": "PERPETUAL",
                        "quoteAsset": "USDT",
                    },
                    {
                        "symbol": "OLDUSDT",
                        "status": "SETTLING",
                        "contractType": "PERPETUAL",
                        "quoteAsset": "USDT",
                    },
                    {
                        "symbol": "BTCUSDC",
                        "status": "TRADING",
                        "contractType": "PERPETUAL",
                        "quoteAsset": "USDC",
                    },
                ]
            }
        )
        self.assertEqual(symbols, frozenset({"LABUSDT"}))


class BinanceSpotAssetCatalogTests(unittest.TestCase):
    def test_parses_trading_spot_assets_across_supported_quotes(self) -> None:
        assets = BinanceSpotAssetCatalog.parse(
            {
                "symbols": [
                    {
                        "symbol": "SOLUSDT",
                        "status": "TRADING",
                        "baseAsset": "SOL",
                        "quoteAsset": "USDT",
                        "isSpotTradingAllowed": True,
                    },
                    {
                        "symbol": "USDCUSDT",
                        "status": "TRADING",
                        "baseAsset": "USDC",
                        "quoteAsset": "USDT",
                        "isSpotTradingAllowed": True,
                    },
                    {
                        "symbol": "BNBFDUSD",
                        "status": "TRADING",
                        "baseAsset": "BNB",
                        "quoteAsset": "FDUSD",
                        "isSpotTradingAllowed": True,
                    },
                    {
                        "symbol": "OLDUSDT",
                        "status": "BREAK",
                        "baseAsset": "OLD",
                        "quoteAsset": "USDT",
                        "isSpotTradingAllowed": True,
                    },
                    {
                        "symbol": "NOSPOTUSDT",
                        "status": "TRADING",
                        "baseAsset": "NOSPOT",
                        "quoteAsset": "USDT",
                        "isSpotTradingAllowed": False,
                    },
                ]
            }
        )
        self.assertEqual(assets, frozenset({"SOL", "USDC", "BNB"}))


class StaticAssetTests(unittest.TestCase):
    def test_vite_asset_prefix_is_mounted_by_fastapi(self) -> None:
        index_html = (PROJECT_ROOT / "dist" / "index.html").read_text(encoding="utf-8")
        webapp_source = (PROJECT_ROOT / "webapp.py").read_text(encoding="utf-8")
        asset_prefixes = set(re.findall(r'(?:src|href)="(/[^/]+/)', index_html))
        self.assertIn("/assets/", asset_prefixes)
        self.assertIn('app.mount("/assets"', webapp_source)


if __name__ == "__main__":
    unittest.main()
