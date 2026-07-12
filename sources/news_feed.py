from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import html
import re
from urllib.parse import urljoin, urlsplit
import xml.etree.ElementTree as ET

import httpx

from ..core.url_policy import validate_news_feed_url
from .models import MaterialArticle
from .techflow import TechFlowNewsletterMonitor


class RSSNewsMonitor:
    def __init__(self, *, timeout_seconds: int = 25, limit: int = 60):
        self.timeout_seconds = timeout_seconds
        self.limit = limit

    @staticmethod
    def _local_name(tag: str) -> str:
        return tag.rsplit("}", 1)[-1].lower()

    @classmethod
    def _child_text(cls, node: ET.Element, *names: str) -> str:
        expected = {name.lower() for name in names}
        for child in node:
            if cls._local_name(child.tag) in expected:
                return "".join(child.itertext()).strip()
        return ""

    @staticmethod
    def _plain_text(value: str) -> str:
        text = re.sub(r"<[^>]+>", " ", html.unescape(value or ""))
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _published_at(value: str) -> str | None:
        text = value.strip()
        if not text:
            return None
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()

    @classmethod
    def parse(
        cls,
        payload: bytes,
        *,
        source_url: str,
        limit: int = 60,
    ) -> list[MaterialArticle]:
        root = ET.fromstring(payload)
        root_name = cls._local_name(root.tag)
        if root_name in {"rss", "rdf"}:
            channel = next(
                (node for node in root.iter() if cls._local_name(node.tag) == "channel"),
                root,
            )
            feed_title = cls._child_text(channel, "title") or urlsplit(source_url).hostname
            entries = [node for node in root.iter() if cls._local_name(node.tag) == "item"]
        elif root_name == "feed":
            channel = root
            feed_title = cls._child_text(root, "title") or urlsplit(source_url).hostname
            entries = [node for node in root if cls._local_name(node.tag) == "entry"]
        else:
            raise ValueError("新闻源不是有效的 RSS 或 Atom 文档")

        articles: list[MaterialArticle] = []
        seen: set[str] = set()
        for entry in entries:
            title = cls._plain_text(cls._child_text(entry, "title"))
            summary = cls._plain_text(
                cls._child_text(entry, "description", "summary", "content", "encoded")
            )
            link = cls._child_text(entry, "link")
            if not link:
                link_node = next(
                    (child for child in entry if cls._local_name(child.tag) == "link"),
                    None,
                )
                link = str(link_node.get("href") or "") if link_node is not None else ""
            link = urljoin(source_url, link)
            external_id = cls._child_text(entry, "guid", "id") or link or title
            if not title or external_id in seen:
                continue
            seen.add(external_id)
            content = f"{title}\n{summary}".strip()
            if len(re.sub(r"\s+", "", content)) < 12:
                continue
            published = cls._child_text(entry, "pubdate", "published", "updated", "date")
            author = cls._plain_text(cls._child_text(entry, "author", "creator"))
            articles.append(
                MaterialArticle(
                    title=title,
                    content=content[:6_000],
                    author=author or feed_title,
                    url=link or None,
                    external_id=external_id,
                    source_created_at=cls._published_at(published),
                )
            )
            if len(articles) >= limit:
                break
        return articles

    def fetch(self, url: str) -> list[MaterialArticle]:
        target = validate_news_feed_url(url)
        with httpx.Client(
            timeout=self.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            headers={"user-agent": "bn-square-agent/1.0 news-feed"},
        ) as client:
            for _ in range(6):
                response = client.get(target)
                if not response.is_redirect:
                    response.raise_for_status()
                    return self.parse(
                        response.content,
                        source_url=target,
                        limit=self.limit,
                    )
                location = response.headers.get("location")
                if not location:
                    break
                target = validate_news_feed_url(urljoin(target, location))
        raise ValueError("新闻源重定向次数过多或缺少重定向地址")


class NewsFeedMonitor:
    def __init__(self):
        self.techflow = TechFlowNewsletterMonitor()
        self.rss = RSSNewsMonitor()

    def fetch(self, url: str) -> list[MaterialArticle]:
        target = validate_news_feed_url(url)
        host = (urlsplit(target).hostname or "").lower()
        if host == "techflowpost.com" or host.endswith(".techflowpost.com"):
            return self.techflow.fetch(target)
        return self.rss.fetch(target)
