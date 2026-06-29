from __future__ import annotations

from dataclasses import dataclass
import base64
import re
from typing import Literal


MarketKind = Literal["future", "spot"]


@dataclass(frozen=True)
class ChartTarget:
    symbol: str
    market: MarketKind


class ChartImageService:
    def __init__(self, *, timeout_ms: int = 60_000):
        self.timeout_ms = timeout_ms

    @staticmethod
    def extract_target(text: str) -> ChartTarget | None:
        explicit_future = re.search(
            r"\{future\}\(([A-Z0-9]{2,30}USDT)\)",
            text,
            re.IGNORECASE,
        )
        if explicit_future:
            return ChartTarget(explicit_future.group(1).upper(), "future")

        symbol_pair = re.search(r"\b([A-Z0-9]{2,30}USDT)\b", text)
        if symbol_pair:
            return ChartTarget(symbol_pair.group(1).upper(), "future")

        cashtag = re.search(r"\$([A-Z][A-Z0-9]{0,14})\b", text)
        if cashtag:
            token = cashtag.group(1).upper()
            if token not in {"USD", "USDT"}:
                return ChartTarget(f"{token}USDT", "future")
        return None

    @staticmethod
    def chart_url(target: ChartTarget) -> str:
        if target.market == "future":
            return f"https://www.binance.com/zh-CN/futures/{target.symbol}"
        base = target.symbol.removesuffix("USDT")
        return f"https://www.binance.com/zh-CN/trade/{base}_USDT"

    def image_for_text(self, text: str) -> str | None:
        target = self.extract_target(text)
        if not target:
            return None
        return self.capture_chart(target)

    def capture_chart(self, target: ChartTarget) -> str | None:
        from playwright.sync_api import sync_playwright

        url = self.chart_url(target)
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                viewport={"width": 1280, "height": 720},
                locale="zh-CN",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            self._dismiss_popups(page)
            if not self._wait_for_chart_pixels(page):
                browser.close()
                return None
            for selector in (
                "[data-testid='kline-chart']",
                ".chart-widget",
                ".tradingview-widget-container",
                "[class*='chart']",
                "canvas",
            ):
                locator = page.locator(selector).first
                try:
                    if locator.count() and locator.bounding_box():
                        png = locator.screenshot(type="png", timeout=10_000)
                        if self._png_has_chart_content(png):
                            browser.close()
                            return "data:image/png;base64," + base64.b64encode(
                                png
                            ).decode("ascii")
                except Exception:
                    continue
            try:
                clip = self._chart_clip(page)
                if clip:
                    png = page.screenshot(type="png", clip=clip)
                    if self._png_has_chart_content(png):
                        browser.close()
                        return "data:image/png;base64," + base64.b64encode(png).decode(
                            "ascii"
                        )
            except Exception:
                pass
            browser.close()
            return None

    @staticmethod
    def _dismiss_popups(page) -> None:
        for text in ("接受", "同意", "Accept", "I Understand", "我知道了"):
            try:
                button = page.get_by_text(text).first
                if button.count() and button.is_visible(timeout=1000):
                    button.click(timeout=1000)
            except Exception:
                continue

    def _wait_for_chart_pixels(self, page) -> bool:
        deadline = self.timeout_ms
        step = 1000
        waited = 0
        while waited < deadline:
            try:
                if page.evaluate(
                    """() => {
                        const canvases = Array.from(document.querySelectorAll('canvas'));
                        for (const canvas of canvases) {
                            const box = canvas.getBoundingClientRect();
                            if (box.width < 200 || box.height < 160) continue;
                            const ctx = canvas.getContext('2d');
                            if (!ctx) continue;
                            const width = Math.min(canvas.width, 320);
                            const height = Math.min(canvas.height, 220);
                            if (width < 80 || height < 80) continue;
                            const data = ctx.getImageData(0, 0, width, height).data;
                            let bright = 0;
                            let varied = 0;
                            for (let i = 0; i < data.length; i += 16) {
                                const r = data[i], g = data[i + 1], b = data[i + 2], a = data[i + 3];
                                if (a && (r > 35 || g > 35 || b > 35)) bright++;
                                if (Math.abs(r - g) > 8 || Math.abs(g - b) > 8) varied++;
                            }
                            if (bright > 500 && varied > 100) return true;
                        }
                        return false;
                    }"""
                ):
                    return True
            except Exception:
                pass
            page.wait_for_timeout(step)
            waited += step
        return False

    @staticmethod
    def _chart_clip(page) -> dict | None:
        return page.evaluate(
            """() => {
                const candidates = Array.from(document.querySelectorAll('canvas, [class*=chart], [data-testid*=chart]'));
                let best = null;
                for (const node of candidates) {
                    const box = node.getBoundingClientRect();
                    if (box.width < 300 || box.height < 220) continue;
                    if (!best || box.width * box.height > best.width * best.height) {
                        best = {x: box.x, y: box.y, width: box.width, height: box.height};
                    }
                }
                if (!best) return null;
                return {
                    x: Math.max(0, best.x),
                    y: Math.max(0, best.y),
                    width: Math.min(window.innerWidth - Math.max(0, best.x), best.width),
                    height: Math.min(window.innerHeight - Math.max(0, best.y), best.height)
                };
            }"""
        )

    @staticmethod
    def _png_has_chart_content(png: bytes) -> bool:
        return len(png) > 20_000
