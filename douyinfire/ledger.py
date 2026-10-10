"""Durable outbox. Once dispatch may have occurred, never retry implicitly."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .identities import validate_contact_identity

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class Ledger:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / 'tasks.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, user_name TEXT NOT NULL,
                    contact_id TEXT NOT NULL, recipient TEXT NOT NULL, contact TEXT NOT NULL,
                    message TEXT NOT NULL, date TEXT NOT NULL, status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '{}',
                    manual_only INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(user_id, recipient, date)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, attempt_id TEXT, status TEXT, detail TEXT, at TEXT
                );
                CREATE TABLE IF NOT EXISTS schedules (
                    date TEXT PRIMARY KEY, target TEXT NOT NULL, started_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS accounts (
                    user_id TEXT PRIMARY KEY, status TEXT NOT NULL, identity TEXT NOT NULL,
                    name TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', checked_at TEXT NOT NULL
                );
            ''')
            columns = {r['name'] for r in db.execute('PRAGMA table_info(attempts)')}
            if 'manual_only' not in columns:
                db.execute('ALTER TABLE attempts ADD COLUMN manual_only INTEGER NOT NULL DEFAULT 0')
        self.path.chmod(0o600)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=10000')
        db.execute('PRAGMA synchronous=FULL')
        try:
            yield db
        finally:
            db.close()

    def claim(self, user, contact, day: str, message: str, source: str = 'manual') -> tuple[dict, bool]:
        # Direct identities remain byte-for-byte identical to existing outbox
        # keys. Canonical group identities have a separate namespace already.
        validate_contact_identity(contact.kind, contact.identity, allow_empty=False)
        if contact.kind == 'group' and contact.profile_url:
            raise ValueError('群聊不能使用个人主页链接。')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            pending = db.execute(
                "SELECT * FROM attempts WHERE user_id=? AND recipient=? AND status IN ('dispatching','uncertain','reserved') ORDER BY created_at LIMIT 1",
                (user.id, contact.identity),
            ).fetchone()
            if pending:
                db.commit()
                return dict(pending), False
            manual = db.execute("SELECT * FROM attempts WHERE user_id=? AND recipient=? AND manual_only=1 AND status='failed' ORDER BY created_at LIMIT 1", (user.id, contact.identity)).fetchone()
            if manual and source == 'scheduled':
                db.commit()
                return dict(manual), False
            if source != 'scheduled':
                db.execute('UPDATE attempts SET manual_only=0 WHERE user_id=? AND recipient=?', (user.id, contact.identity))
            row = db.execute('SELECT * FROM attempts WHERE user_id=? AND recipient=? AND date=?', (user.id, contact.identity, day)).fetchone()
            if row and row['status'] == 'confirmed':
                db.commit()
                return dict(row), False
            now = _now()
            if row:
                attempt_id = row['id']
                db.execute("UPDATE attempts SET status='reserved', reason='', evidence='{}', contact_id=?, contact=?, message=?, updated_at=? WHERE id=?",
                           (contact.id, contact.name, message, now, attempt_id))
            else:
                attempt_id = uuid4().hex
                db.execute('INSERT INTO attempts(id,user_id,user_name,contact_id,recipient,contact,message,date,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                           (attempt_id, user.id, user.name, contact.id, contact.identity, contact.name, message, day, 'reserved', now, now))
            db.execute('INSERT INTO events(attempt_id,status,detail,at) VALUES(?,?,?,?)', (attempt_id, 'reserved', '', now))
            result = dict(db.execute('SELECT * FROM attempts WHERE id=?', (attempt_id,)).fetchone())
            db.commit()
            return result, True

    def transition(self, attempt_id: str, status: str, reason: str = '', evidence: dict | None = None) -> dict:
        allowed = {
            'reserved': {'dispatching', 'failed', 'cancelled', 'uncertain'},
            'dispatching': {'confirmed', 'uncertain'},
            'uncertain': set(), 'confirmed': set(), 'failed': set(), 'cancelled': set(),
        }
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM attempts WHERE id=?', (attempt_id,)).fetchone()
            if row is None or status not in allowed.get(row['status'], set()):
                db.rollback()
                raise ValueError('任务状态已变化，请刷新结果。')
            now = _now()
            db.execute('UPDATE attempts SET status=?,reason=?,evidence=?,updated_at=? WHERE id=?',
                       (status, reason, json.dumps(evidence or {}, ensure_ascii=False), now, attempt_id))
            db.execute('INSERT INTO events(attempt_id,status,detail,at) VALUES(?,?,?,?)', (attempt_id, status, reason, now))
            result = dict(db.execute('SELECT * FROM attempts WHERE id=?', (attempt_id,)).fetchone())
            db.commit()
            return result

    def resolve(self, attempt_id: str, resolution: str) -> dict:
        if resolution not in {'confirmed_sent', 'retry_allowed'}:
            raise ValueError('无效的核实结果。')
        status = 'confirmed' if resolution == 'confirmed_sent' else 'failed'
        reason = '用户已核实：已经发送' if status == 'confirmed' else '用户已核实：未发送，可手动重试'
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM attempts WHERE id=?', (attempt_id,)).fetchone()
            if row is None or row['status'] != 'uncertain':
                db.rollback()
                raise ValueError('只能核实结果待确认的任务。')
            now = _now()
            db.execute('UPDATE attempts SET status=?,reason=?,manual_only=?,updated_at=? WHERE id=?', (status, reason, int(resolution == 'retry_allowed'), now, attempt_id))
            db.execute('INSERT INTO events(attempt_id,status,detail,at) VALUES(?,?,?,?)', (attempt_id, status, reason, now))
            result = dict(db.execute('SELECT * FROM attempts WHERE id=?', (attempt_id,)).fetchone())
            db.commit()
            return result

    def recover(self, user_id: str | None = None) -> None:
        """Call only while owning the service or account lock, never on live workers."""
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            where = ' AND user_id=?' if user_id else ''
            args = (user_id,) if user_id else ()
            for row in db.execute("SELECT id,status FROM attempts WHERE status IN ('reserved','dispatching')" + where, args).fetchall():
                status = 'uncertain' if row['status'] == 'dispatching' else 'failed'
                reason = '上次任务中断，发送结果待核实' if status == 'uncertain' else '上次任务在发送前中断，可重试'
                now = _now()
                db.execute('UPDATE attempts SET status=?,reason=?,updated_at=? WHERE id=?', (status, reason, now, row['id']))
                db.execute('INSERT INTO events(attempt_id,status,detail,at) VALUES(?,?,?,?)', (row['id'], status, reason, now))
            db.commit()

    def attempt(self, attempt_id: str) -> dict | None:
        with self._db() as db:
            row = db.execute('SELECT * FROM attempts WHERE id=?', (attempt_id,)).fetchone()
            return dict(row) if row else None

    def history(self, user_id: str = '', limit: int = 100) -> list[dict]:
        with self._db() as db:
            sql = 'SELECT * FROM attempts'
            args = []
            if user_id:
                sql += ' WHERE user_id=?'
                args.append(user_id)
            sql += ' ORDER BY updated_at DESC LIMIT ?'
            args.append(min(max(limit, 1), 500))
            return [dict(row) for row in db.execute(sql, args)]

    def reserve_schedule(self, day: str, target: str) -> bool:
        with self._db() as db:
            return db.execute('INSERT OR IGNORE INTO schedules VALUES(?,?,?)', (day, target, _now())).rowcount == 1

    def schedule_claimed(self, day: str) -> bool:
        with self._db() as db:
            return db.execute('SELECT 1 FROM schedules WHERE date=?', (day,)).fetchone() is not None

    def account(self, user_id: str, status: str, identity: str = '', name: str = '', reason: str = '') -> None:
        with self._db() as db:
            db.execute('INSERT INTO accounts VALUES(?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET status=excluded.status,identity=excluded.identity,name=excluded.name,reason=excluded.reason,checked_at=excluded.checked_at',
                       (user_id, status, identity, name, reason, _now()))

    def accounts(self) -> dict:
        with self._db() as db:
            return {r['user_id']: dict(r) for r in db.execute('SELECT * FROM accounts')}
