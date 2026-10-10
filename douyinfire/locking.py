"""OS locks release automatically when a worker dies; no stale PID files."""
from __future__ import annotations

import fcntl
from pathlib import Path


class LockBusy(RuntimeError):
    pass


class FileLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.file = self.path.open('a+', encoding='utf-8')
        self.path.chmod(0o600)
        try:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.file.close()
            self.file = None
            raise LockBusy('该账号或应用正在运行，请等待当前任务结束。') from exc
        return self

    def __exit__(self, *args):
        if self.file:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()
            self.file = None
