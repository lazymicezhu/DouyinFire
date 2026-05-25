from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .browser import BrowserError, DouyinBrowser
from .config import AppConfig, UserConfig, ensure_runtime_dirs
from .notify import notify


@dataclass(slots=True)
class ContactResult:
    contact: str
    success: bool
    reason: str = ""


@dataclass(slots=True)
class UserRunResult:
    user: str
    started_at: str
    ended_at: str
    results: list[ContactResult]

    @property
    def success_count(self) -> int:
        return sum(1 for result in self.results if result.success)

    @property
    def failure_count(self) -> int:
        return sum(1 for result in self.results if not result.success)


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


def run_all(config: AppConfig) -> RunResult:
    ensure_runtime_dirs(config)
    setup_logging(config)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
    started_at = _now()
    logging.info("Run started: %s", run_id)

    user_results = [run_user(config, user, run_id) for user in config.enabled_users]
    result = RunResult(run_id=run_id, started_at=started_at, ended_at=_now(), users=user_results)
    _write_run_record(config, result)

    if result.failure_count >= config.failure_notify_threshold:
        notify("DouyinFire 需要处理", f"本次运行有 {result.failure_count} 个联系人发送失败，请查看日志和截图。")

    logging.info("Run finished: %s failures=%s", run_id, result.failure_count)
    return result


def run_user(config: AppConfig, user: UserConfig, run_id: str | None = None) -> UserRunResult:
    ensure_runtime_dirs(config)
    setup_logging(config)
    current_run = run_id or datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
    started_at = _now()
    results: list[ContactResult] = []
    screenshot_dir = config.screenshot_dir / current_run / user.name
    profile_dir = config.data_dir / "profiles" / user.name

    logging.info("User run started: %s", user.name)
    try:
        with DouyinBrowser(profile_dir=profile_dir, screenshot_dir=screenshot_dir, headless=config.headless) as browser:
            for contact in user.contacts:
                result = _send_contact(browser, user, contact, current_run)
                results.append(result)
                time.sleep(config.schedule.min_contact_interval_seconds)
    except BrowserError as exc:
        logging.exception("Browser failed for user %s", user.name)
        remaining = [contact for contact in user.contacts if contact not in {item.contact for item in results}]
        results.extend(ContactResult(contact=contact, success=False, reason=str(exc)) for contact in remaining)
    except Exception as exc:
        logging.exception("Unexpected failure for user %s", user.name)
        remaining = [contact for contact in user.contacts if contact not in {item.contact for item in results}]
        results.extend(ContactResult(contact=contact, success=False, reason=str(exc)) for contact in remaining)

    user_result = UserRunResult(user=user.name, started_at=started_at, ended_at=_now(), results=results)
    logging.info("User run finished: %s success=%s failure=%s", user.name, user_result.success_count, user_result.failure_count)
    return user_result


def login_user(config: AppConfig, user: UserConfig, wait_seconds: int | None = None) -> None:
    ensure_runtime_dirs(config)
    setup_logging(config)
    with DouyinBrowser(
        profile_dir=config.data_dir / "profiles" / user.name,
        screenshot_dir=config.screenshot_dir / "login" / user.name,
        headless=False,
    ) as browser:
        browser.login(wait_seconds=wait_seconds)
    notify("DouyinFire 登录态已保存", f"用户 {user.name} 的浏览器登录态已更新。")


def _send_contact(browser: DouyinBrowser, user: UserConfig, contact: str, run_id: str) -> ContactResult:
    logging.info("Sending message user=%s contact=%s", user.name, contact)
    try:
        browser.send_message(contact, user.message, f"{run_id}_{contact}")
        logging.info("Contact sent user=%s contact=%s", user.name, contact)
        return ContactResult(contact=contact, success=True)
    except Exception as exc:
        logging.warning("Contact failed user=%s contact=%s reason=%s", user.name, contact, exc)
        return ContactResult(contact=contact, success=False, reason=str(exc))


def _write_run_record(config: AppConfig, result: RunResult) -> Path:
    records_dir = config.data_dir / "runs"
    records_dir.mkdir(parents=True, exist_ok=True)
    path = records_dir / f"{result.run_id}.json"
    path.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
