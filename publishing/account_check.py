from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from ..core.config import normalize_proxy_url, playwright_proxy_settings


BINANCE_BASE_URL = "https://www.binance.com"
BINANCE_AUTH_PATH = "/bapi/accounts/v1/public/authcenter/auth"
BINANCE_USER_ENDPOINTS = (
    ("GET", "/bapi/composite/v1/private/pgc/user/client", None, True),
    ("GET", "/bapi/composite/v2/private/pgc/user/client", None, True),
    ("GET", "/bapi/composite/v3/private/pgc/user/client", None, True),
    (
        "POST",
        "/bapi/composite/v3/friendly/pgc/user/client",
        {
            "getFollowCount": True,
            "queryFollowersInfo": True,
            "queryRelationTokens": True,
        },
        False,
    ),
)


BINANCE_PAGE_SESSION_PROBE = """async ({authPath, endpoints}) => {
    const findIdentity = (value) => {
        const stack = [value];
        while (stack.length) {
            const current = stack.pop();
            if (!current || typeof current !== 'object') continue;
            for (const key of ['squareUid', 'signatureKey', 'signature_key']) {
                if ((typeof current[key] === 'string' || typeof current[key] === 'number') && current[key]) {
                    return String(current[key]);
                }
            }
            for (const child of Object.values(current)) {
                if (child && typeof child === 'object') stack.push(child);
            }
        }
        return null;
    };
    const requestJson = async (method, path, body) => {
        try {
            const cookieValues = Object.fromEntries(
                document.cookie.split(';').map((item) => {
                    const index = item.indexOf('=');
                    if (index < 0) return [item.trim(), ''];
                    return [item.slice(0, index).trim(), item.slice(index + 1).trim()];
                }).filter(([name]) => name)
            );
            const csrfToken = cookieValues.cr00 || cookieValues.csrftoken || cookieValues.csrfToken || '';
            const options = {
                method,
                credentials: 'include',
                headers: {
                    'accept': 'application/json',
                    'clienttype': 'web',
                    'content-type': 'application/json',
                    'lang': 'zh-CN',
                    'csrftoken': csrfToken
                }
            };
            if (body !== null && body !== undefined) options.body = JSON.stringify(body);
            const response = await fetch(path, options);
            let payload = null;
            try { payload = await response.json(); } catch (_) {}
            return {response, payload, error: null};
        } catch (error) {
            return {response: null, payload: null, error: String(error)};
        }
    };
    const summary = (path, result) => ({
        path,
        status: result.response ? result.response.status : null,
        success: Boolean(result.payload && result.payload.success),
        code: result.payload && result.payload.code != null ? String(result.payload.code) : null,
        message: result.payload && result.payload.message ? String(result.payload.message) : null,
        error: result.error
    });
    const attempts = [];
    const authResult = await requestJson('POST', authPath, null);
    attempts.push(summary(authPath, authResult));
    const authPayload = authResult.payload;
    const authIdentity = findIdentity(authPayload);
    if (authIdentity) {
        return {valid: true, signature_key: authIdentity, source: 'auth_identity', attempts};
    }

    for (const endpoint of endpoints) {
        const [method, path, body, isPrivate] = endpoint;
        const result = await requestJson(method, path, body);
        attempts.push(summary(path, result));
        const identity = findIdentity(result.payload);
        if (identity) {
            return {valid: true, signature_key: identity, source: path, attempts};
        }
        const data = result.payload && result.payload.data;
        const hasPrivateData = isPrivate && result.payload && result.payload.success && data &&
            (typeof data !== 'object' || Object.keys(data).length > 0);
        if (hasPrivateData) {
            return {valid: true, signature_key: null, source: path, attempts};
        }
    }

    const isVisible = (element) => {
        if (!element) return false;
        const style = window.getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden' &&
            Number(style.opacity || 1) > 0 && rect.width > 0 && rect.height > 0;
    };
    const visibleControls = Array.from(document.querySelectorAll('a, button'))
        .filter(isVisible)
        .map((element) => (element.textContent || '').trim().replace(/\s+/g, ' '));
    const hasLoginControl = visibleControls.some((text) =>
        ['登录', '注册', 'Log In', 'Log in', 'Register', 'Sign Up', 'Sign up'].includes(text)
    );
    const visiblePageText = document.body ? document.body.innerText : '';
    const hasComposerPrompt = visiblePageText.includes('分享您的洞见') ||
        visiblePageText.includes('Share your insights');
    const hasPublishControl = visibleControls.some((text) =>
        text === '发文' || text === 'Post'
    );
    const accountNavLabels = ['个人主页', '聊天', '通知', '历史记录', 'Profile', 'Chat', 'Notifications', 'History'];
    const accountNavMatches = new Set(
        visibleControls.filter((text) => accountNavLabels.includes(text))
    );
    if (!hasLoginControl && hasComposerPrompt && hasPublishControl && accountNavMatches.size >= 2) {
        return {
            valid: true,
            signature_key: null,
            source: 'authenticated_square_ui',
            attempts,
            ui: {
                hasComposerPrompt,
                hasPublishControl,
                accountNavMatches: accountNavMatches.size
            }
        };
    }

    if (authPayload && authPayload.success) {
        return {valid: true, signature_key: null, source: 'auth_success', attempts};
    }
    const authMessage = authPayload && authPayload.message ? String(authPayload.message) : null;
    return {
        valid: false,
        signature_key: null,
        error: authMessage || authResult.error || 'Cookie 未登录或已失效',
        attempts
    };
}"""


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
        cookie_values = {
            item["name"]: item["value"]
            for item in BinanceAccountChecker._parse_cookie_header(cookie)
        }
        return {
            "accept": "application/json",
            "clienttype": "web",
            "content-type": "application/json",
            "cookie": cookie,
            "csrftoken": (
                cookie_values.get("cr00")
                or cookie_values.get("csrftoken")
                or cookie_values.get("csrfToken")
                or ""
            ),
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

    @staticmethod
    def _private_payload_has_data(value: Any) -> bool:
        if not isinstance(value, dict) or not value.get("success"):
            return False
        data = value.get("data")
        if isinstance(data, dict):
            return bool(data)
        if isinstance(data, list):
            return bool(data)
        return data is not None

    @staticmethod
    def probe_page_session(page: Any) -> AccountCheckResult:
        result = page.evaluate(
            BINANCE_PAGE_SESSION_PROBE,
            {
                "authPath": BINANCE_AUTH_PATH,
                "endpoints": [list(item) for item in BINANCE_USER_ENDPOINTS],
            },
        )
        if not isinstance(result, dict):
            return AccountCheckResult(
                valid=False,
                error="Binance 登录状态返回异常",
            )
        return AccountCheckResult(
            valid=bool(result.get("valid")),
            signature_key=result.get("signature_key"),
            error=result.get("error"),
            raw=result,
        )

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
                auth = client.post(f"{BINANCE_BASE_URL}{BINANCE_AUTH_PATH}")
                auth_json = auth.json()
                last_payload: dict[str, Any] = auth_json
                authenticated_by_private_endpoint = False
                for method, path, payload, is_private in BINANCE_USER_ENDPOINTS:
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
                    if is_private and self._private_payload_has_data(data):
                        authenticated_by_private_endpoint = True

                if auth_json.get("success") or authenticated_by_private_endpoint:
                    return AccountCheckResult(
                        valid=True,
                        error="Cookie 有效，但未从广场接口解析到 squareUid/signature_key",
                        raw=last_payload,
                    )

                browser_result = self._check_with_playwright(cookie, proxy_url=proxy)
                if browser_result.valid:
                    return browser_result
                return AccountCheckResult(
                    valid=False,
                    error=(
                        browser_result.error
                        or auth_json.get("message")
                        or "Cookie 未登录或已失效"
                    ),
                    raw=browser_result.raw or last_payload,
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
                result = self.probe_page_session(page)
                browser.close()
            return result
        except Exception as exc:
            return AccountCheckResult(valid=False, error=str(exc))
