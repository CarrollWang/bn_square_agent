from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..storage.database import Database
from .models import MaterialArticle
from .news_feed import NewsFeedMonitor


class MaterialSourceService:
    def __init__(
        self,
        db: Database,
        *,
        material_ttl_seconds: int | None = None,
    ):
        self.db = db
        self.material_ttl_seconds = material_ttl_seconds
        self.news_feed = NewsFeedMonitor()

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
            if source_type != "news_feed":
                raise ValueError(f"不支持的素材源类型: {source_type}")
            articles = self.news_feed.fetch(source["url"])
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
