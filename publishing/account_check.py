from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from ..core.config import normalize_proxy_url, playwright_proxy_settings


BINANCE_BASE_URL = "https://www.binance.com"


@dataclass(frozen=True)
class AccountCheckResult:
    valid: bool
    signature_key: str | None = None
    error: str | None = None
    raw: dict[str, Any] | None = None


class BinanceAccountChecker:
    def __init__(self, *, timeout: float = 30.0):
        self.timeout = timeout

    @staticmethod
    def _headers(cookie: str) -> dict[str, str]:
        return {
            "accept": "application/json",
            "clienttype": "web",
            "content-type": "application/json",
            "cookie": cookie,
            "csrftoken": "",
            "lang": "zh-CN",
            "origin": BINANCE_BASE_URL,
            "referer": f"{BINANCE_BASE_URL}/zh-CN/square",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        }

    @staticmethod
    def _find_square_uid(value: Any) -> str | None:
        if isinstance(value, dict):
            for key in ("squareUid", "signatureKey", "signature_key"):
                item = value.get(key)
                if isinstance(item, str) and item:
                    return item
            for child in value.values():
                found = BinanceAccountChecker._find_square_uid(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = BinanceAccountChecker._find_square_uid(child)
                if found:
                    return found
        return None

    def check(self, cookie: str, proxy_url: str = "") -> AccountCheckResult:
        headers = self._headers(cookie)
        proxy = normalize_proxy_url(proxy_url) if proxy_url else ""
        try:
            with httpx.Client(
                timeout=self.timeout,
                follow_redirects=True,
                headers=headers,
                trust_env=False,
                proxy=proxy or None,
            ) as client:
                auth = client.post("/".join([BINANCE_BASE_URL, "bapi/accounts/v1/public/authcenter/auth"]))
                auth_json = auth.json()
                if not auth_json.get("success"):
                    return AccountCheckResult(
                        valid=False,
                        error=auth_json.get("message") or "Cookie 未登录或已失效",
                        raw=auth_json,
                    )

                candidates = (
                    (
                        "GET",
                        "/bapi/composite/v1/private/pgc/user/client",
                        None,
                    ),
                    (
                        "GET",
                        "/bapi/composite/v2/private/pgc/user/client",
                        None,
                    ),
                    (
                        "GET",
                        "/bapi/composite/v3/private/pgc/user/client",
                        None,
                    ),
                    (
                        "POST",
                        "/bapi/composite/v3/friendly/pgc/user/client",
                        {
                            "getFollowCount": True,
                            "queryFollowersInfo": True,
                            "queryRelationTokens": True,
                        },
                    ),
                )
                last_payload: dict[str, Any] = auth_json
                for method, path, payload in candidates:
                    url = f"{BINANCE_BASE_URL}{path}"
                    response = (
                        client.post(url, json=payload or {})
                        if method == "POST"
                        else client.get(url)
                    )
                    try:
                        data = response.json()
                    except ValueError:
                        continue
                    last_payload = data
                    signature_key = self._find_square_uid(data)
                    if signature_key:
                        return AccountCheckResult(
                            valid=True,
                            signature_key=signature_key,
                            raw=data,
                        )

                return AccountCheckResult(
                    valid=True,
                    error="Cookie 有效，但未从广场接口解析到 squareUid/signature_key",
                    raw=last_payload,
                )
        except Exception as exc:
            browser_result = self._check_with_playwright(cookie, proxy_url=proxy)
            if browser_result.error:
                return AccountCheckResult(
                    valid=browser_result.valid,
                    signature_key=browser_result.signature_key,
                    error=f"HTTP 检测失败: {exc}; 浏览器检测: {browser_result.error}",
                    raw=browser_result.raw,
                )
            return browser_result

    @staticmethod
    def _parse_cookie_header(cookie: str) -> list[dict[str, Any]]:
        items = []
        cookie = cookie.strip()
        lines = [line.strip() for line in cookie.replace("\r", "").split("\n") if line.strip()]
        cookie_lines = [
            line.split(":", 1)[1].strip()
            for line in lines
            if line.lower().startswith("cookie:")
        ]
        if cookie_lines:
            cookie = "; ".join(cookie_lines)
        elif cookie.lower().startswith("cookie:"):
            cookie = cookie.split(":", 1)[1].strip()
        for part in cookie.split(";"):
            if "=" not in part:
                continue
            name, value = part.split("=", 1)
            name = name.strip()
            value = value.strip().strip('"')
            if not name or any(ch.isspace() for ch in name):
                continue
            items.append(
                {
                    "name": name,
                    "value": value,
                    "url": BINANCE_BASE_URL,
                }
            )
        return items

    def _check_with_playwright(
        self,
        cookie: str,
        *,
        proxy_url: str = "",
    ) -> AccountCheckResult:
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                launch_args: dict[str, Any] = {
                    "headless": True,
                    "args": ["--disable-blink-features=AutomationControlled"],
                }
                proxy = playwright_proxy_settings(proxy_url) if proxy_url else None
                if proxy:
                    launch_args["proxy"] = proxy
                browser = p.chromium.launch(
                    **launch_args,
                )
                context = browser.new_context(
                    locale="zh-CN",
                    user_agent=self._headers(cookie)["user-agent"],
                )
                parsed = self._parse_cookie_header(cookie)
                if parsed:
                    context.add_cookies(parsed)
                page = context.new_page()
                page.goto(
                    f"{BINANCE_BASE_URL}/zh-CN/square",
                    wait_until="domcontentloaded",
                    timeout=int(self.timeout * 1000),
                )
                result = page.evaluate(
                    """async () => {
                        const authResponse = await fetch('/bapi/accounts/v1/public/authcenter/auth', {
                            method: 'POST',
                            credentials: 'include'
                        });
                        const auth = await authResponse.json();
                        if (!auth.success) {
                            return {valid: false, error: auth.message || 'Cookie 未登录或已失效', raw: auth};
                        }
                        const endpoints = [
                            ['GET', '/bapi/composite/v1/private/pgc/user/client', null],
                            ['GET', '/bapi/composite/v2/private/pgc/user/client', null],
                            ['GET', '/bapi/composite/v3/private/pgc/user/client', null]
                        ];
                        for (const [method, path, body] of endpoints) {
                            const response = await fetch(path, {
                                method,
                                credentials: 'include',
                                headers: {'content-type': 'application/json'},
                                body: body ? JSON.stringify(body) : undefined
                            });
                            let data = null;
                            try { data = await response.json(); } catch (_) { continue; }
                            const stack = [data];
                            while (stack.length) {
                                const current = stack.pop();
                                if (!current || typeof current !== 'object') continue;
                                for (const key of ['squareUid', 'signatureKey', 'signature_key']) {
                                    if (typeof current[key] === 'string' && current[key]) {
                                        return {valid: true, signature_key: current[key], raw: data};
                                    }
                                }
                                for (const value of Object.values(current)) {
                                    if (value && typeof value === 'object') stack.push(value);
                                }
                            }
                        }
                        return {valid: true, error: 'Cookie 有效，但未解析到 squareUid/signature_key', raw: auth};
                    }"""
                )
                browser.close()
            return AccountCheckResult(
                valid=bool(result.get("valid")),
                signature_key=result.get("signature_key"),
                error=result.get("error"),
                raw=result.get("raw"),
            )
        except Exception as exc:
            return AccountCheckResult(valid=False, error=str(exc))
