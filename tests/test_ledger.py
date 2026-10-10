from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from douyinfire.config import ContactConfig, UserConfig
from douyinfire.identities import group_identity
from douyinfire.ledger import Ledger
from douyinfire.locking import FileLock, LockBusy


def fixture(tmp_path):
    contact=ContactConfig('friend','https://www.douyin.com/user/target',identity='target')
    user=UserConfig('self',[contact],'hello',identity='self')
    return Ledger(tmp_path), user, contact


def test_concurrent_claim_has_one_owner(tmp_path):
    ledger,user,contact=fixture(tmp_path)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results=list(pool.map(lambda _: Ledger(tmp_path).claim(user,contact,'2026-10-10','hello'),range(12)))
    assert sum(owned for _,owned in results)==1
    assert len(ledger.history())==1


def test_crash_recovery_keeps_dispatch_ambiguous(tmp_path):
    ledger,user,contact=fixture(tmp_path)
    first,_=ledger.claim(user,contact,'2026-10-10','hello')
    ledger.transition(first['id'],'dispatching')
    reopened=Ledger(tmp_path)
    reopened.recover(user.id)
    assert reopened.history()[0]['status']=='uncertain'
    assert not reopened.claim(user,contact,'2026-10-11','new')[1]


def test_crash_before_dispatch_can_retry(tmp_path):
    ledger,user,contact=fixture(tmp_path)
    ledger.claim(user,contact,'2026-10-10','hello')
    ledger.recover(user.id)
    assert ledger.claim(user,contact,'2026-10-10','hello')[1]


def test_confirm_requires_dispatch_and_cannot_be_reverted(tmp_path):
    ledger,user,contact=fixture(tmp_path)
    row,_=ledger.claim(user,contact,'2026-10-10','hello')
    with pytest.raises(ValueError): ledger.transition(row['id'],'confirmed')
    ledger.transition(row['id'],'dispatching')
    ledger.transition(row['id'],'confirmed')
    with pytest.raises(ValueError): ledger.resolve(row['id'],'retry_allowed')
    assert not ledger.claim(user,contact,'2026-10-10','changed')[1]
    assert ledger.claim(user,contact,'2026-10-11','hello')[1]


def test_alias_contact_ids_do_not_bypass_recipient_dedup(tmp_path):
    ledger,user,contact=fixture(tmp_path)
    row,_=ledger.claim(user,contact,'2026-10-10','hello')
    ledger.transition(row['id'],'dispatching');ledger.transition(row['id'],'confirmed')
    contact.id='another-id';contact.name='renamed'
    assert not ledger.claim(user,contact,'2026-10-10','hello')[1]


def test_daily_schedule_only_claimed_once(tmp_path):
    ledger,_,_=fixture(tmp_path)
    assert ledger.reserve_schedule('2026-10-10','2026-10-10T00:05:00+08:00')
    assert not Ledger(tmp_path).reserve_schedule('2026-10-10','2026-10-10T01:00:00+08:00')


def test_account_lock_excludes_other_instances(tmp_path):
    with FileLock(tmp_path/'account.lock'):
        with pytest.raises(LockBusy):
            with FileLock(tmp_path/'account.lock'): pass
    with FileLock(tmp_path/'account.lock'): pass


def test_manual_only_gate_survives_restart_and_later_schedule_dates(tmp_path):
    ledger, user, contact = fixture(tmp_path)
    first, _ = ledger.claim(user, contact, '2026-10-10', 'hello')
    ledger.transition(first['id'], 'dispatching')
    ledger.transition(first['id'], 'uncertain')
    resolved = ledger.resolve(first['id'], 'retry_allowed')
    assert resolved['status'] == 'failed' and resolved['manual_only'] == 1

    reopened = Ledger(tmp_path)
    for day in ['2026-10-11', '2026-10-12']:
        blocked, owned = reopened.claim(user, contact, day, 'hello', source='scheduled')
        assert not owned and blocked['id'] == first['id']
        assert reopened.attempt(first['id'])['manual_only'] == 1
    manual, owned = reopened.claim(user, contact, '2026-10-12', 'hello', source='manual')
    assert owned and manual['date'] == '2026-10-12'
    assert manual['status'] == 'reserved'
    assert all(not row['manual_only'] for row in reopened.history())
    reopened.transition(manual['id'], 'dispatching')
    reopened.transition(manual['id'], 'confirmed')
    assert not reopened.claim(user, contact, '2026-10-12', 'hello', source='scheduled')[1]
    assert reopened.claim(user, contact, '2026-10-13', 'hello', source='scheduled')[1]


def test_group_confirmation_deduplicates_after_rename_and_local_id_change(tmp_path):
    ledger, user, _ = fixture(tmp_path)
    contact = ContactConfig('synthetic group', identity=group_identity('1234567890123456789'), kind='group')
    first, owned = ledger.claim(user, contact, '2026-10-10', 'hello')
    assert owned and first['recipient'] == contact.identity
    ledger.transition(first['id'], 'dispatching')
    ledger.transition(first['id'], 'confirmed')
    contact.name, contact.id = 'renamed group', 'different-local-id'
    reopened = Ledger(tmp_path)
    repeated, owned = reopened.claim(user, contact, '2026-10-10', 'changed')
    assert not owned and repeated['id'] == first['id']
    assert reopened.claim(user, contact, '2026-10-11', 'next day')[1]


def test_group_uncertain_send_blocks_later_days(tmp_path):
    ledger, user, _ = fixture(tmp_path)
    contact = ContactConfig('synthetic group', identity=group_identity('1234567890123456789'), kind='group')
    first, _ = ledger.claim(user, contact, '2026-10-10', 'hello')
    ledger.transition(first['id'], 'dispatching')
    reopened = Ledger(tmp_path)
    reopened.recover(user.id)
    for day in ('2026-10-11', '2026-10-12'):
        repeated, owned = reopened.claim(user, contact, day, 'new', source='scheduled')
        assert not owned and repeated['id'] == first['id'] and repeated['status'] == 'uncertain'


def test_direct_and_group_id_namespaces_do_not_collide_or_rewrite_old_keys(tmp_path):
    ledger, user, contact = fixture(tmp_path)
    first, _ = ledger.claim(user, contact, '2026-10-10', 'direct')
    ledger.transition(first['id'], 'dispatching')
    ledger.transition(first['id'], 'confirmed')
    old_row = ledger.attempt(first['id'])
    group = ContactConfig(contact.name, identity=group_identity(contact.identity), kind='group')
    reopened = Ledger(tmp_path)
    group_row, owned = reopened.claim(user, group, '2026-10-10', 'group')
    assert owned and group_row['recipient'] != first['recipient']
    assert reopened.attempt(first['id']) == old_row
    repeated, owned = reopened.claim(user, contact, '2026-10-10', 'new direct')
    assert not owned and repeated['recipient'] == 'target'


@pytest.mark.parametrize('kind,identity,url', [
    ('direct', group_identity('1234567890123456789'), ''),
    ('group', 'plain-direct-identity', ''), ('group', 'group:YR', ''),
    ('group', '', ''), ('unknown', 'plain-direct-identity', ''),
    ('group', group_identity('1234567890123456789'), 'https://www.douyin.com/user/synthetic'),
])
def test_claim_rejects_mismatched_or_unbound_group_before_writing(tmp_path, kind, identity, url):
    ledger, user, _ = fixture(tmp_path)
    contact = ContactConfig('synthetic', url, identity=identity, kind=kind)
    with pytest.raises(ValueError):
        ledger.claim(user, contact, '2026-10-10', 'hello')
    assert ledger.history() == []
