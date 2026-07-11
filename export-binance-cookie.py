from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


PROJECT_DIR = Path(__file__).resolve().parent
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    str(PROJECT_DIR / "ms-playwright"),
)

from playwright.sync_api import sync_playwright


LOGIN_URL = "https://www.binance.com/zh-CN/login"
COOKIE_URLS = ["https://www.binance.com", "https://accounts.binance.com"]
REQUIRED_AUTH_COOKIES = {"p20t", "cr00"}
PROXY_URL = os.getenv("COOKIE_LOGIN_PROXY_URL", "").strip()


def copy_to_clipboard(value: str) -> None:
    if os.name == "nt":
        command = ["clip.exe"]
    elif sys.platform == "darwin":
        command = ["pbcopy"]
    else:
        raise RuntimeError("当前系统暂不支持自动复制，请使用 Windows 或 macOS 运行")
    subprocess.run(command, input=value, text=True, check=True)


def build_cookie_header(cookies: list[dict]) -> tuple[str, list[str]]:
    selected: list[tuple[str, str]] = []
    names: list[str] = []
    for cookie in cookies:
        domain = str(cookie.get("domain") or "").strip().lower().lstrip(".")
        if domain != "binance.com" and not domain.endswith(".binance.com"):
            continue
        name = str(cookie.get("name") or "").strip()
        value = cookie.get("value")
        if not name or value is None:
            continue
        names.append(name)
        selected.append((name, str(value)))
    return "; ".join(f"{name}={value}" for name, value in selected), names


def main() -> int:
    print("BN Square Agent - Binance Cookie 本地导出工具")
    print("Cookie 只会复制到本机剪贴板，不会上传、打印或写入文件。")
    if PROXY_URL:
        print("本次 Binance 登录将通过服务器代理出口，以匹配服务器发布环境。")
    print()

    with sync_playwright() as playwright:
        launch_options = {"headless": False}
        if PROXY_URL:
            launch_options["proxy"] = {"server": PROXY_URL}
        browser = playwright.chromium.launch(**launch_options)
        context = browser.new_context(locale="zh-CN")
        page = context.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)

        while True:
            print("请在弹出的 Binance 窗口完成登录，并确认能打开 Square、看到账号头像。")
            input("确认后回到本窗口，按回车提取 Cookie：")

            cookie_header, cookie_names = build_cookie_header(
                context.cookies(COOKIE_URLS)
            )
            missing = sorted(REQUIRED_AUTH_COOKIES.difference(cookie_names))
            if not cookie_header or missing:
                print(
                    "尚未读取到完整登录 Cookie"
                    + (f"，缺少：{', '.join(missing)}" if missing else "")
                    + "。请继续登录后再按回车。"
                )
                print()
                continue

            copy_to_clipboard(cookie_header)
            print()
            print(
                f"成功：已将 {len(cookie_header)} 字符、{len(cookie_names)} 个 Cookie "
                "复制到本机剪贴板。"
            )
            print("Cookie 已保存到本机剪贴板，仅在确实需要手工迁移时粘贴使用。")
            input("按回车关闭 Binance 窗口和本工具：")
            context.close()
            browser.close()
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
