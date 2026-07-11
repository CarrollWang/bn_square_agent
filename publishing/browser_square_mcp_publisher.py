from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import datetime, timezone
import base64
import binascii
import json
import logging
from pathlib import Path
import re
import tempfile
from typing import Any, Pattern
from urllib.parse import urlparse
import uuid

from ..core.config import mask_url_credentials, playwright_proxy_settings
from .account_check import BINANCE_BASE_URL, BinanceAccountChecker
from .browser_profile import browser_profile_path


LOGGER = logging.getLogger(__name__)
SQUARE_HOME_URL = f"{BINANCE_BASE_URL}/zh-CN/square"
EDITOR_SELECTORS = (
    "textarea",
    "[contenteditable='true']",
    "[role='textbox']",
    ".ProseMirror",
)
COMPOSE_PATTERNS = (
    re.compile(r"发.?布", re.I),
    re.compile(r"写.?文", re.I),
    re.compile(r"创.?作", re.I),
    re.compile(r"post", re.I),
    re.compile(r"publish", re.I),
    re.compile(r"create", re.I),
)
PUBLISH_PATTERNS = (
    re.compile(r"发.?布", re.I),
    re.compile(r"post", re.I),
    re.compile(r"publish", re.I),
    re.compile(r"share", re.I),
)
SUCCESS_PATTERNS = (
    re.compile(r"发布成功", re.I),
    re.compile(r"已发布", re.I),
    re.compile(r"success", re.I),
    re.compile(r"published", re.I),
)
FAILURE_PATTERNS = (
    re.compile(r"发布失败", re.I),
    re.compile(r"请稍后再试", re.I),
    re.compile(r"网络异常", re.I),
    re.compile(r"error", re.I),
    re.compile(r"failed", re.I),
)
UPLOAD_PATTERNS = (
    re.compile(r"图片", re.I),
    re.compile(r"上传", re.I),
    re.compile(r"image", re.I),
    re.compile(r"upload", re.I),
)
NETWORK_SUCCESS_HINTS = ("publish", "post", "create", "content", "article", "square")
NETWORK_CAPTURE_LIMIT = 120


@dataclass(frozen=True)
class BrowserPublishResult:
    success: bool
    message: str
    post_url: str | None = None
    debug_artifact: str | None = None


@dataclass
class PublishDiagnostics:
    attempt_id: str
    started_at: str
    network_events: list[dict[str, Any]] = field(default_factory=list)
    console_events: list[dict[str, Any]] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    request_failures: list[dict[str, Any]] = field(default_factory=list)


class BrowserBinanceSquarePublisher:
    def __init__(
        self,
        *,
        timeout_ms: int = 90_000,
        render_wait_ms: int = 2_000,
        publish_wait_ms: int = 12_000,
        debug_dir: str | Path = "./data/mcp_debug",
    ):
        self.timeout_ms = timeout_ms
        self.render_wait_ms = render_wait_ms
        self.publish_wait_ms = publish_wait_ms
        self.debug_dir = Path(debug_dir)
        self.debug_dir.mkdir(parents=True, exist_ok=True)

    def publish(
        self,
        *,
        cookie: str,
        content: str,
        account_key: str = "",
        coins: str = "",
        image_base64: str = "",
        proxy_url: str = "",
    ) -> BrowserPublishResult:
        profile_path = browser_profile_path(account_key) if account_key.strip() else None
        profile_dir = profile_path if profile_path is not None and profile_path.exists() else None
        if not cookie.strip() and profile_dir is None:
            return BrowserPublishResult(False, "缺少 cookie 或 account_key")
        if not content.strip():
            return BrowserPublishResult(False, "缺少 content")
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            launch_args: dict[str, Any] = {
                "headless": True,
                "args": ["--disable-blink-features=AutomationControlled"],
            }
            proxy = playwright_proxy_settings(proxy_url) if proxy_url else None
            if proxy:
                launch_args["proxy"] = proxy
            browser = None
            if profile_dir is not None:
                context = playwright.chromium.launch_persistent_context(
                    str(profile_dir),
                    locale="zh-CN",
                    viewport={"width": 1440, "height": 960},
                    **launch_args,
                )
            else:
                browser = playwright.chromium.launch(**launch_args)
                context = browser.new_context(
                    locale="zh-CN",
                    viewport={"width": 1440, "height": 960},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/125.0.0.0 Safari/537.36"
                    ),
                )
            try:
                cookies = BinanceAccountChecker._parse_cookie_header(cookie)
                if cookies:
                    context.add_cookies(cookies)
                page = context.pages[0] if context.pages else context.new_page()
                return self.publish_in_page(
                    page=page,
                    content=content,
                    coins=coins,
                    image_base64=image_base64,
                    proxy_url=proxy_url,
                )
            finally:
                context.close()
                if browser is not None:
                    browser.close()

    def publish_in_page(
        self,
        *,
        page: Any,
        content: str,
        coins: str = "",
        image_base64: str = "",
        proxy_url: str = "",
    ) -> BrowserPublishResult:
        """Publish through an already authenticated, still-running page."""
        if not content.strip():
            return BrowserPublishResult(False, "缺少 content")
        content = self._ensure_coin_reference(content, coins)
        diagnostics = PublishDiagnostics(
            attempt_id=f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}",
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        temp_image_path: Path | None = None
        try:
            if image_base64.strip():
                temp_image_path = self._write_temp_image(image_base64)
            self._attach_debug_listeners(page, diagnostics)
            page.goto(
                SQUARE_HOME_URL,
                wait_until="domcontentloaded",
                timeout=self.timeout_ms,
            )
            self._dismiss_popups(page)
            self._ensure_logged_in(page)
            self._open_compose_surface(page)
            self._fill_editor(page, content)
            if temp_image_path is not None:
                self._upload_image(page, temp_image_path)
            return self._publish_post(page, diagnostics)
        except Exception as exc:
            debug_artifact = self._debug_capture_bundle(
                page,
                diagnostics,
                prefix="publish_failed",
                extra={
                    "exception": str(exc),
                    "proxy_url": self.masked_proxy(proxy_url),
                    "live_session": True,
                },
            )
            LOGGER.exception("Binance Square live-session publish failed")
            return BrowserPublishResult(
                False,
                f"发布失败: {exc}",
                debug_artifact=debug_artifact,
            )
        finally:
            if temp_image_path is not None:
                temp_image_path.unlink(missing_ok=True)

    def _attach_debug_listeners(self, page, diagnostics: PublishDiagnostics) -> None:
        def append_limited(items: list[Any], item: Any, limit: int = NETWORK_CAPTURE_LIMIT) -> None:
            items.append(item)
            if len(items) > limit:
                del items[:-limit]

        def handle_console(message) -> None:
            try:
                append_limited(
                    diagnostics.console_events,
                    {
                        "type": message.type,
                        "text": message.text,
                    },
                )
            except Exception:
                return

        def handle_page_error(error: BaseException) -> None:
            append_limited(diagnostics.page_errors, str(error))

        def handle_request_failed(request) -> None:
            try:
                append_limited(
                    diagnostics.request_failures,
                    {
                        "url": request.url,
                        "method": request.method,
                        "resource_type": request.resource_type,
                        "failure": str(request.failure),
                    },
                )
            except Exception:
                return

        def handle_response(response) -> None:
            try:
                request = response.request
                event: dict[str, Any] = {
                    "url": response.url,
                    "method": request.method,
                    "status": response.status,
                    "resource_type": request.resource_type,
                    "ok": response.ok,
                }
                content_type = response.headers.get("content-type", "")
                if (
                    "application/json" in content_type
                    and ("/bapi/" in response.url or request.method.upper() != "GET")
                ):
                    try:
                        preview = response.text()
                        if preview:
                            event["preview"] = preview[:2_000]
                    except Exception:
                        pass
                append_limited(diagnostics.network_events, event)
            except Exception:
                return

        page.on("console", handle_console)
        page.on("pageerror", handle_page_error)
        page.on("requestfailed", handle_request_failed)
        page.on("response", handle_response)

    def _ensure_logged_in(self, page) -> None:
        result = BinanceAccountChecker.probe_page_session(page)
        if not result.valid:
            raise RuntimeError(result.error or "Cookie 未登录或已失效")

    def _open_compose_surface(self, page) -> None:
        if self._has_editor(page):
            return
        for pattern in COMPOSE_PATTERNS:
            if self._click_visible_candidate(page, pattern):
                page.wait_for_timeout(self.render_wait_ms)
                self._dismiss_popups(page)
                if self._has_editor(page):
                    return
        raise RuntimeError("未找到发布编辑器，请检查 Binance Square 页面结构是否变化")

    def _fill_editor(self, page, content: str) -> None:
        for selector in EDITOR_SELECTORS:
            locator = page.locator(selector).first
            try:
                if locator.count() < 1 or not locator.is_visible(timeout=1_000):
                    continue
                locator.click(timeout=3_000)
                if selector == "textarea":
                    locator.fill(content, timeout=5_000)
                else:
                    locator.evaluate(
                        """(el, value) => {
                            el.focus();
                            if ('value' in el) {
                                el.value = value;
                                el.dispatchEvent(new Event('input', {bubbles: true}));
                                el.dispatchEvent(new Event('change', {bubbles: true}));
                                return;
                            }
                            el.innerHTML = '';
                            document.execCommand('insertText', false, value);
                            if (!el.textContent || el.textContent.trim() !== value.trim()) {
                                el.textContent = value;
                            }
                            el.dispatchEvent(new Event('input', {bubbles: true}));
                        }""",
                        content,
                    )
                page.wait_for_timeout(800)
                return
            except Exception:
                continue
        raise RuntimeError("未找到可输入的编辑器")

    def _upload_image(self, page, image_path: Path) -> None:
        input_locator = page.locator("input[type='file']").first
        try:
            if input_locator.count() > 0:
                input_locator.set_input_files(str(image_path), timeout=10_000)
                self._wait_for_upload_settle(page)
                return
        except Exception:
            pass

        for pattern in UPLOAD_PATTERNS:
            try:
                with page.expect_file_chooser(timeout=5_000) as chooser_info:
                    if not self._click_visible_candidate(page, pattern):
                        continue
                chooser_info.value.set_files(str(image_path))
                self._wait_for_upload_settle(page)
                return
            except Exception:
                continue
        raise RuntimeError("未找到图片上传入口")

    def _wait_for_upload_settle(self, page) -> None:
        page.wait_for_timeout(2_000)
        for _ in range(20):
            busy = False
            for pattern in (
                re.compile(r"上传中", re.I),
                re.compile(r"uploading", re.I),
                re.compile(r"处理中", re.I),
            ):
                try:
                    if page.get_by_text(pattern).first.is_visible(timeout=300):
                        busy = True
                        break
                except Exception:
                    continue
            if not busy:
                return
            page.wait_for_timeout(500)

    def _publish_post(
        self,
        page,
        diagnostics: PublishDiagnostics,
    ) -> BrowserPublishResult:
        for pattern in PUBLISH_PATTERNS:
            locator = self._find_visible_candidate(page, pattern)
            if locator is None:
                continue
            self._wait_for_candidate_ready(page, locator)
            event_start = len(diagnostics.network_events)
            before_url = page.url
            self._safe_click(locator)
            page.wait_for_timeout(500)
            outcome = self._wait_for_publish_outcome(
                page,
                diagnostics,
                before_url=before_url,
                event_start=event_start,
            )
            if outcome is not None:
                return outcome
        debug_artifact = self._debug_capture_bundle(
            page,
            diagnostics,
            prefix="publish_uncertain",
            extra={"reason": "publish_button_clicked_but_no_outcome"},
        )
        return BrowserPublishResult(
            False,
            "点击发布后未检测到成功或失败信号，请查看调试包",
            debug_artifact=debug_artifact,
        )

    def _wait_for_publish_outcome(
        self,
        page,
        diagnostics: PublishDiagnostics,
        *,
        before_url: str,
        event_start: int,
    ) -> BrowserPublishResult | None:
        waited = 0
        while waited < self.publish_wait_ms:
            self._dismiss_popups(page)
            failure = self._detect_publish_failure(page, diagnostics, event_start)
            if failure:
                debug_artifact = self._debug_capture_bundle(
                    page,
                    diagnostics,
                    prefix="publish_rejected",
                    extra={"reason": failure},
                )
                return BrowserPublishResult(
                    False,
                    failure,
                    debug_artifact=debug_artifact,
                )
            if self._detect_publish_success(page, diagnostics, before_url, event_start):
                return BrowserPublishResult(
                    True,
                    self._success_message(diagnostics, event_start),
                    post_url=self._extract_post_url(page, diagnostics, before_url, event_start),
                )
            page.wait_for_timeout(500)
            waited += 500
        return None

    def _detect_publish_success(
        self,
        page,
        diagnostics: PublishDiagnostics,
        before_url: str,
        event_start: int,
    ) -> bool:
        for pattern in SUCCESS_PATTERNS:
            try:
                if page.get_by_text(pattern).first.is_visible(timeout=300):
                    return True
            except Exception:
                continue

        if self._network_indicates_success(diagnostics, event_start):
            return True

        try:
            if page.url != before_url and "/square" in page.url and not self._has_editor(page):
                return True
        except Exception:
            pass

        try:
            if not self._has_editor(page):
                locator = self._find_visible_candidate(page, re.compile(r"发.?布", re.I))
                if locator is None:
                    return True
        except Exception:
            pass
        return False

    def _detect_publish_failure(
        self,
        page,
        diagnostics: PublishDiagnostics,
        event_start: int,
    ) -> str | None:
        for pattern in FAILURE_PATTERNS:
            try:
                locator = page.get_by_text(pattern).first
                if locator.count() > 0 and locator.is_visible(timeout=300):
                    text = locator.text_content(timeout=300) or "页面提示发布失败"
                    return text.strip()
            except Exception:
                continue

        network_failure = self._network_failure_message(diagnostics, event_start)
        if network_failure:
            return network_failure

        if diagnostics.page_errors:
            return f"页面脚本异常: {diagnostics.page_errors[-1]}"
        return None

    def _network_indicates_success(
        self,
        diagnostics: PublishDiagnostics,
        event_start: int,
    ) -> bool:
        for event in diagnostics.network_events[event_start:]:
            preview = str(event.get("preview") or "")
            url = str(event.get("url") or "").lower()
            method = str(event.get("method") or "").upper()
            status = int(event.get("status") or 0)
            if method == "GET" and not preview:
                continue
            if not any(hint in url for hint in NETWORK_SUCCESS_HINTS):
                continue
            if status and status >= 400:
                continue
            lowered = preview.lower()
            if '"success":true' in lowered or '"success": true' in lowered:
                return True
        return False

    def _network_failure_message(
        self,
        diagnostics: PublishDiagnostics,
        event_start: int,
    ) -> str | None:
        for event in reversed(diagnostics.network_events[event_start:]):
            url = str(event.get("url") or "")
            lowered_url = url.lower()
            if not any(hint in lowered_url for hint in NETWORK_SUCCESS_HINTS):
                continue
            status = int(event.get("status") or 0)
            preview = str(event.get("preview") or "")
            if status >= 400:
                return f"发布请求失败 HTTP {status}: {url}"
            lowered = preview.lower()
            if '"success":false' in lowered or '"success": false' in lowered:
                return self._extract_error_message(preview) or "发布接口返回 success=false"

        for failure in reversed(diagnostics.request_failures):
            url = str(failure.get("url") or "")
            if any(hint in url.lower() for hint in NETWORK_SUCCESS_HINTS):
                return f"发布请求失败: {failure.get('failure') or url}"
        return None

    @staticmethod
    def _extract_error_message(preview: str) -> str | None:
        try:
            payload = json.loads(preview)
        except Exception:
            payload = None
        if isinstance(payload, dict):
            for key in ("message", "msg", "error", "detail"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        match = re.search(r'"message"\s*:\s*"([^"]+)"', preview)
        if match:
            return match.group(1)
        return None

    def _success_message(
        self,
        diagnostics: PublishDiagnostics,
        event_start: int,
    ) -> str:
        if self._network_indicates_success(diagnostics, event_start):
            return "发布成功，已检测到接口成功响应"
        return "发布成功"

    def _extract_post_url(
        self,
        page,
        diagnostics: PublishDiagnostics,
        before_url: str,
        event_start: int,
    ) -> str | None:
        try:
            if page.url != before_url and "/square" in page.url:
                return page.url
        except Exception:
            pass

        for event in reversed(diagnostics.network_events[event_start:]):
            preview = str(event.get("preview") or "")
            for candidate in re.findall(r"https?://[^\s\"']+", preview):
                parsed = urlparse(candidate)
                if "binance.com" in parsed.netloc and "/square" in parsed.path:
                    return candidate
        return None

    def _find_visible_candidate(self, page, pattern: Pattern[str]):
        for role in ("button", "link"):
            try:
                locator = page.get_by_role(role, name=pattern).first
                if locator.count() > 0 and locator.is_visible(timeout=800):
                    return locator
            except Exception:
                continue
        try:
            locator = page.get_by_text(pattern).first
            if locator.count() > 0 and locator.is_visible(timeout=800):
                return locator
        except Exception:
            return None
        return None

    def _click_visible_candidate(self, page, pattern: Pattern[str]) -> bool:
        locator = self._find_visible_candidate(page, pattern)
        if locator is None:
            return False
        self._safe_click(locator)
        return True

    def _wait_for_candidate_ready(self, page, locator) -> None:
        waited = 0
        while waited < 10_000:
            try:
                disabled = locator.evaluate(
                    """(el) => {
                        const ariaDisabled = (el.getAttribute('aria-disabled') || '').toLowerCase();
                        const cls = String(el.className || '').toLowerCase();
                        return Boolean(el.disabled) || ariaDisabled === 'true' || cls.includes('disabled');
                    }"""
                )
                if not disabled:
                    return
            except Exception:
                return
            page.wait_for_timeout(300)
            waited += 300

    @staticmethod
    def _safe_click(locator) -> None:
        try:
            locator.scroll_into_view_if_needed(timeout=1_000)
        except Exception:
            pass
        try:
            locator.click(timeout=3_000)
            return
        except Exception:
            pass
        try:
            locator.click(timeout=3_000, force=True)
            return
        except Exception:
            pass
        locator.evaluate("(el) => el.click()")

    def _has_editor(self, page) -> bool:
        for selector in EDITOR_SELECTORS:
            locator = page.locator(selector).first
            try:
                if locator.count() > 0 and locator.is_visible(timeout=500):
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _dismiss_popups(page) -> None:
        for text in (
            "接受",
            "同意",
            "Accept",
            "I Understand",
            "我知道了",
            "稍后再说",
            "Later",
            "关闭",
            "Close",
        ):
            try:
                button = page.get_by_text(text).first
                if button.count() and button.is_visible(timeout=300):
                    button.click(timeout=800)
            except Exception:
                continue

    def _debug_capture_bundle(
        self,
        page,
        diagnostics: PublishDiagnostics,
        *,
        prefix: str,
        extra: dict[str, Any] | None = None,
    ) -> str | None:
        bundle_dir = self.debug_dir / f"{prefix}_{diagnostics.attempt_id}"
        try:
            bundle_dir.mkdir(parents=True, exist_ok=True)
            screenshot_path = bundle_dir / "page.png"
            html_path = bundle_dir / "page.html"
            meta_path = bundle_dir / "diagnostics.json"
            page.screenshot(path=str(screenshot_path), full_page=True)
            html_path.write_text(page.content(), encoding="utf-8")
            metadata = {
                "attempt_id": diagnostics.attempt_id,
                "started_at": diagnostics.started_at,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "page_url": page.url,
                "title": page.title(),
                "network_events": diagnostics.network_events,
                "console_events": diagnostics.console_events,
                "page_errors": diagnostics.page_errors,
                "request_failures": diagnostics.request_failures,
                "extra": extra or {},
            }
            meta_path.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return str(bundle_dir)
        except Exception:
            LOGGER.exception("Failed to write MCP debug bundle")
            return None

    @staticmethod
    def _write_temp_image(image_base64: str) -> Path:
        raw = image_base64.strip()
        suffix = ".png"
        if raw.startswith("data:"):
            header, _, raw = raw.partition(",")
            if "jpeg" in header or "jpg" in header:
                suffix = ".jpg"
            elif "webp" in header:
                suffix = ".webp"
        try:
            binary = base64.b64decode(raw, validate=True)
        except binascii.Error as exc:
            raise RuntimeError("image_base64 不是合法的 base64 图片") from exc
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
            handle.write(binary)
            return Path(handle.name)

    @staticmethod
    def _ensure_coin_reference(content: str, coins: str) -> str:
        raw = coins.strip()
        if not raw or ":" not in raw:
            return content
        token = raw.split(":", 1)[0].strip().upper()
        if not token or re.search(rf"\${re.escape(token)}\b", content, re.I):
            return content
        return f"{content.rstrip()}\n\n${token}"

    @staticmethod
    def masked_proxy(proxy_url: str) -> str:
        return mask_url_credentials(proxy_url)
