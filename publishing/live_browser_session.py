from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..core.config import normalize_proxy_url, playwright_proxy_settings
from ..core.url_policy import validate_binance_url
from .account_check import BINANCE_BASE_URL, AccountCheckResult, BinanceAccountChecker
from .browser_profile import browser_profile_dir


SQUARE_HOME_URL = f"{BINANCE_BASE_URL}/zh-CN/square"
DEFAULT_LOGIN_URL = f"{BINANCE_BASE_URL}/zh-CN/login"


@dataclass
class LiveBrowserSession:
    session_id: str
    account_key: str
    name: str
    proxy_url: str
    profile_dir: Path
    playwright: Any
    context: Any
    page: Any
    created_at: str
    ready: bool = False
    last_check: AccountCheckResult | None = None


class LiveBrowserSessionManager:
    """Own long-lived Binance browser contexts inside the MCP process.

    All methods must be called from the same worker thread because Playwright's
    synchronous API is thread-affine. The MCP server enforces that invariant.
    """

    def __init__(self, *, headless: bool = False, timeout_ms: int = 90_000):
        self.headless = headless
        self.timeout_ms = timeout_ms
        self._sessions: dict[str, LiveBrowserSession] = {}

    @staticmethod
    def _cookie_header(cookies: list[dict[str, Any]]) -> str:
        selected: list[tuple[str, str]] = []
        for cookie in cookies:
            domain = str(cookie.get("domain") or "").strip().lower().lstrip(".")
            if domain != "binance.com" and not domain.endswith(".binance.com"):
                continue
            name = str(cookie.get("name") or "").strip()
            value = cookie.get("value")
            if not name or value is None:
                continue
            selected.append((name, str(value)))
        selected.sort(key=lambda item: item[0].lower())
        return "; ".join(f"{name}={value}" for name, value in selected)

    @staticmethod
    def _is_alive(session: LiveBrowserSession) -> bool:
        try:
            return not session.page.is_closed()
        except Exception:
            return False

    def _find_by_session_id(self, session_id: str) -> LiveBrowserSession | None:
        return next(
            (
                session
                for session in self._sessions.values()
                if session.session_id == session_id
            ),
            None,
        )

    def start_login(
        self,
        *,
        account_key: str,
        name: str = "",
        proxy_url: str = "",
        login_url: str = DEFAULT_LOGIN_URL,
    ) -> dict[str, Any]:
        key = account_key.strip()
        if not key:
            raise ValueError("账号标识不能为空")
        login_url = validate_binance_url(login_url, label="登录地址")
        proxy = normalize_proxy_url(proxy_url) if proxy_url.strip() else ""

        existing = self._sessions.get(key)
        if existing and self._is_alive(existing):
            if existing.proxy_url != proxy:
                raise RuntimeError("该账号已有常驻浏览器，会话代理不可在运行中切换")
            try:
                existing.page.bring_to_front()
            except Exception:
                pass
            return self._public_status(existing, message="已恢复该账号的常驻登录窗口")
        if existing:
            self._close_session(existing)
            self._sessions.pop(key, None)

        from playwright.sync_api import sync_playwright

        profile_dir = browser_profile_dir(key)
        for singleton_file in profile_dir.glob("Singleton*"):
            try:
                singleton_file.unlink()
            except OSError:
                pass

        playwright = None
        context = None
        try:
            playwright = sync_playwright().start()
            launch_options: dict[str, Any] = {
                "headless": self.headless,
                "locale": "zh-CN",
                "viewport": {"width": 1440, "height": 960},
                "args": ["--disable-blink-features=AutomationControlled"],
                "ignore_default_args": ["--enable-automation"],
            }
            proxy_settings = playwright_proxy_settings(proxy) if proxy else None
            if proxy_settings:
                launch_options["proxy"] = proxy_settings
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir),
                **launch_options,
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(login_url, wait_until="domcontentloaded", timeout=60_000)
            validate_binance_url(page.url, label="登录页重定向地址")
            session = LiveBrowserSession(
                session_id=uuid4().hex,
                account_key=key,
                name=(name or key).strip() or key,
                proxy_url=proxy,
                profile_dir=profile_dir,
                playwright=playwright,
                context=context,
                page=page,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            self._sessions[key] = session
            return self._public_status(
                session,
                message="登录窗口已打开；确认登录后浏览器会保持运行，供 MCP 持续发布",
            )
        except Exception:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if playwright is not None:
                try:
                    playwright.stop()
                except Exception:
                    pass
            raise

    def finish_login(self, session_id: str) -> dict[str, Any]:
        session = self._find_by_session_id(session_id)
        if session is None or not self._is_alive(session):
            raise KeyError("登录会话不存在或浏览器已关闭")

        cookies = session.context.cookies(
            ["https://www.binance.com", "https://accounts.binance.com"]
        )
        cookie_header = self._cookie_header(cookies)
        if not cookie_header:
            raise RuntimeError("未读取到 Binance Cookie，请确认已完成登录")

        try:
            if "/square" not in str(session.page.url):
                session.page.goto(
                    SQUARE_HOME_URL,
                    wait_until="domcontentloaded",
                    timeout=self.timeout_ms,
                )
        except Exception:
            # Navigation may still be settling after QR login. The probe below
            # retries against the same live page and reports the useful error.
            pass

        check = self._probe_with_retries(session.page)
        session.last_check = check
        if not check.valid:
            raise RuntimeError(check.error or "Binance 登录状态尚未生效")
        session.ready = True
        result = self._public_status(session, message="登录已确认，常驻浏览器会话保持运行")
        result.update(
            {
                "name": session.name,
                "proxy_url": session.proxy_url,
                "cookie_header": cookie_header,
                "cookie_length": len(cookie_header),
                "cookie_names": [
                    item["name"]
                    for item in BinanceAccountChecker._parse_cookie_header(cookie_header)
                ],
                "signature_key": check.signature_key,
            }
        )
        return result

    def status(self, account_key: str) -> dict[str, Any]:
        key = account_key.strip()
        session = self._sessions.get(key)
        if session is None:
            return {
                "account_key": key,
                "active": False,
                "ready": False,
                "valid": False,
                "error": "该账号没有正在运行的浏览器会话，请重新登录",
            }
        if not self._is_alive(session):
            self._close_session(session)
            self._sessions.pop(key, None)
            return {
                "account_key": key,
                "active": False,
                "ready": False,
                "valid": False,
                "error": "该账号的浏览器窗口已关闭，请重新登录",
            }
        check = self._probe_with_retries(session.page)
        session.last_check = check
        session.ready = bool(check.valid)
        return self._public_status(session)

    def get_ready_page(self, account_key: str) -> Any:
        session = self._sessions.get(account_key.strip())
        if session is None or not self._is_alive(session):
            raise RuntimeError("账号没有正在运行的浏览器会话，请先登录")
        if not session.ready:
            check = self._probe_with_retries(session.page)
            session.last_check = check
            session.ready = bool(check.valid)
        if not session.ready:
            raise RuntimeError(
                session.last_check.error
                if session.last_check and session.last_check.error
                else "账号登录状态无效，请重新登录"
            )
        return session.page

    def has_session(self, account_key: str) -> bool:
        session = self._sessions.get(account_key.strip())
        return bool(session and self._is_alive(session))

    def close(self, *, account_key: str = "", session_id: str = "") -> bool:
        session = (
            self._sessions.get(account_key.strip())
            if account_key.strip()
            else self._find_by_session_id(session_id)
        )
        if session is None:
            return False
        self._sessions.pop(session.account_key, None)
        self._close_session(session)
        return True

    def close_all(self) -> None:
        sessions = list(self._sessions.values())
        self._sessions.clear()
        for session in sessions:
            self._close_session(session)

    def session_count(self) -> int:
        return sum(1 for session in self._sessions.values() if self._is_alive(session))

    def _probe_with_retries(self, page: Any) -> AccountCheckResult:
        last_error: Exception | None = None
        for _ in range(5):
            try:
                return BinanceAccountChecker.probe_page_session(page)
            except Exception as exc:
                last_error = exc
                try:
                    page.wait_for_timeout(750)
                except Exception:
                    break
        return AccountCheckResult(
            valid=False,
            error=f"Binance 登录状态检测失败: {last_error}",
        )

    @staticmethod
    def _close_session(session: LiveBrowserSession) -> None:
        try:
            session.context.close()
        except Exception:
            pass
        try:
            session.playwright.stop()
        except Exception:
            pass

    @staticmethod
    def _public_status(
        session: LiveBrowserSession,
        *,
        message: str | None = None,
    ) -> dict[str, Any]:
        check = session.last_check
        result: dict[str, Any] = {
            "session_id": session.session_id,
            "account_key": session.account_key,
            "active": True,
            "ready": session.ready,
            "valid": bool(check.valid) if check else session.ready,
            "signature_key": check.signature_key if check else None,
            "error": check.error if check and not check.valid else None,
            "created_at": session.created_at,
        }
        if message:
            result["message"] = message
        return result
