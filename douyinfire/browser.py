from __future__ import annotations

import logging
import json
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Callable

from .config import BrowserConfig, TimeoutsConfig

DOUYIN_URL = "https://www.douyin.com/"


class BrowserError(RuntimeError):
    """Raised for browser automation failures."""


class DouyinBrowser(AbstractContextManager["DouyinBrowser"]):
    def __init__(
        self,
        profile_dir: Path,
        screenshot_dir: Path,
        headless: bool = False,
        timeouts: TimeoutsConfig | None = None,
        browser_config: BrowserConfig | None = None,
        storage_state_path: Path | None = None,
        mode: str = "run",
        progress: Callable[[str, str, str], None] | None = None,
    ) -> None:
        self.profile_dir = profile_dir
        self.screenshot_dir = screenshot_dir
        self.headless = headless
        self.timeouts = timeouts or TimeoutsConfig()
        self.browser_config = browser_config or BrowserConfig(run_headless=headless)
        self.storage_state_path = storage_state_path or self.browser_config.storage_state_path
        self.mode = mode
        self.progress = progress
        self._playwright: Any = None
        self.browser: Any = None
        self.context: Any = None
        self.page: Any = None

    def __enter__(self) -> "DouyinBrowser":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def start(self) -> None:
        if self.mode == "login":
            self._start_playwright_persistent(headless=self.browser_config.login_headless)
            return

        if self.browser_config.backend == "cloakbrowser":
            self._start_cloakbrowser()
        else:
            self._start_playwright_persistent(headless=self.browser_config.run_headless)

    def _start_playwright_persistent(self, headless: bool) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserError("Playwright is not installed. Run: pip install -r requirements.txt") from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        self.context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=headless,
            viewport={"width": 1440, "height": 1000},
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(max(self.timeouts.contact_search_seconds, self.timeouts.input_box_seconds) * 1000)

    def _start_cloakbrowser(self) -> None:
        try:
            from cloakbrowser import launch
        except ImportError as exc:
            raise BrowserError("请先 pip install cloakbrowser，或在配置中把 browser.backend 改为 playwright") from exc

        if not self.storage_state_path.exists():
            logging.info("Storage state missing; exporting from profile: %s", self.storage_state_path)
            self._export_storage_state_from_profile()

        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self.browser = launch(headless=self.browser_config.run_headless)
        self.context = self.browser.new_context(
            storage_state=str(self.storage_state_path),
            viewport={"width": 1440, "height": 1000},
        )
        self.page = self.context.new_page()
        self.page.set_default_timeout(max(self.timeouts.contact_search_seconds, self.timeouts.input_box_seconds) * 1000)

    def _export_storage_state_from_profile(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserError("Playwright is not installed. Run: pip install -r requirements.txt") from exc

        if not self.profile_dir.exists():
            raise BrowserError(f"未找到浏览器 profile: {self.profile_dir}。请先在 GUI 中执行登录。") from None

        playwright = sync_playwright().start()
        context = None
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=False,
                viewport={"width": 1440, "height": 1000},
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(DOUYIN_URL, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(3000)
            self.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(self.storage_state_path))
            logging.info("Storage state exported: %s", self.storage_state_path)
        finally:
            if context is not None:
                context.close()
            playwright.stop()

    def close(self) -> None:
        if self.context is not None:
            self.context.close()
            self.context = None
        if self.browser is not None:
            self.browser.close()
            self.browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None
        self.page = None

    def login(self, wait_seconds: int | None = None) -> None:
        page = self._require_page()
        page.goto(DOUYIN_URL, wait_until="domcontentloaded")
        if wait_seconds is None:
            print("浏览器已打开。请在页面中完成抖音登录；登录完成后回到终端按 Enter 保存登录态。")
            input()
        else:
            print(f"浏览器已打开。请在 {wait_seconds} 秒内完成抖音登录，时间到后会自动保存登录态。")
            page.wait_for_timeout(wait_seconds * 1000)
        page.goto(DOUYIN_URL, wait_until="domcontentloaded")
        self.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
        self.context.storage_state(path=str(self.storage_state_path))

    def send_message(self, contact: str, message: str, screenshot_prefix: str, profile_url: str = "") -> None:
        page = self._require_page()
        try:
            self._step_start("open_home", contact)
            page.goto(DOUYIN_URL, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(self.timeouts.home_ready_seconds * 1000)
            self.screenshot(f"{screenshot_prefix}_home")
            self._step_done("open_home", contact)

            self._step_start("open_messages", contact)
            self._open_messages(page)
            self.screenshot(f"{screenshot_prefix}_message_panel")
            self._step_done("open_messages", contact)

            self._step_start("open_conversation", contact)
            self._open_contact(page, contact, profile_url)
            self.screenshot(f"{screenshot_prefix}_conversation")
            self._step_done("open_conversation", contact)
            self._write_contact_metadata(screenshot_prefix, contact, profile_url)

            self._fill_and_send(page, message)
            self._step_start("done", contact)
            self._step_done("done", contact)
        except Exception as exc:
            self.screenshot(f"{screenshot_prefix}_failure")
            raise BrowserError(str(exc)) from exc

    def screenshot(self, name: str) -> Path:
        page = self._require_page()
        path = self.screenshot_dir / f"{name}.png"
        try:
            page.screenshot(path=str(path), full_page=True, timeout=5000)
        except Exception as exc:
            logging.warning("Screenshot failed name=%s reason=%s", name, exc)
        return path

    def _require_page(self) -> Any:
        if self.page is None:
            raise BrowserError("Browser has not been started")
        return self.page

    def _open_messages(self, page: Any) -> None:
        candidates = [
            page.get_by_text("消息", exact=True),
            page.get_by_text("私信", exact=True),
            page.get_by_label("消息"),
            page.get_by_label("私信"),
            page.locator("a[href*='message']").first,
            page.locator("a[href*='im']").first,
            page.locator("[href*='message']").first,
            page.locator("[href*='im']").first,
            page.locator("div[role='button']").filter(has_text="消息").first,
        ]
        _click_first_visible(candidates, "message entry", timeout_seconds=self.timeouts.message_panel_seconds)
        page.wait_for_timeout(self.timeouts.message_panel_seconds * 1000)

    def _open_contact(self, page: Any, contact: str, profile_url: str = "") -> None:
        profile_error: Exception | None = None
        if profile_url:
            try:
                self._open_contact_from_profile(page, contact, profile_url)
                self._step_done("contact_recent_list", "未使用最近会话兜底")
                return
            except Exception as exc:
                profile_error = exc
                logging.warning("Contact profile navigation failed contact=%s url=%s reason=%s; falling back to recent list", contact, profile_url, exc)
                self._step_done("profile_message_entry", f"主页私信入口失败，回退最近会话: {exc}")
                self.screenshot(f"profile_message_entry_failure_{_safe_name(contact)}")

        try:
            self._open_contact_from_recent_list(page, contact)
        except Exception as exc:
            if profile_url:
                raise BrowserError(f"未能通过主页链接或最近会话找到联系人 {contact}: profile={profile_error}; recent={exc}") from exc
            raise BrowserError(f"未能在最近会话中找到联系人 {contact}，且未配置主页链接: recent={exc}") from exc

    def _open_contact_from_profile(self, page: Any, contact: str, profile_url: str) -> None:
        self._step_start("open_contact_profile", profile_url)
        logging.info("Opening contact profile contact=%s url=%s", contact, profile_url)
        page.goto(profile_url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(self.timeouts.home_ready_seconds * 1000)
        self._step_done("open_contact_profile", page.url)

        self._step_start("profile_message_entry", contact)
        candidates = [
            page.get_by_text("私信", exact=True),
            page.get_by_text("消息", exact=True),
            page.get_by_text("聊天", exact=True),
            page.get_by_role("button", name="私信"),
            page.get_by_role("button", name="消息"),
            page.get_by_role("button", name="聊天"),
            page.locator("button").filter(has_text="私信").first,
            page.locator("button").filter(has_text="消息").first,
            page.locator("button").filter(has_text="聊天").first,
            page.locator("a").filter(has_text="私信").first,
            page.locator("a").filter(has_text="消息").first,
            page.locator("a").filter(has_text="聊天").first,
            page.locator("[href*='message']").first,
            page.locator("[href*='im']").first,
        ]
        _click_first_visible(candidates, f"profile message entry {contact}", timeout_seconds=self.timeouts.contact_search_seconds)
        page.wait_for_timeout(1000)
        self._step_done("profile_message_entry", contact)

    def _open_contact_from_recent_list(self, page: Any, contact: str) -> None:
        self._step_start("contact_recent_list", contact)
        candidates = [
            page.get_by_text(contact, exact=True),
            page.get_by_text(contact),
            page.locator(f"text={contact}").first,
        ]
        _click_first_visible(candidates, f"recent contact {contact}", timeout_seconds=self.timeouts.contact_search_seconds)
        page.wait_for_timeout(1000)
        self._step_done("contact_recent_list", contact)

    def _write_contact_metadata(self, screenshot_prefix: str, contact: str, profile_url: str) -> None:
        page = self._require_page()
        path = self.screenshot_dir / f"{screenshot_prefix}_contact.json"
        data = {
            "contact": contact,
            "profile_url": profile_url,
            "final_url": page.url,
            "title": "",
        }
        try:
            data["title"] = page.title()
        except Exception as exc:
            logging.warning("Could not read page title contact=%s reason=%s", contact, exc)
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logging.warning("Could not write contact metadata contact=%s reason=%s", contact, exc)

    def _fill_and_send(self, page: Any, message: str) -> None:
        self._step_start("input_box")
        candidates = [
            page.locator("textarea").last,
            page.locator("[contenteditable='true']").last,
            page.get_by_role("textbox").last,
        ]
        target = _first_visible(candidates, "message input", timeout_seconds=self.timeouts.input_box_seconds)
        target.click()
        self._step_done("input_box")

        self._step_start("input_message")
        target.fill(message)
        self._step_done("input_message")

        self._step_start("press_enter")
        target.press("Enter")
        page.wait_for_timeout(self.timeouts.after_send_seconds * 1000)
        self._step_done("press_enter")

    def _step_start(self, step: str, detail: str = "") -> None:
        logging.info("Step %s start %s", step, detail)
        if self.progress:
            self.progress(step, "running", detail)

    def _step_done(self, step: str, detail: str = "") -> None:
        logging.info("Step %s done %s", step, detail)
        if self.progress:
            self.progress(step, "done", detail)


def _click_first_visible(candidates: list[Any], label: str, timeout_seconds: int) -> None:
    target = _first_visible(candidates, label, timeout_seconds=timeout_seconds)
    target.click()


def _first_visible(candidates: list[Any], label: str, timeout_seconds: int) -> Any:
    return _first_visible_from_locators(candidates, label, timeout_seconds=timeout_seconds)


def _first_visible_from_locators(
    locators: list[Any],
    label: str,
    timeout_seconds: int,
    predicate: Callable[[Any], bool] | None = None,
) -> Any:
    last_error: Exception | None = None
    for locator in locators:
        try:
            count = locator.count()
            if count == 0:
                candidate = locator
                candidate.wait_for(state="visible", timeout=timeout_seconds * 1000)
                if predicate is None or predicate(candidate):
                    return candidate
                continue

            for index in range(count):
                candidate = locator.nth(index)
                try:
                    candidate.wait_for(state="visible", timeout=700)
                    if predicate is None or predicate(candidate):
                        return candidate
                except Exception as item_exc:
                    last_error = item_exc
        except Exception as exc:
            last_error = exc
    if last_error:
        raise BrowserError(f"Could not find visible {label}: {last_error}") from last_error
    raise BrowserError(f"Could not find visible {label}")


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in value)[:40] or "contact"
