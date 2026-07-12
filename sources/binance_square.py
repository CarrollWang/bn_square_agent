from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Any
from urllib.parse import urlparse

from ..core.url_policy import validate_binance_url
from ..storage.database import Database
from .models import MaterialArticle
from .techflow import TechFlowNewsletterMonitor


BINANCE_BASE_URL = "https://www.binance.com"
PAGE_FETCH_TIMEOUT_MS = 45_000
USER_CLIENT_API = "/bapi/composite/v3/friendly/pgc/user/client"
PROFILE_CONTENTS_API = (
    "/bapi/composite/v2/friendly/pgc/content/queryUserProfilePageContentsWithFilter"
)


class BinanceSquareMonitor:
    def __init__(self, *, timeout_ms: int = 60_000, max_pages: int = 3):
        self.timeout_ms = timeout_ms
        self.max_pages = max_pages

    @staticmethod
    def _username_from_url(url: str) -> str:
        validated = validate_binance_url(url, label="BN 广场作者主页")
        path = urlparse(validated).path.rstrip("/")
        match = re.search(r"/square/profile/([^/]+)$", path, re.IGNORECASE)
        if not match:
            raise ValueError("素材源 URL 必须是 BN 广场作者主页，例如 /square/profile/xxx")
        return match.group(1)

    @staticmethod
    def _ms_to_iso(value: Any) -> str | None:
        if not isinstance(value, int):
            return None
        return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()

    @staticmethod
    def _article_from_content(item: dict[str, Any]) -> MaterialArticle | None:
        content = str(item.get("bodyTextOnly") or "").strip()
        if len(content) < 20:
            return None
        content_id = item.get("id")
        title = str(item.get("title") or "").strip() or None
        if not title:
            title = content[:48]
        return MaterialArticle(
            title=title,
            content=content,
            author=str(item.get("displayName") or item.get("username") or "").strip()
            or None,
            url=item.get("webLink"),
            external_id=str(content_id) if content_id is not None else None,
            source_created_at=BinanceSquareMonitor._ms_to_iso(
                item.get("firstReleaseTime") or item.get("createTime")
            ),
        )

    def fetch(self, url: str) -> list[MaterialArticle]:
        username = self._username_from_url(url)
        return self._fetch_with_playwright(username)

    def _fetch_with_playwright(self, username: str) -> list[MaterialArticle]:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                locale="zh-CN",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()
            profile_url = f"{BINANCE_BASE_URL}/zh-CN/square/profile/{username}"
            page.goto(profile_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            page.wait_for_timeout(3000)

            user_result = page.evaluate(
                """async ({api, username, timeoutMs}) => {
                    const controller = new AbortController();
                    const timer = setTimeout(() => controller.abort(), timeoutMs);
                    try {
                      const response = await fetch(api, {
                        method: 'POST',
                        credentials: 'include',
                        headers: {'content-type': 'application/json'},
                        signal: controller.signal,
                        body: JSON.stringify({
                            username,
                            getFollowCount: true,
                            queryFollowersInfo: true,
                            queryRelationTokens: true
                        })
                      });
                      return await response.json();
                    } finally {
                      clearTimeout(timer);
                    }
                }""",
                {
                    "api": USER_CLIENT_API,
                    "username": username,
                    "timeoutMs": PAGE_FETCH_TIMEOUT_MS,
                },
            )
            data = user_result.get("data") or {}
            square_uid = data.get("squareUid")
            if not square_uid:
                browser.close()
                raise ValueError(f"无法解析作者 squareUid: {user_result}")

            articles: list[MaterialArticle] = []
            seen_ids: set[str] = set()
            time_offset: int | str = -1
            for _ in range(self.max_pages):
                contents_result = page.evaluate(
                    """async ({api, squareUid, timeOffset, timeoutMs}) => {
                        const controller = new AbortController();
                        const timer = setTimeout(() => controller.abort(), timeoutMs);
                        const params = new URLSearchParams({
                            targetSquareUid: squareUid,
                            timeOffset: String(timeOffset),
                            filterType: 'ALL'
                        });
                        try {
                          const response = await fetch(`${api}?${params}`, {
                              credentials: 'include',
                              signal: controller.signal
                          });
                          return await response.json();
                        } finally {
                          clearTimeout(timer);
                        }
                    }""",
                    {
                        "api": PROFILE_CONTENTS_API,
                        "squareUid": square_uid,
                        "timeOffset": time_offset,
                        "timeoutMs": PAGE_FETCH_TIMEOUT_MS,
                    },
                )
                contents_data = contents_result.get("data") or {}
                contents = contents_data.get("contents") or []
                if not contents:
                    break
                for item in contents:
                    article = self._article_from_content(item)
                    if not article:
                        continue
                    key = article.external_id or article.content[:120]
                    if key in seen_ids:
                        continue
                    seen_ids.add(key)
                    articles.append(article)
                next_offset = contents_data.get("timeOffset")
                if not next_offset or next_offset == time_offset:
                    break
                time_offset = next_offset
            browser.close()
            return articles


class MaterialSourceService:
    def __init__(
        self,
        db: Database,
        *,
        material_ttl_seconds: int | None = None,
    ):
        self.db = db
        self.material_ttl_seconds = material_ttl_seconds
        self.binance_square = BinanceSquareMonitor()
        self.techflow_newsletter = TechFlowNewsletterMonitor()

    def _is_stale(self, article: MaterialArticle) -> bool:
        if not self.material_ttl_seconds or not article.source_created_at:
            return False
        try:
            published_at = datetime.fromisoformat(
                article.source_created_at.replace("Z", "+00:00")
            )
        except ValueError:
            return False
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
        cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=self.material_ttl_seconds
        )
        return published_at.astimezone(timezone.utc) < cutoff

    def check_source(self, source: dict[str, Any]) -> dict[str, Any]:
        source_type = source["source_type"]
        try:
            if source_type == "binance_square":
                articles = self.binance_square.fetch(source["url"])
            elif source_type == "techflow_newsletter":
                articles = self.techflow_newsletter.fetch(source["url"])
            else:
                raise ValueError(f"不支持的素材源类型: {source_type}")
            inserted = 0
            stale_skipped = 0
            for article in articles:
                if self._is_stale(article):
                    stale_skipped += 1
                    continue
                _, fresh = self.db.add_material_item(
                    source_id=source["id"],
                    external_id=article.external_id,
                    author=article.author,
                    title=article.title,
                    content=article.content,
                    url=article.url,
                    source_created_at=article.source_created_at,
                )
                inserted += 1 if fresh else 0
            self.db.update_material_source_check(source["id"])
            return {
                "source_id": source["id"],
                "source_type": source_type,
                "found": len(articles),
                "inserted": inserted,
                "stale_skipped": stale_skipped,
            }
        except Exception as exc:
            error = str(exc)
            if "AbortError" in error:
                error = f"BN 页面接口超时 {PAGE_FETCH_TIMEOUT_MS // 1000}s，下一轮重试"
            self.db.update_material_source_check(source["id"], error=error)
            return {
                "source_id": source["id"],
                "source_type": source_type,
                "found": 0,
                "inserted": 0,
                "error": error,
            }

    def check_all(self) -> list[dict[str, Any]]:
        return [self.check_source(source) for source in self.db.list_material_sources()]
