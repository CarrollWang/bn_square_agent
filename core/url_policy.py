from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def _host_matches(host: str, domain: str) -> bool:
    normalized = host.rstrip(".").lower()
    expected = domain.rstrip(".").lower()
    return normalized == expected or normalized.endswith(f".{expected}")


def validate_https_url_for_domains(
    value: str,
    *,
    domains: tuple[str, ...],
    label: str,
) -> str:
    url = value.strip()
    parts = urlsplit(url)
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise ValueError(f"{label}必须使用 https:// 地址")
    if parts.username or parts.password:
        raise ValueError(f"{label}不能包含 URL 用户名或密码")
    if not any(_host_matches(parts.hostname, domain) for domain in domains):
        allowed = "、".join(domains)
        raise ValueError(f"{label}仅允许域名: {allowed}")
    return urlunsplit(("https", parts.netloc, parts.path, parts.query, ""))


def validate_binance_url(value: str, *, label: str = "Binance 地址") -> str:
    return validate_https_url_for_domains(
        value,
        domains=("binance.com",),
        label=label,
    )


def validate_techflow_url(value: str, *, label: str = "TechFlow 地址") -> str:
    return validate_https_url_for_domains(
        value,
        domains=("techflowpost.com",),
        label=label,
    )


NEWS_FEED_DOMAINS = (
    "techflowpost.com",
    "panewslab.com",
    "coindesk.com",
    "cointelegraph.com",
)


def validate_news_feed_url(value: str, *, label: str = "新闻源地址") -> str:
    return validate_https_url_for_domains(
        value,
        domains=NEWS_FEED_DOMAINS,
        label=label,
    )
