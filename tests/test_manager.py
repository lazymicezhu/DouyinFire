"""Supervisor tests use real spawn workers and only temporary local data."""
from __future__ import annotations

import multiprocessing as mp
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from douyinfire.config import AppConfig, ContactConfig, UserConfig, load_config, save_config
from douyinfire.ledger import Ledger
from douyinfire.manager import TaskManager
import douyinfire.manager as manager_module


def fake_worker(config, kind, user_id, contact_ids, payload, cancel, output):
    """Runs in a real private group; never imports or launches a browser."""
    os.setsid()
    output.put({'type': 'worker_started', 'pid': os.getpid(), 'pgid': os.getpgrp()})
    user = config.user(user_id)
    scenario = payload.get('scenario', 'finish')
    if kind in {'list_conversations', 'bind'}:
        from douyinfire.identities import group_identity
        contact = next(c for c in user.contacts if c.id == payload['contact_id'])
        if kind == 'list_conversations':
            identity = group_identity('fake-platform-group') if contact.kind == 'group' else 'fake-person'
            result = {'kind': payload['kind'], 'user_id': 'wrong-account', 'contact_id': 'wrong-contact', 'job_id': 'wrong-job',
                      'conversations': [{'kind': payload['kind'], 'identity': identity,
                                         'name': '合成会话', 'profile_url': '' if contact.kind == 'group' else 'https://www.douyin.com/user/'+identity}]}
        else:
            identity = payload.get('identity', contact.identity)
            if scenario == 'wrong_identity':
                identity = group_identity('different-group') if contact.kind == 'group' else 'different-person'
            result = {'kind': ('direct' if contact.kind == 'group' else 'group') if scenario == 'wrong_kind' else contact.kind,
                      'contact_id': contact.id, 'identity': identity, 'name': '已核实合成群' if contact.kind == 'group' else '已核实联系人',
                      'profile_url': '' if contact.kind == 'group' else 'https://www.douyin.com/user/'+identity}
        output.put({'type': 'finished', 'result': result, 'cancelled': scenario == 'cancelled_bind'})
        output.close()
        output.join_thread()
        return
    if scenario == 'crash_after_dispatch':
        ledger = Ledger(config.data_dir)
        attempt, _ = ledger.claim(user, user.contacts[0], '2026-10-10', 'test only')
        ledger.transition(attempt['id'], 'dispatching')
        output.put({'type': 'progress', 'progress': {'step': 'dispatching', 'detail': 'local ledger only'}})
        output.close()
        output.join_thread()
        os._exit(23)
    if scenario == 'orphan_on_cancel':
        marker = config.data_dir / 'test-child.pid'
        # This descendant deliberately ignores TERM, reproducing a browser/driver
        # surviving its worker. It inherits the worker's already-private group.
        script = ('import os,signal,time; from pathlib import Path; '
                  'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                  f'Path({str(marker)!r}).write_text(str(os.getpid())); '
                  '\nwhile True: time.sleep(.1)\n')
        child = subprocess.Popen([sys.executable, '-c', script])
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        output.put({'type': 'progress', 'progress': {'step': 'child_ready', 'detail': str(child.pid)}})
        output.close()
        output.join_thread()
        # The worker retains its default TERM handler and intentionally ignores
        # the cooperative event so the supervisor has to clean up the whole group.
        while True:
            time.sleep(.1)
    if scenario == 'cooperative_cancel':
        output.put({'type': 'progress', 'progress': {'step': 'waiting', 'detail': 'no external work'}})
        cancel.wait(10)
        output.put({'type': 'finished', 'result': {'user_id': user.id, 'results': []}, 'cancelled': True})
    elif scenario == 'login_save_failure':
        Ledger(config.data_dir).account(user.id, 'ready', 'new-test-identity', 'test identity')
        output.put({'type': 'finished', 'result': {'identity': 'new-test-identity', 'name': 'test identity'}})
    else:
        output.put({'type': 'progress', 'progress': {'step': 'first', 'detail': 'one'}})
        output.put({'type': 'progress', 'progress': {'step': 'last', 'detail': 'two'}})
        results = ([{'contact_id': user.contacts[0].id, 'status': 'failed', 'recipient': 'untrusted', 'kind': 'wrong'}]
                   if scenario == 'failure_before_ledger' else [])
        output.put({'type': 'finished', 'result': {'user_id': user.id, 'results': results, 'observed_message': user.message}})
    output.close()
    output.join_thread()


class LateReadQueue:
    """Hold all events for the post-exit read, including progress before finished."""
    def __init__(self, inner):
        self.inner = inner

    def put(self, value):
        return self.inner.put(value)

    def get_nowait(self):
        raise queue.Empty

    def get(self, timeout=None):
        return self.inner.get(timeout=timeout)

    def close(self):
        self.inner.close()

    def join_thread(self):
        self.inner.join_thread()


class LocalSpawnContext:
    def __init__(self, late_reads=False):
        self.inner = mp.get_context('spawn')
        self.late_reads = late_reads
        self.processes = []

    def Event(self):
        return self.inner.Event()

    def Queue(self):
        raw = self.inner.Queue()
        return LateReadQueue(raw) if self.late_reads else raw

    def Process(self, target, args):
        process = self.inner.Process(target=fake_worker, args=args)
        self.processes.append(process)
        return process


@pytest.fixture
def config_path(tmp_path):
    contact = ContactConfig('fake friend', 'https://www.douyin.com/user/fake-target', 'custom test message',
                            id='fake-contact', identity='fake-target')
    user = UserConfig('fake account', [contact], 'original message', id='fake-account', identity='fake-self')
    config = AppConfig([user], data_dir=tmp_path/'data', log_dir=tmp_path/'logs', screenshot_dir=tmp_path/'screenshots')
    path = tmp_path/'config'/'test.yaml'
    save_config(config, path)
    return path


def wait_for(predicate, timeout=10, description='expected supervisor state'):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError(f'Timed out waiting for {description}')


def wait_idle(manager):
    wait_for(lambda: not manager.snapshot()['running'], description='worker completion')
    return manager.snapshot()


def process_running(pid):
    result = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True, check=False)
    return bool(result.stdout.strip()) and not result.stdout.strip().startswith('Z')


@pytest.mark.parametrize('late_reads', [False, True])
def test_spawn_completion_drains_every_late_event(config_path, late_reads):
    manager = TaskManager(config_path, schedule=False, process_context=LocalSpawnContext(late_reads))
    try:
        manager.start('run', user_id='fake-account')
        state = wait_idle(manager)
        assert state['error'] == ''
        assert state['message'] == '已完成'
        assert state['result']['observed_message'] == 'original message'
        assert state['progress']['step'] == 'last'
    finally:
        manager.close()


def add_group(config_path):
    config = load_config(config_path)
    config.users[0].contacts.append(ContactConfig('待绑定群', id='fake-group', kind='group'))
    save_config(config, config_path)


@pytest.mark.parametrize('cancelled', [False, True])
def test_group_scan_results_are_correlated_by_supervisor_without_saving(config_path, cancelled):
    add_group(config_path)
    before = config_path.read_bytes()
    manager = TaskManager(config_path, schedule=False, process_context=LocalSpawnContext())
    try:
        started = manager.start('list_conversations', user_id='fake-account',
                                payload={'kind': 'group', 'contact_id': 'fake-group', 'job_id': 'spoof',
                                         'scenario': 'cancelled_bind' if cancelled else 'finish'})
        state = wait_idle(manager)
        assert state['error'] == ''
        assert started['job_id'] == state['job_id'] != 'spoof'
        if cancelled:
            assert state['result'] is None
        else:
            assert state['result']['job_id'] == started['job_id']
            assert state['result']['user_id'] == 'fake-account'
            assert state['result']['contact_id'] == 'fake-group'
            assert len(state['result']['conversations']) == 1
        assert config_path.read_bytes() == before
    finally:
        manager.close()


@pytest.mark.parametrize('scenario,saved', [('finish', True), ('wrong_kind', False),
                                          ('wrong_identity', False), ('cancelled_bind', False)])
def test_group_bind_only_saves_matching_verified_identity(config_path, scenario, saved):
    from douyinfire.identities import group_identity
    add_group(config_path)
    identity = group_identity('fake-platform-group')
    original = load_config(config_path).users[0].contacts[0]
    manager = TaskManager(config_path, schedule=False, process_context=LocalSpawnContext())
    try:
        manager.start('bind', user_id='fake-account', payload={'contact_id': 'fake-group', 'identity': identity, 'scenario': scenario})
        state = wait_idle(manager)
        config = load_config(config_path)
        group = config.users[0].contacts[1]
        assert group.kind == 'group' and group.profile_url == ''
        assert group.identity == (identity if saved else '')
        assert config.users[0].contacts[0] == original
        if saved:
            assert group.name == '已核实合成群' and state['error'] == ''
        elif scenario != 'cancelled_bind':
            assert state['error']
    finally:
        manager.close()


def test_direct_scan_is_read_only_and_keeps_job_correlation(config_path):
    original = config_path.read_bytes()
    manager = TaskManager(config_path, schedule=False, process_context=LocalSpawnContext())
    try:
        started = manager.start('list_conversations', user_id='fake-account',
                                payload={'kind': 'direct', 'contact_id': 'fake-contact'})
        state = wait_idle(manager)
        assert state['error'] == ''
        result = state['result']
        assert result['kind'] == 'direct' and result['job_id'] == started['job_id']
        assert result['user_id'] == 'fake-account' and result['contact_id'] == 'fake-contact'
        assert result['conversations'][0]['profile_url'] == 'https://www.douyin.com/user/fake-person'
        assert config_path.read_bytes() == original
    finally:
        manager.close()


@pytest.mark.parametrize('scenario,saved', [('finish', True), ('wrong_kind', False),
                                          ('wrong_identity', False), ('cancelled_bind', False)])
def test_direct_list_binding_requires_selected_identity_and_correct_kind(config_path, scenario, saved):
    before = load_config(config_path).users[0].contacts[0]
    manager = TaskManager(config_path, schedule=False, process_context=LocalSpawnContext())
    try:
        manager.start('bind', user_id='fake-account', payload={'contact_id': 'fake-contact',
                      'identity': 'selected-person', 'scenario': scenario})
        state = wait_idle(manager)
        contact = load_config(config_path).users[0].contacts[0]
        if saved:
            assert state['error'] == ''
            assert contact.kind == 'direct' and contact.identity == 'selected-person'
            assert contact.profile_url == 'https://www.douyin.com/user/selected-person'
        else:
            assert contact == before
            if scenario != 'cancelled_bind':
                assert state['error']
    finally:
        manager.close()


@pytest.mark.parametrize('conversation_kind', ['direct', 'group'])
def test_worker_dispatches_requested_conversation_kind_to_read_only_task(config_path, monkeypatch, conversation_kind):
    import douyinfire.tasks as tasks
    calls = []

    def list_stub(config, user, kind, **kwargs):
        calls.append((user.id, kind))
        return {'kind': kind, 'conversations': []}

    monkeypatch.setattr(tasks, 'list_conversations', list_stub)
    monkeypatch.setattr(manager_module.os, 'setsid', lambda: None)
    output = queue.Queue()
    manager_module.worker_main(load_config(config_path), 'list_conversations', 'fake-account', None,
                               {'kind': conversation_kind}, threading.Event(), output)
    events = []
    while not output.empty():
        events.append(output.get_nowait())
    assert calls == [('fake-account', conversation_kind)]
    assert events[-1]['result'] == {'kind': conversation_kind, 'conversations': []}


def test_group_jobs_require_saved_group_and_canonical_identity(config_path):
    add_group(config_path)
    context = LocalSpawnContext()
    manager = TaskManager(config_path, schedule=False, process_context=context)
    try:
        for kind, payload in [
            ('list_conversations', {'kind': 'group', 'contact_id': 'unknown'}),
            ('list_conversations', {'kind': 'group', 'contact_id': 'fake-contact'}),
            ('list_conversations', {'kind': 'direct', 'contact_id': 'fake-group'}),
            ('bind', {'contact_id': 'fake-group', 'identity': 'not-a-group'}),
        ]:
            with pytest.raises(ValueError):
                manager.start(kind, user_id='fake-account', payload=payload)
        assert context.processes == []
    finally:
        manager.close()


@pytest.mark.parametrize('previous_kind', ['direct', 'group'])
@pytest.mark.parametrize('source', ['ledger', 'recent_result'])
def test_retry_does_not_follow_local_contact_id_to_a_different_recipient(config_path, previous_kind, source):
    from douyinfire.identities import group_identity
    config = load_config(config_path)
    contact = config.users[0].contacts[0]
    if previous_kind == 'group':
        contact.kind, contact.profile_url, contact.identity = 'group', '', group_identity('previous-group')
        save_config(config, config_path)
    previous_identity = contact.identity
    context = LocalSpawnContext()
    manager = TaskManager(config_path, schedule=False, process_context=context)
    try:
        if source == 'ledger':
            attempt, owned = manager.ledger.claim(config.users[0], contact, '2000-01-01', 'old target only')
            assert owned
            manager.ledger.transition(attempt['id'], 'dispatching')
            manager.ledger.transition(attempt['id'], 'uncertain')
            manager.ledger.resolve(attempt['id'], 'retry_allowed')
        else:
            manager.start('run', user_id='fake-account', payload={'scenario': 'failure_before_ledger'})
            result = wait_idle(manager)['result']['results'][0]
            assert result['recipient'] == previous_identity and result['kind'] == previous_kind
        process_count = len(context.processes)
        config = load_config(config_path)
        changed = config.users[0].contacts[0]
        changed.kind, changed.profile_url, changed.identity = 'group', '', group_identity('new-group')
        save_config(config, config_path)
        with pytest.raises(ValueError, match='新绑定或更换对象'):
            manager.start('retry', user_id='fake-account')
        assert len(context.processes) == process_count
    finally:
        manager.close()


def test_spawn_crash_recovers_dispatching_as_uncertain(config_path):
    context = LocalSpawnContext()
    manager = TaskManager(config_path, schedule=False, process_context=context)
    try:
        manager.start('run', user_id='fake-account', payload={'scenario': 'crash_after_dispatch'})
        state = wait_idle(manager)
        assert context.processes[0].exitcode == 23
        assert state['error']
        record = manager.history('fake-account')[0]
        assert record['status'] == 'uncertain'
        assert '待核实' in record['reason']
        assert manager._active is None
    finally:
        manager.close()


def test_spawn_cancel_can_finish_cooperatively(config_path):
    context = LocalSpawnContext()
    manager = TaskManager(config_path, schedule=False, process_context=context)
    try:
        manager.start('run', user_id='fake-account', payload={'scenario': 'cooperative_cancel'})
        wait_for(lambda: manager.snapshot()['progress'].get('step') == 'waiting')
        assert manager.cancel()
        state = wait_idle(manager)
        assert state['message'] == '已停止'
        assert state['error'] == ''
        assert context.processes[0].exitcode == 0
    finally:
        manager.close()


def test_spawn_cancel_kills_private_group_after_worker_exits_on_term(config_path, monkeypatch):
    monkeypatch.setattr(manager_module, 'CANCEL_GRACE_SECONDS', .05)
    monkeypatch.setattr(manager_module, 'TERMINATE_GRACE_SECONDS', .15)
    context = LocalSpawnContext()
    manager = TaskManager(config_path, schedule=False, process_context=context)
    marker = load_config(config_path).data_dir / 'test-child.pid'
    parent_group = os.getpgrp()
    signals = []
    actual_killpg = os.killpg

    def record_signal(pgid, signum):
        signals.append((pgid, signum))
        return actual_killpg(pgid, signum)

    monkeypatch.setattr(manager_module.os, 'killpg', record_signal)
    child_pid = None
    try:
        manager.start('run', user_id='fake-account', payload={'scenario': 'orphan_on_cancel'})
        wait_for(lambda: manager.snapshot()['progress'].get('step') == 'child_ready')
        child_pid = int(marker.read_text())
        worker_pid = context.processes[0].pid
        assert os.getpgid(child_pid) == worker_pid != parent_group
        assert manager.cancel()
        state = wait_idle(manager)
        assert state['message'] == '已停止'
        wait_for(lambda: not process_running(child_pid), description='TERM-resistant descendant cleanup')
        assert (worker_pid, signal.SIGTERM) in signals
        assert (worker_pid, signal.SIGKILL) in signals
        assert all(pgid == worker_pid and pgid != parent_group for pgid, _ in signals)
        assert context.processes[0].exitcode == -signal.SIGTERM
    finally:
        manager.close()
        if child_pid and process_running(child_pid):
            # Only the test's own recorded descendant can reach this fallback.
            os.kill(child_pid, signal.SIGKILL)


def test_terminate_never_signals_the_supervisor_process_group(monkeypatch):
    class DeadProcess:
        pid = os.getpgrp()
        def is_alive(self):
            return False
        def join(self, timeout=None):
            pass

    def forbidden(*args):
        raise AssertionError('Supervisor process group must never be signalled')

    monkeypatch.setattr(manager_module.os, 'killpg', forbidden)
    TaskManager._terminate(DeadProcess(), pgid=os.getpgrp())


def test_spawn_login_save_error_downgrades_auth(config_path, monkeypatch):
    manager = TaskManager(config_path, schedule=False, process_context=LocalSpawnContext())

    def fail_save(*args):
        raise OSError('test disk write failed')

    monkeypatch.setattr(manager_module, 'save_config', fail_save)
    try:
        manager.start('login', user_id='fake-account', payload={'scenario': 'login_save_failure'})
        state = wait_idle(manager)
        assert 'disk write failed' in state['error']
        assert state['auth']['fake-account']['status'] == 'needs_login'
        assert load_config(config_path).user('fake-account').identity == 'fake-self'
    finally:
        manager.close()


def test_spawn_start_observes_complete_configuration_after_operation_lock(config_path):
    context = LocalSpawnContext()
    manager = TaskManager(config_path, schedule=False, process_context=context)
    attempted = threading.Event()
    failures = []

    def start():
        attempted.set()
        try:
            manager.start('run', user_id='fake-account')
        except Exception as exc:
            failures.append(exc)

    try:
        with manager.operation_lock:
            starter = threading.Thread(target=start)
            starter.start()
            assert attempted.wait(5)
            config = load_config(config_path)
            config.user('fake-account').message = 'complete new configuration'
            save_config(config, config_path)
            assert context.processes == []
        starter.join(5)
        assert not failures
        state = wait_idle(manager)
        assert state['result']['observed_message'] == 'complete new configuration'
    finally:
        manager.close()
