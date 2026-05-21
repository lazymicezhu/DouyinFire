from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

DOUYIN_URL = "https://www.douyin.com/"


class BrowserError(RuntimeError):
    """Raised for browser automation failures."""


class DouyinBrowser(AbstractContextManager["DouyinBrowser"]):
    def __init__(self, profile_dir: Path, screenshot_dir: Path, headless: bool = False) -> None:
        self.profile_dir = profile_dir
        self.screenshot_dir = screenshot_dir
        self.headless = headless
        self._playwright: Any = None
        self.context: Any = None
        self.page: Any = None

    def __enter__(self) -> "DouyinBrowser":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def start(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserError("Playwright is not installed. Run: pip install -r requirements.txt") from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        self.context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            viewport={"width": 1440, "height": 1000},
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(15_000)

    def close(self) -> None:
        if self.context is not None:
            self.context.close()
            self.context = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None
        self.page = None

    def login(self) -> None:
        page = self._require_page()
        page.goto(DOUYIN_URL, wait_until="domcontentloaded")
        print("浏览器已打开。请在页面中完成抖音登录；登录完成后回到终端按 Enter 保存登录态。")
        input()
        page.goto(DOUYIN_URL, wait_until="domcontentloaded")

    def send_message(self, contact: str, message: str, screenshot_prefix: str) -> None:
        page = self._require_page()
        page.goto(DOUYIN_URL, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle", timeout=20_000)

        try:
            self._open_messages(page)
            self._open_contact(page, contact)
            self._fill_and_send(page, message)
        except Exception as exc:
            self.screenshot(f"{screenshot_prefix}_failure")
            raise BrowserError(str(exc)) from exc

    def screenshot(self, name: str) -> Path:
        page = self._require_page()
        path = self.screenshot_dir / f"{name}.png"
        page.screenshot(path=str(path), full_page=True)
        return path

    def _require_page(self) -> Any:
        if self.page is None:
            raise BrowserError("Browser has not been started")
        return self.page

    def _open_messages(self, page: Any) -> None:
        candidates = [
            page.get_by_text("消息", exact=True),
            page.get_by_text("私信", exact=True),
            page.locator("a[href*='message']").first,
            page.locator("div[role='button']").filter(has_text="消息").first,
        ]
        _click_first_visible(candidates, "message entry")
        page.wait_for_timeout(1500)

    def _open_contact(self, page: Any, contact: str) -> None:
        candidates = [
            page.get_by_text(contact, exact=True),
            page.get_by_text(contact),
            page.locator(f"text={contact}").first,
        ]
        _click_first_visible(candidates, f"contact {contact}")
        page.wait_for_timeout(1000)

    def _fill_and_send(self, page: Any, message: str) -> None:
        candidates = [
            page.locator("textarea").last,
            page.locator("[contenteditable='true']").last,
            page.get_by_role("textbox").last,
        ]
        target = _first_visible(candidates, "message input")
        target.click()
        target.fill(message)
        target.press("Enter")
        page.wait_for_timeout(1000)


def _click_first_visible(candidates: list[Any], label: str) -> None:
    target = _first_visible(candidates, label)
    target.click()


def _first_visible(candidates: list[Any], label: str) -> Any:
    last_error: Exception | None = None
    for locator in candidates:
        try:
            if locator.count() > 0 and locator.first.is_visible():
                return locator.first
        except Exception as exc:
            last_error = exc
            try:
                if locator.is_visible():
                    return locator
            except Exception as nested:
                last_error = nested
    if last_error:
        raise BrowserError(f"Could not find visible {label}: {last_error}") from last_error
    raise BrowserError(f"Could not find visible {label}")
