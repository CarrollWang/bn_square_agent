from __future__ import annotations

import threading
import time

import httpx


FUTURES_EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
SPOT_EXCHANGE_INFO_URL = "https://api.binance.com/api/v3/exchangeInfo"
SPOT_QUOTE_ASSETS = frozenset({"USDT", "USDC", "FDUSD"})


class BinanceFuturesSymbolCatalog:
    def __init__(self, *, ttl_seconds: int = 6 * 60 * 60, timeout_seconds: int = 20):
        self.ttl_seconds = ttl_seconds
        self.timeout_seconds = timeout_seconds
        self._symbols: frozenset[str] = frozenset()
        self._expires_at = 0.0
        self._lock = threading.Lock()

    @staticmethod
    def parse(payload: object) -> frozenset[str]:
        if not isinstance(payload, dict):
            raise ValueError("Binance 合约目录返回格式错误")
        rows = payload.get("symbols")
        if not isinstance(rows, list):
            raise ValueError("Binance 合约目录缺少 symbols")
        symbols = {
            str(row.get("symbol") or "").upper()
            for row in rows
            if isinstance(row, dict)
            and str(row.get("status") or "").upper() == "TRADING"
            and str(row.get("contractType") or "").upper() == "PERPETUAL"
            and str(row.get("quoteAsset") or "").upper() == "USDT"
        }
        symbols = {symbol for symbol in symbols if symbol.endswith("USDT")}
        if not symbols:
            raise ValueError("Binance 合约目录没有可用 USDT 永续合约")
        return frozenset(symbols)

    def get(self) -> frozenset[str]:
        now = time.monotonic()
        if self._symbols and now < self._expires_at:
            return self._symbols
        with self._lock:
            now = time.monotonic()
            if self._symbols and now < self._expires_at:
                return self._symbols
            try:
                with httpx.Client(timeout=self.timeout_seconds, trust_env=False) as client:
                    response = client.get(FUTURES_EXCHANGE_INFO_URL)
                    response.raise_for_status()
                    symbols = self.parse(response.json())
            except Exception as exc:
                if self._symbols:
                    return self._symbols
                raise RuntimeError(f"无法获取 Binance 合约交易对目录: {exc}") from exc
            self._symbols = symbols
            self._expires_at = now + self.ttl_seconds
            return symbols


futures_symbol_catalog = BinanceFuturesSymbolCatalog()


class BinanceSpotAssetCatalog:
    def __init__(self, *, ttl_seconds: int = 6 * 60 * 60, timeout_seconds: int = 20):
        self.ttl_seconds = ttl_seconds
        self.timeout_seconds = timeout_seconds
        self._assets: frozenset[str] = frozenset()
        self._expires_at = 0.0
        self._lock = threading.Lock()

    @staticmethod
    def parse(payload: object) -> frozenset[str]:
        if not isinstance(payload, dict):
            raise ValueError("Binance 现货目录返回格式错误")
        rows = payload.get("symbols")
        if not isinstance(rows, list):
            raise ValueError("Binance 现货目录缺少 symbols")
        assets = {
            str(row.get("baseAsset") or "").upper()
            for row in rows
            if isinstance(row, dict)
            and str(row.get("status") or "").upper() == "TRADING"
            and str(row.get("quoteAsset") or "").upper() in SPOT_QUOTE_ASSETS
            and row.get("isSpotTradingAllowed") is not False
        }
        assets = {asset for asset in assets if asset}
        if not assets:
            raise ValueError("Binance 现货目录没有可用币种")
        return frozenset(assets)

    def get(self) -> frozenset[str]:
        now = time.monotonic()
        if self._assets and now < self._expires_at:
            return self._assets
        with self._lock:
            now = time.monotonic()
            if self._assets and now < self._expires_at:
                return self._assets
            try:
                with httpx.Client(timeout=self.timeout_seconds, trust_env=False) as client:
                    response = client.get(SPOT_EXCHANGE_INFO_URL)
                    response.raise_for_status()
                    assets = self.parse(response.json())
            except Exception as exc:
                if self._assets:
                    return self._assets
                raise RuntimeError(f"无法获取 Binance 现货币种目录: {exc}") from exc
            self._assets = assets
            self._expires_at = now + self.ttl_seconds
            return assets


spot_asset_catalog = BinanceSpotAssetCatalog()
