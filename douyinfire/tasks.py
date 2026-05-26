from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from uuid import uuid4

from .browser import BrowserError, BrowserInterrupted, DouyinBrowser
from .config import AppConfig, ContactConfig, UserConfig, ensure_runtime_dirs
from .notify import notify


@dataclass(slots=True)
class ContactResult:
    contact: str
    success: bool
    reason: str = ""
    skipped: bool = False
    profile_url: str = ""


@dataclass(slots=True)
class UserRunResult:
    user: str
    started_at: str
    ended_at: str
    results: list[ContactResult]
    final_message_panel_screenshot: str = ""

    @property
    def success_count(self) -> int:
        return sum(1 for result in self.results if result.success)

    @property
    def failure_count(self) -> int:
        return sum(1 for result in self.results if not result.success and not result.skipped)


@dataclass(slots=True)
class RunResult:
    run_id: str
    started_at: str
    ended_at: str
    users: list[UserRunResult]

    @property
    def failure_count(self) -> int:
        return sum(user.failure_count for user in self.users)


def setup_logging(config: AppConfig) -> None:
    config.log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(config.log_dir / "douyinfire.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
        force=True,
    )


ProgressCallback = Callable[[str, str, str], None]
StopCallback = Callable[[], bool]


def run_all(config: AppConfig, progress: ProgressCallback | None = None, should_stop: StopCallback | None = None) -> RunResult:
    ensure_runtime_dirs(config)
    setup_logging(config)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
    started_at = _now()
    logging.info("Run started: %s", run_id)

    user_results: list[UserRunResult] = []
    for user in config.enabled_users:
        if should_stop and should_stop():
            logging.info("Run interrupted before user: %s", user.name)
            break
        user_results.append(run_user(config, user, run_id, progress=progress, should_stop=should_stop))
    result = RunResult(run_id=run_id, started_at=started_at, ended_at=_now(), users=user_results)
    _write_run_record(config, result)

    if result.failure_count >= config.failure_notify_threshold:
        notify("DouyinFire 需要处理", f"本次运行有 {result.failure_count} 个联系人发送失败，请查看日志和截图。")

    logging.info("Run finished: %s failures=%s", run_id, result.failure_count)
    return result


def run_user(
    config: AppConfig,
    user: UserConfig,
    run_id: str | None = None,
    progress: ProgressCallback | None = None,
    should_stop: StopCallback | None = None,
) -> UserRunResult:
    ensure_runtime_dirs(config)
    setup_logging(config)
    current_run = run_id or datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
    started_at = _now()
    results: list[ContactResult] = []
    screenshot_dir = config.screenshot_dir / current_run / user.name
    profile_dir = config.data_dir / "profiles" / user.name
    final_message_panel_screenshot = ""

    logging.info("User run started: %s", user.name)
    try:
        with DouyinBrowser(
            profile_dir=profile_dir,
            screenshot_dir=screenshot_dir,
            headless=config.headless,
            timeouts=config.timeouts,
            browser_config=config.browser,
            storage_state_path=_storage_state_path(config, user),
            mode="run",
            progress=progress,
            should_stop=should_stop,
        ) as browser:
            for index, contact in enumerate(user.contacts):
                if should_stop and should_stop():
                    logging.info("User run interrupted before contact user=%s contact=%s", user.name, contact.name)
                    _append_interrupted_results(results, user.contacts[index:])
                    break
                if _recently_sent(config, user, contact):
                    logging.info("Skipping duplicate send user=%s contact=%s", user.name, contact.name)
                    result = ContactResult(
                        contact=contact.name,
                        success=True,
                        reason="最近已发送，跳过重复",
                        skipped=True,
                        profile_url=contact.profile_url,
                    )
                else:
                    result = _send_contact(browser, user, contact, current_run)
                    if result.success and not result.skipped:
                        _record_sent(config, user, contact)
                results.append(result)
                if index < len(user.contacts) - 1:
                    if not _sleep_interruptible(config.schedule.min_contact_interval_seconds, should_stop):
                        logging.info("User run interrupted during contact interval user=%s", user.name)
                        _append_interrupted_results(results, user.contacts[index + 1 :])
                        break
            if not should_stop or not should_stop():
                try:
                    final_screenshot = browser.capture_final_message_panel(f"{current_run}_final_message_panel")
                    if final_screenshot.exists():
                        final_message_panel_screenshot = str(final_screenshot)
                except BrowserInterrupted:
                    logging.info("Final message panel screenshot skipped after interrupt user=%s", user.name)
                except Exception as exc:
                    logging.warning("Final message panel screenshot failed user=%s reason=%s", user.name, exc)
    except BrowserInterrupted:
        logging.info("Browser interrupted for user %s", user.name)
        remaining = [contact for contact in user.contacts if contact.name not in {item.contact for item in results}]
        _append_interrupted_results(results, remaining)
    except BrowserError as exc:
        logging.exception("Browser failed for user %s", user.name)
        remaining = [contact for contact in user.contacts if contact.name not in {item.contact for item in results}]
        results.extend(ContactResult(contact=contact.name, success=False, reason=str(exc), profile_url=contact.profile_url) for contact in remaining)
    except Exception as exc:
        logging.exception("Unexpected failure for user %s", user.name)
        remaining = [contact for contact in user.contacts if contact.name not in {item.contact for item in results}]
        results.extend(ContactResult(contact=contact.name, success=False, reason=str(exc), profile_url=contact.profile_url) for contact in remaining)

    user_result = UserRunResult(
        user=user.name,
        started_at=started_at,
        ended_at=_now(),
        results=results,
        final_message_panel_screenshot=final_message_panel_screenshot,
    )
    logging.info("User run finished: %s success=%s failure=%s", user.name, user_result.success_count, user_result.failure_count)
    return user_result


def login_user(config: AppConfig, user: UserConfig, wait_seconds: int | None = None) -> None:
    ensure_runtime_dirs(config)
    setup_logging(config)
    with DouyinBrowser(
        profile_dir=config.data_dir / "profiles" / user.name,
        screenshot_dir=config.screenshot_dir / "login" / user.name,
        headless=False,
        timeouts=config.timeouts,
        browser_config=config.browser,
        storage_state_path=_storage_state_path(config, user),
        mode="login",
    ) as browser:
        browser.login(wait_seconds=wait_seconds)
    notify("DouyinFire 登录态已保存", f"用户 {user.name} 的浏览器登录态已更新。")


def _send_contact(browser: DouyinBrowser, user: UserConfig, contact: ContactConfig, run_id: str) -> ContactResult:
    message = contact.message or user.message
    logging.info("Sending message user=%s contact=%s profile_url=%s", user.name, contact.name, contact.profile_url or "-")
    try:
        browser.send_message(contact.name, message, f"{run_id}_{contact.name}", profile_url=contact.profile_url)
        logging.info("Contact sent user=%s contact=%s", user.name, contact.name)
        return ContactResult(contact=contact.name, success=True, profile_url=contact.profile_url)
    except BrowserInterrupted:
        raise
    except Exception as exc:
        logging.warning("Contact failed user=%s contact=%s reason=%s", user.name, contact.name, exc)
        return ContactResult(contact=contact.name, success=False, reason=str(exc), profile_url=contact.profile_url)


def _append_interrupted_results(results: list[ContactResult], contacts: list[ContactConfig]) -> None:
    for contact in contacts:
        results.append(ContactResult(contact=contact.name, success=False, reason="已中断，未发送", skipped=True, profile_url=contact.profile_url))


def _sleep_interruptible(seconds: int, should_stop: StopCallback | None) -> bool:
    end_at = time.monotonic() + seconds
    while time.monotonic() < end_at:
        if should_stop and should_stop():
            return False
        time.sleep(min(0.2, end_at - time.monotonic()))
    return True


def _write_run_record(config: AppConfig, result: RunResult) -> Path:
    records_dir = config.data_dir / "runs"
    records_dir.mkdir(parents=True, exist_ok=True)
    path = records_dir / f"{result.run_id}.json"
    path.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _storage_state_path(config: AppConfig, user: UserConfig) -> Path:
    raw = str(config.browser.storage_state_path)
    if "{user}" in raw:
        return Path(raw.format(user=user.name))
    return config.browser.storage_state_path


def _recently_sent(config: AppConfig, user: UserConfig, contact: ContactConfig, cooldown_seconds: int = 180) -> bool:
    record = _sent_records(config)
    key = _sent_key(user, contact)
    value = record.get(key)
    if not value:
        return False
    try:
        sent_at = datetime.fromisoformat(value)
    except ValueError:
        return False
    return datetime.now() - sent_at < timedelta(seconds=cooldown_seconds)


def _record_sent(config: AppConfig, user: UserConfig, contact: ContactConfig) -> None:
    path = config.data_dir / "last_sent.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = _sent_records(config)
    record[_sent_key(user, contact)] = datetime.now().isoformat(timespec="seconds")
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def _sent_records(config: AppConfig) -> dict[str, str]:
    path = config.data_dir / "last_sent.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items()}


def _sent_key(user: UserConfig, contact: ContactConfig) -> str:
    return f"{user.name}:{contact.name}:{contact.profile_url}:{contact.message or user.message}"
