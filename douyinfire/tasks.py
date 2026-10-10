"""One execution path for GUI, scheduled work and explicit retries."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from .browser import BrowserError, BrowserInterrupted, DouyinBrowser, LoginRequired, SendUncertain, validate_contact_url
from .config import AppConfig, ContactConfig, UserConfig, ensure_runtime_dirs
from .identities import group_conversation_id, validate_contact_identity
from .ledger import Ledger
from .locking import FileLock


@dataclass(slots=True)
class ContactResult:
    contact: str
    success: bool
    reason: str = ''
    skipped: bool = False
    profile_url: str = ''
    status: str = 'failed'
    contact_id: str = ''
    attempt_id: str = ''


@dataclass(slots=True)
class UserRunResult:
    user: str
    started_at: str
    ended_at: str
    results: list[ContactResult]
    final_message_panel_screenshot: str = ''
    user_id: str = ''

    @property
    def success_count(self):
        return sum(r.status == 'confirmed' and not r.skipped for r in self.results)

    @property
    def failure_count(self):
        return sum(r.status in {'failed', 'uncertain'} for r in self.results)


@dataclass(slots=True)
class RunResult:
    run_id: str
    started_at: str
    ended_at: str
    users: list[UserRunResult]

    @property
    def failure_count(self):
        return sum(u.failure_count for u in self.users)


ProgressCallback = Callable[[str, str, str], None]
StopCallback = Callable[[], bool]


def setup_logging(config: AppConfig) -> None:
    config.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = config.log_dir / 'douyinfire.log'
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                        handlers=[logging.FileHandler(path, encoding='utf-8')], force=True)
    path.chmod(0o600)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


def _day(config: AppConfig) -> str:
    return datetime.now(ZoneInfo(config.schedule.timezone)).date().isoformat()


def _account_lock(config, user):
    return FileLock(config.data_dir / 'locks' / f'account-{user.id}.lock')


def _browser(config, user, run_id, *, mode='run', progress=None, should_stop=None, expected=None):
    return DouyinBrowser(
        profile_dir=config.data_dir / 'profiles' / user.id,
        screenshot_dir=config.screenshot_dir / run_id / user.id,
        headless=True, timeouts=config.timeouts, browser_config=config.browser,
        storage_state_path=config.data_dir / 'states' / f'{user.id}.json',
        mode=mode, progress=progress, should_stop=should_stop,
        expected_account_id=user.identity if expected is None else expected,
    )


def login_user(config: AppConfig, user: UserConfig, wait_seconds: int | None = 180,
               progress=None, should_stop=None) -> dict:
    ensure_runtime_dirs(config)
    setup_logging(config)
    ledger = Ledger(config.data_dir)
    with _account_lock(config, user):
        try:
            with _browser(config, user, 'login', mode='login', progress=progress, should_stop=should_stop) as browser:
                identity = browser.login(wait_seconds=wait_seconds or 180)
            if should_stop and should_stop():
                raise BrowserInterrupted('已取消登录')
            # The actual silent runtime must accept the same account before showing "ready".
            with _browser(config, user, 'login-check', expected=identity['identity'], progress=progress, should_stop=should_stop) as browser:
                identity = browser.verify_session()
            ledger.account(user.id, 'ready', identity['identity'], identity.get('name', user.name))
            return identity
        except Exception as exc:
            ledger.account(user.id, 'needs_login', user.identity, user.name, str(exc))
            raise


def check_user(config, user, *, progress=None, should_stop=None) -> dict:
    ensure_runtime_dirs(config)
    ledger = Ledger(config.data_dir)
    with _account_lock(config, user):
        try:
            with _browser(config, user, 'check', progress=progress, should_stop=should_stop) as browser:
                identity = browser.verify_session()
            ledger.account(user.id, 'ready', identity['identity'], identity.get('name', user.name))
            return identity
        except Exception as exc:
            ledger.account(user.id, 'needs_login', user.identity, user.name, str(exc))
            raise


def _validate_conversation_result(item, kind, account_identity):
    if not isinstance(item, dict) or item.get('kind') != kind:
        raise BrowserError('会话类型或身份无效，请重新读取。')
    identity = item.get('identity', '')
    try:
        validate_contact_identity(kind, identity, allow_empty=False)
    except ValueError as exc:
        raise BrowserError('会话身份无效，请重新读取。') from exc
    if kind == 'group':
        if item.get('profile_url'):
            raise BrowserError('群聊不能使用个人主页，请重新读取。')
    else:
        canonical = f'https://www.douyin.com/user/{identity}'
        if validate_contact_url(canonical) != canonical or item.get('profile_url') != canonical:
            raise BrowserError('私聊身份与主页不一致，请重新读取。')
        if identity == account_identity:
            raise BrowserError('不能把当前账号自己设置为续火花联系人。')
    if not isinstance(item.get('name'), str) or not item['name'].strip():
        raise BrowserError('会话名称尚未加载，请重新读取。')


def list_conversations(config, user, kind='group', *, progress=None, should_stop=None) -> dict:
    if not user.identity:
        raise BrowserError('请先登录或检查账号，再读取会话。')
    if kind not in {'direct', 'group'}:
        raise BrowserError('请选择私聊或群聊。')
    ensure_runtime_dirs(config)
    with _account_lock(config, user):
        with _browser(config, user, 'list-conversations', progress=progress, should_stop=should_stop) as browser:
            conversations = browser.list_conversations(kind=kind)
    if not isinstance(conversations, list):
        raise BrowserError('没有取得可核实的会话列表。')
    seen = set()
    for item in conversations:
        _validate_conversation_result(item, kind, user.identity)
        if item['identity'] in seen:
            raise BrowserError('会话列表含重复身份，请重新读取。')
        seen.add(item['identity'])
    return {'kind': kind, 'conversations': conversations}


def bind_contact(config, user, contact: ContactConfig, *, identity='', progress=None, should_stop=None) -> dict:
    if not user.identity:
        raise BrowserError('请先登录或检查账号，再核对联系人。')
    if contact.kind not in {'direct', 'group'}:
        raise BrowserError('请选择私聊或群聊。')
    ensure_runtime_dirs(config)
    with _account_lock(config, user):
        with _browser(config, user, 'bind', progress=progress, should_stop=should_stop) as browser:
            if contact.kind == 'group':
                selected = identity or contact.identity
                try:
                    group_conversation_id(selected)
                except ValueError as exc:
                    raise BrowserError('请先从群聊列表中选择要绑定的群。') from exc
                result = browser.resolve_conversation(selected, kind='group')
                if result.get('identity') != selected or result.get('kind') != 'group' or result.get('profile_url'):
                    raise BrowserError('群聊页面与所选群身份不一致，未保存绑定。')
                _validate_conversation_result(result, 'group', user.identity)
                return result
            if identity:
                if identity == user.identity:
                    raise BrowserError('不能把当前账号自己设置为续火花联系人。')
                result = browser.resolve_conversation(identity, kind='direct')
                if result.get('identity') != identity or result.get('kind') != 'direct':
                    raise BrowserError('私聊页面与所选联系人身份不一致，未保存绑定。')
                _validate_conversation_result(result, 'direct', user.identity)
                return result
            browser.verify_session()
            result = browser.resolve_contact(contact.profile_url)
            if result['identity'] == user.identity:
                raise BrowserError('不能把当前账号自己设置为续火花联系人。')
            return {**result, 'kind': 'direct'}


def run_all(config: AppConfig, progress=None, reset_progress=None, should_stop=None, source='manual') -> RunResult:
    ensure_runtime_dirs(config)
    setup_logging(config)
    run_id = uuid4().hex
    started = _now()
    results = []
    for user in config.enabled_users:
        if should_stop and should_stop():
            break
        results.append(run_user(config, user, run_id, progress, reset_progress, should_stop, source=source))
    result = RunResult(run_id, started, _now(), results)
    _write_run_record(config, result)
    return result


def run_user(config: AppConfig, user: UserConfig, run_id: str | None = None,
             progress=None, reset_progress=None, should_stop=None, source='manual') -> UserRunResult:
    ensure_runtime_dirs(config)
    setup_logging(config)
    run_id = run_id or uuid4().hex
    started = _now()
    results = []
    ledger = Ledger(config.data_dir)
    contacts = [c for c in user.contacts if c.enabled]
    try:
        with _account_lock(config, user):
            ledger.recover(user.id)
            if not user.identity:
                raise LoginRequired('请先在网页中登录或检查该账号。')
            if not contacts:
                raise BrowserError('请先添加并启用联系人。')
            with _browser(config, user, run_id, progress=progress, should_stop=should_stop) as browser:
                identity = browser.verify_session()
                if identity['identity'] != user.identity:
                    raise LoginRequired('浏览器中的账号与已绑定账号不一致，请重新登录。')
                ledger.account(user.id, 'ready', identity['identity'], identity.get('name', user.name))
                for index, contact in enumerate(contacts):
                    if reset_progress:
                        reset_progress()
                    if should_stop and should_stop():
                        results.extend(_result(c, 'cancelled', '已停止，未发送') for c in contacts[index:])
                        break
                    result = _send_contact(browser, config, user, contact, run_id, ledger, source=source)
                    results.append(result)
                    if result.status == 'uncertain':
                        results.extend(_result(c, 'cancelled', '已有发送结果待核实，本次停止后续任务') for c in contacts[index + 1:])
                        break
                    if index < len(contacts) - 1 and not _sleep_interruptible(config.schedule.min_contact_interval_seconds, should_stop):
                        results.extend(_result(c, 'cancelled', '已停止，未发送') for c in contacts[index + 1:])
                        break
    except Exception as exc:
        status = 'cancelled' if isinstance(exc, BrowserInterrupted) else 'failed'
        if isinstance(exc, LoginRequired):
            ledger.account(user.id, 'needs_login', user.identity, user.name, str(exc))
        completed = {r.contact_id for r in results}
        results.extend(_result(c, status, str(exc)) for c in contacts if c.id not in completed)
        if not contacts:
            results.append(ContactResult('', False, str(exc), status=status))
        logging.warning('User task %s stopped: %s', user.id, type(exc).__name__)
    result = UserRunResult(user.name, started, _now(), results, user_id=user.id)
    _write_run_record(config, result, run_id + '-' + user.id)
    return result


def _result(contact, status, reason='', *, skipped=False, attempt_id=''):
    return ContactResult(contact.name, status == 'confirmed', reason, skipped,
                         contact.profile_url, status, contact.id, attempt_id)


def _send_contact(browser, config, user, contact, run_id, ledger, source='manual'):
    if not contact.identity or (contact.kind == 'direct' and not contact.profile_url):
        reason = '请先从群聊列表选择并核实绑定此群。' if contact.kind == 'group' else '请先从私聊列表选择并核实绑定此联系人。'
        return _result(contact, 'failed', reason)
    try:
        validate_contact_identity(contact.kind, contact.identity, allow_empty=False)
        if contact.kind == 'group' and contact.profile_url:
            raise ValueError('群聊不能使用个人主页链接，请重新绑定。')
    except ValueError as exc:
        return _result(contact, 'failed', str(exc))
    if contact.kind == 'direct' and contact.identity == user.identity:
        return _result(contact, 'failed', '联系人不能是当前账号自己。')
    message = contact.message or user.message
    if not message.strip():
        return _result(contact, 'failed', '消息内容不能为空。')
    attempt, owned = ledger.claim(user, contact, _day(config), message, source=source)
    attempt_id = attempt['id']
    if not owned:
        if attempt['status'] == 'confirmed':
            return _result(contact, 'confirmed', '今天已确认发送，跳过重复任务', skipped=True, attempt_id=attempt_id)
        if attempt.get('manual_only'):
            return _result(contact, 'skipped', '人工核实后仅允许手动重试，已跳过定时任务', skipped=True, attempt_id=attempt_id)
        return _result(contact, 'uncertain', '已有发送结果待核实，请先核实历史记录', skipped=True, attempt_id=attempt_id)
    dispatched = False

    def before_send():
        nonlocal dispatched
        if dispatched:
            raise SendUncertain('一次任务不能再次触发发送。')
        ledger.transition(attempt_id, 'dispatching')
        dispatched = True

    try:
        evidence = browser.send_message(contact.name, message, attempt_id,
                                        profile_url=contact.profile_url, identity=contact.identity,
                                        kind=contact.kind,
                                        before_send=before_send)
        if not dispatched or not isinstance(evidence, dict) or evidence.get('status') != 'confirmed':
            raise BrowserError('浏览器没有提供可核实的发送成功证据。')
        ledger.transition(attempt_id, 'confirmed', evidence=evidence)
        return _result(contact, 'confirmed', '平台已确认发送', attempt_id=attempt_id)
    except Exception as exc:
        # A database write can commit and then raise during cleanup. Read back
        # durable truth before choosing a recovery state or reporting failure.
        try:
            stored = ledger.attempt(attempt_id) or {}
        except Exception:
            stored = {}
        if stored.get('status') == 'confirmed':
            return _result(contact, 'confirmed', '平台已确认发送', attempt_id=attempt_id)
        if dispatched or isinstance(exc, SendUncertain) or stored.get('status') in {'dispatching', 'uncertain'}:
            status = 'uncertain'
        else:
            status = 'cancelled' if isinstance(exc, BrowserInterrupted) else 'failed'
        try:
            if stored.get('status') != status:
                ledger.transition(attempt_id, status, str(exc))
        except Exception:
            # Preserve the original failure. An unfinished durable dispatch is
            # recovered as uncertain after the worker exits, never retried.
            logging.exception('Could not persist result for attempt %s', attempt_id)
        if isinstance(exc, LoginRequired) and status != 'uncertain':
            raise
        return _result(contact, status, str(exc), attempt_id=attempt_id)


def _sleep_interruptible(seconds: float, should_stop=None) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if should_stop and should_stop():
            return False
        time.sleep(max(0, min(.1, deadline - time.monotonic())))
    return True


def _write_run_record(config, result, run_id=None):
    directory = config.data_dir / 'runs'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f'{run_id or result.run_id}.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.chmod(0o600)
    temporary.replace(path)
    return path
