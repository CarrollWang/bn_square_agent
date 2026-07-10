from __future__ import annotations

from datetime import datetime
import html
import json
import re
from typing import Any
from urllib.parse import urljoin

import httpx

from ..core.url_policy import validate_techflow_url
from .models import MaterialArticle


TECHFLOW_BASE_URL = "https://www.techflowpost.com"
TECHFLOW_DEFAULT_URL = (
    "https://www.techflowpost.com/newsletter?is_hot=1&articleType=1006"
)


class TechFlowNewsletterMonitor:
    def __init__(self, *, timeout_seconds: int = 25, limit: int = 60):
        self.timeout_seconds = timeout_seconds
        self.limit = limit

    @staticmethod
    def normalize_url(url: str | None) -> str:
        value = (url or "").strip()
        return validate_techflow_url(
            value or TECHFLOW_DEFAULT_URL,
            label="TechFlow 素材源",
        )

    @staticmethod
    def _normalize_page_text(text: str) -> str:
        normalized = html.unescape(text)
        return (
            normalized.replace(r"\/", "/")
            .replace(r"\u0026", "&")
            .replace(r"\"", '"')
        )

    @staticmethod
    def _article_url(item: dict[str, Any]) -> str:
        item_id = str(item.get("id") or "").strip()
        return urljoin(TECHFLOW_BASE_URL, f"/zh-CN/newsletter/{item_id}")

    @staticmethod
    def _source_created_at(value: Any) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
        except ValueError:
            return value

    @classmethod
    def _article_from_item(cls, item: dict[str, Any]) -> MaterialArticle | None:
        title = str(item.get("title") or "").strip()
        abstract = str(item.get("abstract") or "").strip()
        if len(title) < 4 or len(abstract) < 8:
            return None
        content = f"{title}\n{abstract}"
        source_url = str(item.get("url") or "").strip()
        return MaterialArticle(
            title=title,
            content=content,
            author="深潮 TechFlow",
            url=source_url or cls._article_url(item),
            external_id=str(item.get("id") or "").strip() or None,
            source_created_at=cls._source_created_at(item.get("created_at")),
        )

    def fetch(self, url: str | None = None) -> list[MaterialArticle]:
        target_url = self.normalize_url(url)
        with httpx.Client(
            timeout=self.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            headers={
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "accept-language": "zh-CN,zh;q=0.9",
                "user-agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
            },
        ) as client:
            response = self._get_with_safe_redirects(client, target_url)
            response.raise_for_status()
            page_text = response.text
        return self._parse_articles(page_text)

    @staticmethod
    def _get_with_safe_redirects(
        client: httpx.Client,
        url: str,
        *,
        max_redirects: int = 5,
    ) -> httpx.Response:
        target = validate_techflow_url(url, label="TechFlow 素材源")
        for _ in range(max_redirects + 1):
            response = client.get(target)
            if not response.is_redirect:
                return response
            location = response.headers.get("location")
            if not location:
                return response
            target = validate_techflow_url(
                urljoin(str(response.url), location),
                label="TechFlow 重定向地址",
            )
        raise ValueError("TechFlow 重定向次数过多")

    def _parse_articles(self, page_text: str) -> list[MaterialArticle]:
        normalized = self._normalize_page_text(page_text)
        pattern = re.compile(
            r'\{"id":\d+,"title":.*?,"is_favorited":(?:true|false)\}',
            re.DOTALL,
        )
        articles: list[MaterialArticle] = []
        seen_ids: set[str] = set()
        for match in pattern.finditer(normalized):
            try:
                item = json.loads(match.group(0))
            except json.JSONDecodeError:
                continue
            article = self._article_from_item(item)
            if not article or not article.external_id:
                continue
            if article.external_id in seen_ids:
                continue
            seen_ids.add(article.external_id)
            articles.append(article)
            if len(articles) >= self.limit:
                break
        return articles
