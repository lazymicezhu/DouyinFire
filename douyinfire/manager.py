"""Local job supervisor. Browser workers never run in HTTP request threads."""
from __future__ import annotations

import copy
import multiprocessing as mp
import os
import queue
import signal
import threading
import time
from dataclasses import asdict, is_dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from .config import load_config, save_config, ensure_runtime_dirs
from .ledger import Ledger
from .locking import FileLock, LockBusy
from .scheduler import daily_due, next_run_at


CANCEL_GRACE_SECONDS = 8.0
TERMINATE_GRACE_SECONDS = 2.0
EXIT_QUEUE_GRACE_SECONDS = 0.5


def _now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def worker_main(config, kind, user_id, contact_ids, payload, cancel, output):
    # A private process group allows cancellation to close Chromium and its driver too.
    if hasattr(os, 'setsid'):
        os.setsid()
        output.put({'type': 'worker_started', 'pid': os.getpid(), 'pgid': os.getpgrp()})
    from .tasks import run_all, run_user, login_user, check_user, bind_contact, list_conversations
    from .browser import BrowserInterrupted

    def progress(step, status, detail=''):
        output.put({'type': 'progress', 'progress': {'step': step, 'status': status, 'detail': detail}})

    try:
        user = config.user(user_id) if user_id else None
        if kind in {'run', 'retry'}:
            if user and contact_ids is not None:
                user = replace(user, contacts=[c for c in user.contacts if c.id in contact_ids])
                if not user.contacts:
                    raise ValueError('没有可运行的联系人。')
            source = payload.get('_source', 'manual')
            result = (run_user(config, user, progress=progress, should_stop=cancel.is_set, source=source)
                      if user else run_all(config, progress=progress, should_stop=cancel.is_set, source=source))
        elif kind == 'login':
            result = login_user(config, user, progress=progress, should_stop=cancel.is_set)
        elif kind == 'check':
            result = check_user(config, user, progress=progress, should_stop=cancel.is_set)
        elif kind == 'list_conversations':
            result = list_conversations(config, user, kind=payload['kind'], progress=progress, should_stop=cancel.is_set)
        elif kind == 'bind':
            contact = next((c for c in user.contacts if c.id == payload.get('contact_id')), None)
            if contact is None:
                raise ValueError('请先保存联系人，再核对主页。')
            result = bind_contact(config, user, contact, identity=payload.get('identity', ''),
                                  progress=progress, should_stop=cancel.is_set)
            result['contact_id'] = contact.id
        else:
            raise ValueError('不支持的任务类型。')
        if is_dataclass(result):
            result = asdict(result)
        output.put({'type': 'finished', 'result': result, 'cancelled': cancel.is_set()})
    except Exception as exc:
        output.put({'type': 'finished', 'error': str(exc), 'cancelled': isinstance(exc, BrowserInterrupted) or cancel.is_set()})


class TaskManager:
    def __init__(self, config_path: Path, *, schedule=True, process_context=None):
        self.config_path = Path(config_path)
        self.operation_lock = threading.RLock()
        config = load_config(self.config_path)
        ensure_runtime_dirs(config)
        self.ledger = Ledger(config.data_dir)
        self._ctx = process_context or mp.get_context('spawn')
        self._closed = threading.Event()
        self._schedule_enabled = schedule
        self._active = None
        self._last = dict(running=False, kind='', user_id='', job_id='', message='就绪', error='', result=None,
                          progress={}, started_at='', ended_at='')
        # Recover only profiles whose cross-process locks are currently free.
        self._recover(config)
        self._thread = threading.Thread(target=self._watch, daemon=True, name='douyinfire-supervisor')
        self._thread.start()

    def _recover(self, config):
        for user in config.users:
            try:
                with FileLock(config.data_dir / 'locks' / f'account-{user.id}.lock'):
                    self.ledger.recover(user.id)
            except LockBusy:
                pass

    def snapshot(self):
        with self.operation_lock:
            state = copy.deepcopy(self._last)
            state['auth'] = self.ledger.accounts()
            state['next_run'] = ''
            try:
                config = load_config(self.config_path)
                if config.schedule.enabled:
                    now = datetime.now(ZoneInfo(config.schedule.timezone))
                    day = now.date().isoformat()
                    due = daily_due(now, config.schedule)
                    target = due or next_run_at(now, config.schedule)
                    if self.ledger.schedule_claimed(day) and target.date() == now.date():
                        target = next_run_at(now.replace(hour=23, minute=59, second=59), config.schedule)
                    state['next_run'] = target.isoformat(timespec='seconds')
            except Exception as exc:
                state['schedule_error'] = str(exc)
            return state

    def start(self, kind: str, user_id: str = '', contact_ids=None, payload=None):
        with self.operation_lock:
            if self._closed.is_set():
                raise ValueError('后台正在退出。')
            if self._active:
                raise ValueError('已有任务正在运行，请等待或停止。')
            if kind not in {'login', 'check', 'bind', 'run', 'retry', 'list_conversations'}:
                raise ValueError('无效的任务类型。')
            config = load_config(self.config_path)
            payload = payload or {}
            if not isinstance(payload, dict):
                raise ValueError('任务参数必须是对象。')
            user = config.user(user_id) if user_id else None
            if kind in {'login', 'check', 'bind', 'retry', 'list_conversations'} and user is None:
                raise ValueError('请先选择账号。')
            if contact_ids is not None:
                if not isinstance(contact_ids, list) or user is None:
                    raise ValueError('联系人参数不正确。')
                valid = {c.id for c in user.contacts}
                if not contact_ids or any(c not in valid for c in contact_ids):
                    raise ValueError('联系人已变化，请刷新后重试。')
            if kind == 'retry':
                day = datetime.now(ZoneInfo(config.schedule.timezone)).date().isoformat()
                current = {c.id: c for c in user.contacts}
                retry_ids = {r['contact_id'] for r in self.ledger.history(user.id, 500)
                             if r['status'] == 'failed' and (r['date'] == day or r.get('manual_only'))
                             and r['contact_id'] in current and r['recipient'] == current[r['contact_id']].identity}
                recent = self._last.get('result') or {}
                for previous in recent.get('users', [recent]):
                    if previous.get('user_id') == user.id:
                        retry_ids.update(r['contact_id'] for r in previous.get('results', [])
                                         if r.get('status') == 'failed' and r.get('contact_id') in current
                                         and r.get('recipient') == current[r['contact_id']].identity
                                         and r.get('kind') == current[r['contact_id']].kind)
                contact_ids = [c.id for c in user.contacts if c.enabled and c.id in retry_ids]
                if not contact_ids:
                    raise ValueError('今天没有可重试的失败项；新绑定或更换对象后请使用“立即执行”。待核实任务须先人工核实。')
            if kind in {'bind', 'list_conversations'}:
                contact = next((c for c in user.contacts if c.id == payload.get('contact_id')), None)
                if contact is None:
                    raise ValueError('请先保存联系人，再核对。')
                if kind == 'list_conversations':
                    if payload.get('kind') not in {'direct', 'group'} or payload['kind'] != contact.kind:
                        raise ValueError('请先保存正确的联系人类型，再读取会话。')
                elif contact.kind == 'group' or 'identity' in payload:
                    from .identities import validate_contact_identity
                    validate_contact_identity(contact.kind, payload.get('identity', ''), allow_empty=False)
            if kind == 'run' and not user and not config.enabled_users:
                raise ValueError('请先添加并启用账号。')
            cancel = self._ctx.Event()
            output = self._ctx.Queue()
            process = self._ctx.Process(target=worker_main, args=(config, kind, user_id, contact_ids, payload, cancel, output))
            process.start()
            job_id = uuid4().hex
            self._active = {'process': process, 'cancel': cancel, 'queue': output, 'config': config,
                            'kind': kind, 'user_id': user_id, 'job_id': job_id, 'payload': payload, 'cancel_at': None, 'finished': None, 'pgid': None,
                            'deadline': time.monotonic() + (600 if kind in {'login','check','bind','list_conversations'} else max(600, sum(len(u.contacts) for u in config.users) * 180))}
            self._last = dict(running=True, kind=kind, user_id=user_id, job_id=job_id, message='正在运行', error='', result=None,
                              progress={}, started_at=_now(), ended_at='')
            return self.snapshot()

    def cancel(self):
        with self.operation_lock:
            if not self._active:
                return False
            self._active['cancel'].set()
            self._active['cancel_at'] = self._active['cancel_at'] or time.monotonic()
            self._last['message'] = '正在停止'
            return True

    def history(self, user_id=''):
        return self.ledger.history(user_id)

    def resolve_attempt(self, attempt_id, resolution):
        with self.operation_lock:
            if self._active:
                raise ValueError('请等待当前任务结束后核实结果。')
            return self.ledger.resolve(attempt_id, resolution)

    def _finish(self, active, outcome):
        result = outcome.get('result')
        error = outcome.get('error', '')
        if outcome.get('cancelled') and active['kind'] == 'list_conversations':
            result = None
        if isinstance(result, dict) and active['kind'] in {'bind', 'list_conversations'}:
            result = {**result, 'user_id': active['user_id'], 'contact_id': active['payload'].get('contact_id', ''),
                      'job_id': active.get('job_id', '')}
        if isinstance(result, dict) and active['kind'] in {'run', 'retry'}:
            result = copy.deepcopy(result)
            for previous in result.get('users', [result]):
                started_user = next((u for u in active['config'].users if u.id == previous.get('user_id')), None)
                if started_user is None:
                    continue
                contacts = {c.id: c for c in started_user.contacts}
                for row in previous.get('results', []):
                    contact = contacts.get(row.get('contact_id'))
                    if contact is not None:
                        row.update(recipient=contact.identity, kind=contact.kind)
        if not error and result and not outcome.get('cancelled') and active['kind'] in {'login', 'check', 'bind'}:
            try:
                config = load_config(self.config_path)
                user = config.user(active['user_id'])
                identity = result.get('identity', '')
                if not identity:
                    raise ValueError('页面没有提供可核实的身份，未保存。')
                if active['kind'] == 'bind':
                    contact = next(c for c in user.contacts if c.id == result['contact_id'])
                    started_contact = next(c for c in active['config'].user(user.id).contacts if c.id == contact.id)
                    result_kind = result.get('kind', 'direct')
                    if contact.kind != started_contact.kind or result_kind != contact.kind:
                        raise ValueError('联系人类型已变化，请重新核实绑定。')
                    if contact.kind == 'group':
                        from .identities import group_conversation_id
                        group_conversation_id(identity)
                    if active['payload'].get('identity') and identity != active['payload']['identity']:
                        raise ValueError('核实的会话与所选会话不一致，未保存。')
                    if any(c.id != contact.id and c.identity == identity for c in user.contacts):
                        raise ValueError('该联系人已经存在，请勿重复添加。')
                    contact.identity = identity
                    contact.profile_url = '' if contact.kind == 'group' else result['profile_url']
                    if result.get('name'):
                        contact.name = result['name']
                else:
                    if any(u.id != user.id and u.identity == identity for u in config.users):
                        raise ValueError('此抖音账号已经绑定在另一个本地账号中。')
                    user.identity = identity
                save_config(config, self.config_path)
            except Exception as exc:
                error = str(exc)
                if active['kind'] in {'login', 'check'}:
                    self.ledger.account(active['user_id'], 'needs_login', reason=error)
        self._recover(active['config'])
        cancelled = outcome.get('cancelled', False)
        message = '已停止' if cancelled else ('任务失败' if error else '已完成')
        if result and active['kind'] in {'run', 'retry'}:
            users = result.get('users', [result])
            states = [r.get('status') for u in users for r in u.get('results', [])]
            if 'uncertain' in states:
                message = '有发送结果待核实'
            elif 'failed' in states:
                message = '部分任务未完成，请查看结果'
        self._last.update(running=False, message=message, error=error, result=result, ended_at=_now())
        active['queue'].close()
        self._active = None

    @staticmethod
    def _safe_worker_group(process, pgid):
        """A group is usable only when its leader is this worker, never our parent group."""
        pid = getattr(process, 'pid', None)
        return (isinstance(pid, int) and pid > 1 and pgid == pid
                and hasattr(os, 'getpgrp') and pgid != os.getpgrp())

    @classmethod
    def _current_worker_group(cls, process):
        pid = getattr(process, 'pid', None)
        if not isinstance(pid, int) or pid <= 1 or not hasattr(os, 'getpgid') or not process.is_alive():
            return None
        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            return None
        return pgid if cls._safe_worker_group(process, pgid) else None

    def _remember_worker_group(self, active):
        pgid = self._current_worker_group(active['process'])
        if pgid is not None:
            active['pgid'] = pgid

    @classmethod
    def _terminate(cls, process, pgid=None):
        # pgid is either observed while the worker lives or acknowledged by that
        # worker immediately after setsid(). Keep it after the leader exits: the
        # Chromium descendants can still belong to this private process group.
        current = cls._current_worker_group(process)
        group = current or (pgid if cls._safe_worker_group(process, pgid) else None)
        if group is not None:
            try:
                os.killpg(group, signal.SIGTERM)
            except ProcessLookupError:
                process.join(timeout=.2)
                return
            deadline = time.monotonic() + TERMINATE_GRACE_SECONDS
            while time.monotonic() < deadline:
                process.join(timeout=min(.05, max(0, deadline - time.monotonic())))
                try:
                    os.killpg(group, 0)
                except ProcessLookupError:
                    return
                # join returns immediately once the worker exits; do not spin
                # while its descendants are taking their termination grace.
                time.sleep(min(.02, max(0, deadline - time.monotonic())))
            # A TERM-responsive worker does not prove its descendants exited.
            # Kill the confirmed private group even when process.is_alive=False.
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.join(timeout=2)
            return
        # Before setsid the worker cannot yet have launched a browser. Never
        # signal the inherited parent process group in that short startup window.
        if process.is_alive():
            process.terminate()
            process.join(timeout=TERMINATE_GRACE_SECONDS)
            if process.is_alive():
                process.kill()
                process.join(timeout=2)

    def _consume_event(self, active, event):
        if not isinstance(event, dict):
            return
        kind = event.get('type')
        if kind == 'worker_started':
            if (event.get('pid') == getattr(active['process'], 'pid', None)
                    and self._safe_worker_group(active['process'], event.get('pgid'))):
                active['pgid'] = event['pgid']
        elif kind == 'progress':
            self._last['progress'] = event.get('progress', {})
        elif kind == 'finished':
            active['finished'] = event

    def _drain_events(self, active, deadline=None):
        while not active['finished']:
            try:
                if deadline is None:
                    event = active['queue'].get_nowait()
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    event = active['queue'].get(timeout=min(remaining, .05))
            except queue.Empty:
                if deadline is None:
                    break
                continue
            except (EOFError, OSError):
                break
            self._consume_event(active, event)

    def _poll(self):
        active = self._active
        if not active:
            return
        self._remember_worker_group(active)
        self._drain_events(active)
        now = time.monotonic()
        if now > active['deadline'] and not active['cancel_at']:
            active['cancel'].set()
            active['cancel_at'] = now
            self._last['message'] = '任务超时，正在停止'
        if active['cancel_at'] and now - active['cancel_at'] > CANCEL_GRACE_SECONDS:
            self._terminate(active['process'], active.get('pgid'))
            active['group_cleaned'] = True
        if not active['process'].is_alive():
            active['process'].join(timeout=.2)
            # A queue may deliver several late progress events before finished.
            # Continue until finished or a short absolute deadline, not one read.
            self._drain_events(active, time.monotonic() + EXIT_QUEUE_GRACE_SECONDS)
            if active.get('pgid') is not None and not active.get('group_cleaned'):
                self._terminate(active['process'], active['pgid'])
            outcome = active['finished'] or {'error': '任务已停止；可能发出的消息已保留为待核实。', 'cancelled': bool(active['cancel_at'])}
            self._finish(active, outcome)

    def _schedule_tick(self):
        if self._active or not self._schedule_enabled or self._closed.is_set():
            return
        config = load_config(self.config_path)
        if not config.schedule.enabled or not config.enabled_users:
            return
        now = datetime.now(ZoneInfo(config.schedule.timezone))
        target = daily_due(now, config.schedule)
        if target is None:
            return
        day = now.date().isoformat()
        if self.ledger.schedule_claimed(day):
            return
        # Reserve first: a service crash must not auto-resend an unknown daily run.
        if self.ledger.reserve_schedule(day, target.isoformat()):
            self.start('run', payload={'_source': 'scheduled'})

    def _watch(self):
        next_schedule = 0
        while not self._closed.wait(.2):
            with self.operation_lock:
                try:
                    self._poll()
                    if time.monotonic() >= next_schedule:
                        self._schedule_tick()
                        next_schedule = time.monotonic() + 10
                except Exception as exc:
                    self._last['error'] = str(exc)

    def close(self):
        self._closed.set()
        with self.operation_lock:
            if self._active:
                active = self._active
                active['cancel'].set()
                self._remember_worker_group(active)
                active['process'].join(timeout=2)
                self._drain_events(active)
                self._terminate(active['process'], active.get('pgid'))
                self._drain_events(active, time.monotonic() + EXIT_QUEUE_GRACE_SECONDS)
                outcome = active['finished'] or {'cancelled': True}
                self._finish(active, outcome)
        self._thread.join(timeout=2)
