from dataclasses import replace
from unittest.mock import patch

import pytest

from douyinfire.browser import BrowserError, BrowserInterrupted, LoginRequired, SendUncertain
from douyinfire.config import AppConfig, ContactConfig, UserConfig, ScheduleConfig
from douyinfire.ledger import Ledger
from douyinfire.identities import group_identity
from douyinfire.tasks import run_user, _day, bind_contact, list_conversations


@pytest.fixture
def config(tmp_path):
    friend = ContactConfig('friend', 'https://www.douyin.com/user/friend123', 'custom', identity='friend123')
    user = UserConfig('main', [friend], 'global', identity='self123')
    return AppConfig([user], data_dir=tmp_path/'data', log_dir=tmp_path/'logs', screenshot_dir=tmp_path/'screenshots',
                     schedule=ScheduleConfig(min_contact_interval_seconds=1))


class FakeBrowser:
    mode = 'success'
    sends = []
    attempts = []
    targets = []
    def __init__(self, **kwargs):
        self.expected = kwargs['expected_account_id']
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def verify_session(self): return {'identity': self.expected, 'name': 'main'}
    def send_message(self, contact, message, prefix, *, before_send, **kwargs):
        self.attempts.append(contact)
        self.targets.append(kwargs)
        if self.mode == 'uncertain_before': raise SendUncertain('submission may have occurred before callback')
        if self.mode == 'login_second_before' and len(self.attempts) == 2:
            raise LoginRequired('session expired before submitting the second contact')
        if self.mode == 'before': raise BrowserError('not found')
        if self.mode == 'cancel_before': raise BrowserInterrupted('stopped')
        before_send()
        self.sends.append(message)
        if self.mode == 'after': raise SendUncertain('no confirmation')
        if self.mode == 'cancel_after': raise BrowserInterrupted('stopped after enter')
        if self.mode == 'missing_evidence': return None
        return {'status': 'confirmed', 'identity': kwargs['identity'], 'message_id': 'new-message'}


@pytest.fixture(autouse=True)
def fake_browser(monkeypatch):
    FakeBrowser.sends = []
    FakeBrowser.attempts = []
    FakeBrowser.targets = []
    FakeBrowser.mode = 'success'
    monkeypatch.setattr('douyinfire.tasks.DouyinBrowser', FakeBrowser)


def test_only_confirmed_records_success_and_uses_custom_message(config):
    result = run_user(config, config.users[0])
    assert result.success_count == 1
    assert FakeBrowser.sends == ['custom']
    assert Ledger(config.data_dir).history()[0]['status'] == 'confirmed'
    assert list((config.data_dir/'runs').glob('*.json'))


@pytest.mark.parametrize('mode,status,sends', [
    ('before','failed',0), ('after','uncertain',1), ('missing_evidence','uncertain',1),
    ('cancel_before','cancelled',0), ('cancel_after','uncertain',1),
])
def test_failures_respect_dispatch_boundary(config, mode, status, sends):
    FakeBrowser.mode = mode
    result = run_user(config, config.users[0])
    assert result.results[0].status == status
    assert result.success_count == 0
    assert Ledger(config.data_dir).history()[0]['status'] == status
    assert len(FakeBrowser.sends) == sends


def test_confirmed_today_is_not_resent_after_edit_or_restart(config):
    run_user(config, config.users[0])
    config.users[0].contacts[0].message = 'changed'
    result = run_user(config, config.users[0])
    assert result.results[0].skipped
    assert result.success_count == 0
    assert FakeBrowser.sends == ['custom']


def test_uncertain_blocks_retry_even_on_a_later_day(config, monkeypatch):
    FakeBrowser.mode = 'after'
    run_user(config, config.users[0])
    monkeypatch.setattr('douyinfire.tasks._day', lambda c: '2099-01-01')
    FakeBrowser.mode = 'success'
    result = run_user(config, config.users[0])
    assert result.results[0].status == 'uncertain'
    assert len(FakeBrowser.sends) == 1


def test_user_must_explicitly_resolve_uncertain_before_retry(config):
    FakeBrowser.mode = 'after'
    run_user(config, config.users[0])
    ledger = Ledger(config.data_dir)
    attempt = ledger.history()[0]
    ledger.resolve(attempt['id'], 'retry_allowed')
    FakeBrowser.mode = 'success'
    result = run_user(config, config.users[0])
    assert result.success_count == 1
    assert FakeBrowser.sends == ['custom', 'custom']


def test_uncertain_stops_remaining_contacts(config):
    config.users[0].contacts.append(ContactConfig('second', 'https://www.douyin.com/user/second123', identity='second123'))
    FakeBrowser.mode = 'after'
    result = run_user(config, config.users[0])
    assert [r.status for r in result.results] == ['uncertain','cancelled']
    assert len(FakeBrowser.sends) == 1


def test_unbound_contact_is_never_sent(config):
    config.users[0].contacts[0].identity = ''
    result = run_user(config, config.users[0])
    assert result.failure_count == 1
    assert not FakeBrowser.sends


def test_unverified_account_never_sends(config):
    config.users[0].identity = ''
    result = run_user(config, config.users[0])
    assert result.failure_count == 1
    assert not FakeBrowser.sends


def test_manual_only_resolution_blocks_scheduled_send_across_days(config, monkeypatch):
    FakeBrowser.mode = 'after'
    first = run_user(config, config.users[0])
    assert first.results[0].status == 'uncertain'
    ledger = Ledger(config.data_dir)
    ledger.resolve(first.results[0].attempt_id, 'retry_allowed')
    monkeypatch.setattr('douyinfire.tasks._day', lambda _: '2099-01-01')
    FakeBrowser.mode = 'success'

    scheduled = run_user(config, config.users[0], source='scheduled')
    assert scheduled.results[0].status == 'skipped'
    assert scheduled.results[0].skipped
    assert scheduled.success_count == 0
    assert FakeBrowser.sends == ['custom']
    assert ledger.history()[0]['manual_only'] == 1

    manual = run_user(config, config.users[0], source='manual')
    assert manual.success_count == 1
    assert FakeBrowser.sends == ['custom', 'custom']
    assert ledger.attempt(manual.results[0].attempt_id)['status'] == 'confirmed'
    assert all(not row['manual_only'] for row in ledger.history())

    duplicate = run_user(config, config.users[0], source='scheduled')
    assert duplicate.results[0].skipped
    assert FakeBrowser.sends == ['custom', 'custom']


def test_dispatch_commit_then_exception_stays_uncertain_without_sending(config, monkeypatch):
    original_transition = Ledger.transition

    def commit_then_raise(self, attempt_id, status, *args, **kwargs):
        result = original_transition(self, attempt_id, status, *args, **kwargs)
        if status == 'dispatching':
            raise OSError('dispatch committed, then cleanup failed')
        return result

    monkeypatch.setattr(Ledger, 'transition', commit_then_raise)
    result = run_user(config, config.users[0])
    assert result.results[0].status == 'uncertain'
    assert result.success_count == 0
    assert FakeBrowser.sends == []
    ledger = Ledger(config.data_dir)
    assert ledger.attempt(result.results[0].attempt_id)['status'] == 'uncertain'

    retry = run_user(config, config.users[0])
    assert retry.results[0].status == 'uncertain' and retry.results[0].skipped
    assert FakeBrowser.sends == []


def test_confirmation_commit_then_exception_preserves_confirmed_result(config, monkeypatch):
    original_transition = Ledger.transition

    def commit_then_raise(self, attempt_id, status, *args, **kwargs):
        result = original_transition(self, attempt_id, status, *args, **kwargs)
        if status == 'confirmed':
            raise OSError('confirmation committed, then cleanup failed')
        return result

    monkeypatch.setattr(Ledger, 'transition', commit_then_raise)
    result = run_user(config, config.users[0])
    assert result.results[0].status == 'confirmed'
    assert result.success_count == 1 and result.failure_count == 0
    assert FakeBrowser.sends == ['custom']
    stored = Ledger(config.data_dir).attempt(result.results[0].attempt_id)
    assert stored['status'] == 'confirmed'

    duplicate = run_user(config, config.users[0])
    assert duplicate.results[0].status == 'confirmed' and duplicate.results[0].skipped
    assert FakeBrowser.sends == ['custom']


def test_mid_run_login_expiry_before_send_marks_auth_and_stops_remaining(config, monkeypatch):
    config.users[0].contacts.extend([
        ContactConfig('second', 'https://www.douyin.com/user/second123', identity='second123'),
        ContactConfig('third', 'https://www.douyin.com/user/third123', identity='third123'),
    ])
    monkeypatch.setattr('douyinfire.tasks._sleep_interruptible', lambda *args: True)
    FakeBrowser.mode = 'login_second_before'
    result = run_user(config, config.users[0])
    assert [item.status for item in result.results] == ['confirmed', 'failed', 'failed']
    assert FakeBrowser.attempts == ['friend', 'second']
    assert FakeBrowser.sends == ['custom']
    assert result.success_count == 1 and result.failure_count == 2
    ledger = Ledger(config.data_dir)
    assert ledger.accounts()[config.users[0].id]['status'] == 'needs_login'
    assert 'session expired' in ledger.accounts()[config.users[0].id]['reason']
    assert {row['contact']: row['status'] for row in ledger.history()} == {'friend': 'confirmed', 'second': 'failed'}


def test_uncertain_before_dispatch_callback_remains_uncertain(config):
    FakeBrowser.mode = 'uncertain_before'
    result = run_user(config, config.users[0])
    assert result.results[0].status == 'uncertain'
    assert result.success_count == 0
    assert FakeBrowser.sends == []
    assert Ledger(config.data_dir).attempt(result.results[0].attempt_id)['status'] == 'uncertain'


def group_contact():
    return ContactConfig('合成群聊', message='群测试', identity=group_identity('9000000000000000001'), kind='group')


def test_bound_group_uses_group_route_without_profile_and_preserves_dedup(config):
    contact = group_contact()
    config.users[0].contacts = [contact]
    first = run_user(config, config.users[0])
    assert first.results[0].status == 'confirmed'
    assert FakeBrowser.targets == [{'profile_url': '', 'identity': contact.identity, 'kind': 'group'}]
    contact.name = '群改名'
    contact.id = 'renamed-local-id'
    contact.message = '改后的消息'
    second = run_user(config, config.users[0])
    assert second.results[0].skipped
    assert FakeBrowser.sends == ['群测试']


def test_unbound_group_returns_group_instructions_without_sending(config):
    config.users[0].contacts = [ContactConfig('合成群聊', kind='group')]
    result = run_user(config, config.users[0])
    assert result.results[0].status == 'failed'
    assert '群聊列表' in result.results[0].reason
    assert not FakeBrowser.sends and not Ledger(config.data_dir).history()


def test_group_uncertain_prevents_remaining_contacts_and_later_retry(config, monkeypatch):
    config.users[0].contacts.insert(0, group_contact())
    FakeBrowser.mode = 'after'
    result = run_user(config, config.users[0])
    assert [r.status for r in result.results] == ['uncertain', 'cancelled']
    monkeypatch.setattr('douyinfire.tasks._day', lambda c: '2099-02-01')
    FakeBrowser.mode = 'success'
    again = run_user(config, config.users[0])
    assert [r.status for r in again.results] == ['uncertain', 'cancelled']
    assert FakeBrowser.sends == ['群测试']


def test_group_discovery_is_read_only_and_returns_stable_candidates(config, monkeypatch):
    contact = group_contact()
    candidate = {'kind': 'group', 'identity': contact.identity, 'name': contact.name, 'profile_url': ''}
    monkeypatch.setattr(FakeBrowser, 'list_conversations', lambda self, *, kind: [candidate], raising=False)
    result = list_conversations(config, config.users[0])
    assert result == {'kind': 'group', 'conversations': [candidate]}
    assert not FakeBrowser.sends and not Ledger(config.data_dir).history()


def test_group_bind_reopens_selected_identity_and_never_sends(config, monkeypatch):
    contact = group_contact()
    selected = contact.identity
    contact.identity = ''
    opened = []
    def resolve(self, identity, *, kind):
        opened.append((identity, kind))
        return {'kind': 'group', 'identity': identity, 'name': '已核实群名', 'profile_url': ''}
    monkeypatch.setattr(FakeBrowser, 'resolve_conversation', resolve, raising=False)
    result = bind_contact(config, config.users[0], contact, identity=selected)
    assert opened == [(selected, 'group')]
    assert result['identity'] == selected and result['name'] == '已核实群名'
    assert contact.identity == ''  # Saving a verified binding belongs to manager.
    assert not FakeBrowser.sends


@pytest.mark.parametrize('changed', ['identity', 'kind', 'profile_url'])
def test_group_bind_rejects_mismatched_resolved_target(config, monkeypatch, changed):
    contact = group_contact()
    observed = {'kind': 'group', 'identity': contact.identity, 'name': contact.name, 'profile_url': ''}
    observed[changed] = {'identity': group_identity('another-group'), 'kind': 'direct', 'profile_url': 'https://www.douyin.com/user/friend123'}[changed]
    monkeypatch.setattr(FakeBrowser, 'resolve_conversation', lambda *args, **kwargs: observed, raising=False)
    with pytest.raises(BrowserError, match='群身份不一致'):
        bind_contact(config, config.users[0], contact)
    assert not FakeBrowser.sends


def test_group_bind_requires_a_selection_not_just_a_name(config):
    with pytest.raises(BrowserError, match='选择'):
        bind_contact(config, config.users[0], ContactConfig('合成群聊', kind='group'))
    assert not FakeBrowser.sends


def direct_candidate(identity='friend123', name='合成私聊'):
    return {'kind': 'direct', 'identity': identity, 'name': name,
            'profile_url': f'https://www.douyin.com/user/{identity}'}


def test_direct_discovery_preserves_distinct_same_name_targets_without_sending(config, monkeypatch):
    candidates = [direct_candidate(), direct_candidate('other123')]
    read_kinds = []
    def read(self, *, kind):
        read_kinds.append(kind)
        return candidates
    monkeypatch.setattr(FakeBrowser, 'list_conversations', read, raising=False)
    assert list_conversations(config, config.users[0], kind='direct') == {'kind': 'direct', 'conversations': candidates}
    assert read_kinds == ['direct'] and not FakeBrowser.sends
    assert Ledger(config.data_dir).history() == []


@pytest.mark.parametrize('change', [
    {'identity': 'self123', 'profile_url': 'https://www.douyin.com/user/self123'},
    {'identity': group_identity('9000000000000000001')},
    {'kind': 'group'},
    {'profile_url': 'https://www.douyin.com/user/other123'},
    {'profile_url': ''},
    {'name': ''},
])
def test_direct_discovery_rejects_invalid_identity_or_profile(config, monkeypatch, change):
    candidate = {**direct_candidate(), **change}
    monkeypatch.setattr(FakeBrowser, 'list_conversations', lambda self, *, kind: [candidate], raising=False)
    with pytest.raises(BrowserError):
        list_conversations(config, config.users[0], kind='direct')
    assert not FakeBrowser.sends


def test_direct_discovery_rejects_duplicate_identity(config, monkeypatch):
    monkeypatch.setattr(FakeBrowser, 'list_conversations', lambda self, *, kind: [direct_candidate(), direct_candidate(name='另一个名称')], raising=False)
    with pytest.raises(BrowserError, match='重复身份'):
        list_conversations(config, config.users[0], kind='direct')


def test_direct_list_selection_reopens_selected_identity_without_old_profile(config, monkeypatch):
    user = config.users[0]
    contact = user.contacts[0]
    selected = direct_candidate('newfriend123')
    opened = []
    def resolve(self, identity, *, kind):
        opened.append((identity, kind))
        return selected
    monkeypatch.setattr(FakeBrowser, 'resolve_conversation', resolve, raising=False)
    result = bind_contact(config, user, contact, identity=selected['identity'])
    assert result == selected and opened == [(selected['identity'], 'direct')]
    assert contact.identity == 'friend123'  # Manager saves only a verified result.
    assert not FakeBrowser.sends and Ledger(config.data_dir).history() == []


@pytest.mark.parametrize('change', [
    {'identity': 'other123', 'profile_url': 'https://www.douyin.com/user/other123'},
    {'kind': 'group'},
    {'profile_url': 'https://www.douyin.com/user/other123'},
])
def test_direct_list_binding_rejects_changed_target_or_mismatched_profile(config, monkeypatch, change):
    monkeypatch.setattr(FakeBrowser, 'resolve_conversation', lambda *args, **kwargs: {**direct_candidate(), **change}, raising=False)
    with pytest.raises(BrowserError):
        bind_contact(config, config.users[0], config.users[0].contacts[0], identity='friend123')
    assert not FakeBrowser.sends


def test_direct_list_binding_rejects_self_before_opening(config):
    with pytest.raises(BrowserError, match='自己'):
        bind_contact(config, config.users[0], config.users[0].contacts[0], identity='self123')
    assert not FakeBrowser.sends


def test_manual_profile_binding_remains_supported(config, monkeypatch):
    candidate = direct_candidate()
    requested = []
    def resolve(self, profile_url):
        requested.append(profile_url)
        return {key: value for key, value in candidate.items() if key != 'kind'}
    monkeypatch.setattr(FakeBrowser, 'resolve_contact', resolve, raising=False)
    result = bind_contact(config, config.users[0], config.users[0].contacts[0])
    assert result == candidate and requested == [candidate['profile_url']]
    assert not FakeBrowser.sends
