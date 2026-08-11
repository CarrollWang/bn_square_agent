from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
import json
import re
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

import httpx

from ..core.url_policy import validate_https_url_for_domains
from .models import MaterialArticle


WUSHUO_RSS_URL = "https://www.wublock123.com/rss"
ETHEREUM_BLOG_RSS_URL = "https://blog.ethereum.org/feed.xml"
ETHEREUM_BLOG_REDIRECT_RSS_URL = "https://blog.ethereum.org/en/feed.xml"
BITCOIN_CORE_RSS_URL = "https://bitcoincore.org/en/feed.xml"
WALLSTREETCN_API_URL = (
    "https://api-one.wallstcn.com/apiv1/content/lives"
    "?channel=global-channel&limit=100"
)
CHAINCATCHER_API_URL = (
    "https://api.chaincatcher.com/v1/open-api/news-flash"
    "?type=flash&page=1&size=50&lang=zh-CN"
)


RSS_FEED_PRESETS: dict[str, dict[str, Any]] = {
    WUSHUO_RSS_URL: {
        "name": "吴说区块链",
        "author": "吴说区块链",
        "feed_domains": ("wublock123.com",),
        "item_domains": ("wublock123.com",),
    },
    ETHEREUM_BLOG_RSS_URL: {
        "name": "Ethereum Foundation Blog",
        "author": "Ethereum Foundation",
        "feed_domains": ("blog.ethereum.org",),
        "item_domains": ("blog.ethereum.org", "ethereum.org"),
    },
    BITCOIN_CORE_RSS_URL: {
        "name": "Bitcoin Core",
        "author": "Bitcoin Core",
        "feed_domains": ("bitcoincore.org",),
        "item_domains": ("bitcoincore.org",),
    },
}

# Exact official redirect targets may be accepted without opening arbitrary feed URLs.
RSS_FEED_PRESET_ALIASES = {
    ETHEREUM_BLOG_REDIRECT_RSS_URL: ETHEREUM_BLOG_RSS_URL,
}


MARKET_RELEVANCE_KEYWORDS = (
    "bitcoin",
    "btc",
    "ethereum",
    "eth",
    "solana",
    "sol",
    "xrp",
    "doge",
    "crypto",
    "web3",
    "blockchain",
    "stablecoin",
    "token",
    "binance",
    "比特币",
    "以太坊",
    "加密",
    "区块链",
    "稳定币",
    "代币",
    "币安",
    "美联储",
    "降息",
    "加息",
    "非农",
    "cpi",
    "美元指数",
    "美国国债",
)


class SourceParseError(ValueError):
    pass


class _PlainTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        value = data.strip()
        if value:
            self.parts.append(value)


def clean_text(value: str, *, max_chars: int = 4_000) -> str:
    parser = _PlainTextParser()
    try:
        parser.feed(unescape(value or ""))
        text = " ".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", unescape(value or ""))
    return " ".join(text.split())[:max_chars]


def _canonical_url(value: str) -> str:
    parts = urlsplit(value.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def validate_rss_feed_url(value: str) -> str:
    canonical = _canonical_url(value)
    preset_url = RSS_FEED_PRESET_ALIASES.get(canonical, canonical)
    preset = RSS_FEED_PRESETS.get(preset_url)
    if not preset:
        allowed = "、".join(item["name"] for item in RSS_FEED_PRESETS.values())
        raise ValueError(f"RSS 素材源仅支持预设: {allowed}")
    validated = validate_https_url_for_domains(
        canonical,
        domains=tuple(preset["feed_domains"]),
        label="RSS 素材源",
    )
    if validated != canonical:
        raise ValueError("RSS 素材源地址不匹配预设")
    return canonical


def validate_news_source_url(source_type: str, value: str) -> str:
    canonical = _canonical_url(value)
    if source_type == "rss_feed":
        return validate_rss_feed_url(canonical)
    if source_type == "wallstreetcn_live":
        validate_https_url_for_domains(
            canonical,
            domains=("api-one.wallstcn.com",),
            label="华尔街见闻素材源",
        )
        if canonical != WALLSTREETCN_API_URL:
            raise ValueError("华尔街见闻素材源必须使用系统预设接口")
        return canonical
    if source_type == "chaincatcher_flash":
        validate_https_url_for_domains(
            canonical,
            domains=("api.chaincatcher.com",),
            label="ChainCatcher 素材源",
        )
        if canonical != CHAINCATCHER_API_URL:
            raise ValueError("ChainCatcher 素材源必须使用系统预设接口")
        return canonical
    raise ValueError(f"不支持的新闻素材源类型: {source_type}")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(element: ET.Element, names: tuple[str, ...]) -> str:
    for child in list(element):
        if _local_name(child.tag) in names:
            return " ".join(part.strip() for part in child.itertext() if part.strip())
    return ""


def _entry_link(element: ET.Element) -> str:
    for child in list(element):
        if _local_name(child.tag) != "link":
            continue
        href = str(child.attrib.get("href") or "").strip()
        if href:
            return href
        if child.text:
            return child.text.strip()
    return ""


def _parse_datetime(value: str) -> str | None:
    raw = value.strip()
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def parse_rss_or_atom(
    raw: bytes,
    *,
    author: str,
    item_domains: tuple[str, ...],
    limit: int = 60,
) -> list[MaterialArticle]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise SourceParseError(f"RSS/Atom 解析失败: {exc}") from exc
    entries = [
        element
        for element in root.iter()
        if _local_name(element.tag) in {"item", "entry"}
    ]
    if not entries:
        raise SourceParseError("RSS/Atom 未找到文章条目")
    articles: list[MaterialArticle] = []
    seen: set[str] = set()
    for entry in entries:
        title = clean_text(_child_text(entry, ("title",)), max_chars=300)
        link = _entry_link(entry)
        if not link:
            link = _child_text(entry, ("link",))
        published_at = _parse_datetime(
            _child_text(entry, ("pubdate", "published", "updated", "date"))
        )
        summary = clean_text(
            _child_text(entry, ("description", "summary", "content", "encoded")),
            max_chars=4_000,
        )
        if not title or not link or not published_at:
            continue
        try:
            safe_link = validate_https_url_for_domains(
                link,
                domains=item_domains,
                label="RSS 文章链接",
            )
        except ValueError:
            continue
        external_id = _child_text(entry, ("guid", "id")) or safe_link
        if external_id in seen:
            continue
        seen.add(external_id)
        content = title if not summary or summary == title else f"{title}\n{summary}"
        articles.append(
            MaterialArticle(
                title=title,
                content=content,
                author=author,
                url=safe_link,
                external_id=external_id,
                source_created_at=published_at,
            )
        )
        if len(articles) >= limit:
            break
    if not articles:
        raise SourceParseError("RSS/Atom 没有可安全入库的文章")
    return articles


def _is_market_relevant(title: str, summary: str) -> bool:
    text = f"{title}\n{summary}".casefold()
    for keyword in MARKET_RELEVANCE_KEYWORDS:
        normalized = keyword.casefold()
        if normalized.isascii() and normalized.isalnum() and len(normalized) <= 4:
            if re.search(rf"\b{re.escape(normalized)}\b", text):
                return True
        elif normalized in text:
            return True
    return False


def _fetch_bytes(
    url: str,
    *,
    validator: Callable[[str], str],
    timeout_seconds: int,
    max_bytes: int = 2_000_000,
) -> bytes:
    target = validator(url)
    with httpx.Client(
        timeout=timeout_seconds,
        follow_redirects=False,
        trust_env=False,
        headers={"user-agent": "bn-square-agent/1.0", "accept": "application/json, application/xml, text/xml, */*"},
    ) as client:
        for _ in range(4):
            response = client.get(target)
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise ValueError("素材源重定向缺少 Location")
                target = validator(urljoin(str(response.url), location))
                continue
            response.raise_for_status()
            body = response.content
            if len(body) > max_bytes:
                raise ValueError("素材源响应过大")
            return body
    raise ValueError("素材源重定向次数过多")


class RssFeedMonitor:
    def __init__(self, *, timeout_seconds: int = 25, limit: int = 60):
        self.timeout_seconds = timeout_seconds
        self.limit = limit

    def fetch(self, url: str) -> list[MaterialArticle]:
        canonical = validate_rss_feed_url(url)
        preset_url = RSS_FEED_PRESET_ALIASES.get(canonical, canonical)
        preset = RSS_FEED_PRESETS[preset_url]
        raw = _fetch_bytes(
            canonical,
            validator=validate_rss_feed_url,
            timeout_seconds=self.timeout_seconds,
        )
        return parse_rss_or_atom(
            raw,
            author=str(preset["author"]),
            item_domains=tuple(preset["item_domains"]),
            limit=self.limit,
        )


class WallStreetCNMonitor:
    def __init__(self, *, timeout_seconds: int = 25, limit: int = 60):
        self.timeout_seconds = timeout_seconds
        self.limit = limit

    @staticmethod
    def parse(raw: bytes, *, limit: int = 60) -> list[MaterialArticle]:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SourceParseError(f"华尔街见闻 JSON 解析失败: {exc}") from exc
        data = payload.get("data")
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise SourceParseError("华尔街见闻响应缺少 data.items")
        articles: list[MaterialArticle] = []
        for item in items:
            if not isinstance(item, dict) or item.get("is_calendar"):
                continue
            title = clean_text(str(item.get("title") or ""), max_chars=300)
            summary = clean_text(
                str(item.get("content_text") or item.get("content") or ""),
                max_chars=4_000,
            )
            uri = str(item.get("uri") or "").strip()
            stamp = item.get("display_time")
            if not uri or stamp is None or not (title or summary):
                continue
            if not _is_market_relevant(title, summary):
                continue
            try:
                safe_url = validate_https_url_for_domains(
                    uri,
                    domains=("wallstreetcn.com",),
                    label="华尔街见闻文章链接",
                )
                published_at = datetime.fromtimestamp(int(stamp), tz=timezone.utc).isoformat()
            except (ValueError, TypeError, OverflowError):
                continue
            content = title if not summary or summary == title else f"{title}\n{summary}"
            articles.append(
                MaterialArticle(
                    title=title or summary[:60],
                    content=content,
                    author="华尔街见闻",
                    url=safe_url,
                    external_id=str(item.get("id") or safe_url),
                    source_created_at=published_at,
                )
            )
            if len(articles) >= limit:
                break
        return articles

    def fetch(self, url: str) -> list[MaterialArticle]:
        raw = _fetch_bytes(
            url,
            validator=lambda value: validate_news_source_url("wallstreetcn_live", value),
            timeout_seconds=self.timeout_seconds,
        )
        return self.parse(raw, limit=self.limit)


class ChainCatcherMonitor:
    def __init__(self, *, timeout_seconds: int = 25, limit: int = 60):
        self.timeout_seconds = timeout_seconds
        self.limit = limit

    @staticmethod
    def parse(raw: bytes, *, limit: int = 60) -> list[MaterialArticle]:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SourceParseError(f"ChainCatcher JSON 解析失败: {exc}") from exc
        data = payload.get("data")
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise SourceParseError("ChainCatcher 响应缺少 data.items")
        articles: list[MaterialArticle] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            title = clean_text(str(item.get("title") or ""), max_chars=300)
            summary = clean_text(
                str(item.get("description") or item.get("content") or ""),
                max_chars=4_000,
            )
            url = str(item.get("url") or "").strip()
            stamp = item.get("releaseTimeStamp")
            if not url or stamp is None or not (title or summary):
                continue
            if not _is_market_relevant(title, summary):
                continue
            try:
                safe_url = validate_https_url_for_domains(
                    url,
                    domains=("chaincatcher.com",),
                    label="ChainCatcher 文章链接",
                )
                timestamp = int(stamp)
                if timestamp > 1_000_000_000_000:
                    timestamp //= 1_000
                published_at = datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
            except (ValueError, TypeError, OverflowError):
                continue
            content = title if not summary or summary == title else f"{title}\n{summary}"
            articles.append(
                MaterialArticle(
                    title=title or summary[:60],
                    content=content,
                    author="ChainCatcher",
                    url=safe_url,
                    external_id=str(item.get("id") or safe_url),
                    source_created_at=published_at,
                )
            )
            if len(articles) >= limit:
                break
        return articles

    def fetch(self, url: str) -> list[MaterialArticle]:
        raw = _fetch_bytes(
            url,
            validator=lambda value: validate_news_source_url("chaincatcher_flash", value),
            timeout_seconds=self.timeout_seconds,
        )
        return self.parse(raw, limit=self.limit)
