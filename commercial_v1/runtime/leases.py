"""持久化 Lease / Heartbeat / Fencing。

所有 takeover 都会单调递增 fencing_token；旧 worker 即使恢复也不能继续写。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from commercial_v1.storage.writer import StorageWriter

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


class LeaseConflict(RuntimeError):
    pass


class StaleFencingToken(RuntimeError):
    pass


@dataclass(frozen=True)
class Lease:
    resource_key: str
    owner_instance_id: str
    task_uid: str
    priority: int
    fencing_token: int
    acquired_at: str
    heartbeat_at: str
    expires_at: str


class LeaseManager:
    def __init__(self, writer: StorageWriter, *, clock: Clock = utc_now) -> None:
        self._writer = writer
        self._clock = clock

    def acquire(self, resource_key: str, *, owner_instance_id: str, task_uid: str, priority: int = 100, ttl_seconds: int = 45) -> Lease:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        now = self._clock()
        now_text = _iso(now)
        expires_text = _iso(now + timedelta(seconds=ttl_seconds))

        def work(conn):
            row = conn.execute("SELECT * FROM task_lease WHERE resource_key=?", (resource_key,)).fetchone()
            if row is None:
                token = 1
                conn.execute("INSERT INTO task_lease(resource_key,owner_instance_id,task_uid,priority,acquired_at,heartbeat_at,expires_at,fencing_token) VALUES(?,?,?,?,?,?,?,?)", (resource_key, owner_instance_id, task_uid, priority, now_text, now_text, expires_text, token))
            else:
                same_holder = row["owner_instance_id"] == owner_instance_id and row["task_uid"] == task_uid
                expired = str(row["expires_at"]) <= now_text
                if same_holder and not expired:
                    token = int(row["fencing_token"])
                    conn.execute("UPDATE task_lease SET priority=?,heartbeat_at=?,expires_at=? WHERE resource_key=? AND fencing_token=?", (priority, now_text, expires_text, resource_key, token))
                elif expired:
                    token = int(row["fencing_token"]) + 1
                    conn.execute("UPDATE task_lease SET owner_instance_id=?,task_uid=?,priority=?,acquired_at=?,heartbeat_at=?,expires_at=?,fencing_token=? WHERE resource_key=?", (owner_instance_id, task_uid, priority, now_text, now_text, expires_text, token, resource_key))
                else:
                    raise LeaseConflict(f"resource is leased: {resource_key}")
            return Lease(resource_key, owner_instance_id, task_uid, priority, token, now_text, now_text, expires_text)

        return self._writer.transaction(work).result(timeout=5)

    def heartbeat(self, lease: Lease, *, ttl_seconds: int = 45) -> Lease:
        now = self._clock()
        now_text = _iso(now)
        expires_text = _iso(now + timedelta(seconds=ttl_seconds))

        def work(conn):
            row = conn.execute("SELECT * FROM task_lease WHERE resource_key=?", (lease.resource_key,)).fetchone()
            self._assert_row(row, lease, now_text)
            conn.execute("UPDATE task_lease SET heartbeat_at=?,expires_at=? WHERE resource_key=? AND fencing_token=?", (now_text, expires_text, lease.resource_key, lease.fencing_token))
            return Lease(lease.resource_key, lease.owner_instance_id, lease.task_uid, lease.priority, lease.fencing_token, lease.acquired_at, now_text, expires_text)

        return self._writer.transaction(work).result(timeout=5)

    def assert_current(self, lease: Lease) -> None:
        now_text = _iso(self._clock())
        def work(conn):
            row = conn.execute("SELECT * FROM task_lease WHERE resource_key=?", (lease.resource_key,)).fetchone()
            self._assert_row(row, lease, now_text)
        self._writer.transaction(work).result(timeout=5)

    def release(self, lease: Lease) -> bool:
        def work(conn):
            cursor = conn.execute("DELETE FROM task_lease WHERE resource_key=? AND owner_instance_id=? AND task_uid=? AND fencing_token=?", (lease.resource_key, lease.owner_instance_id, lease.task_uid, lease.fencing_token))
            return cursor.rowcount == 1
        return bool(self._writer.transaction(work).result(timeout=5))

    @staticmethod
    def _assert_row(row, lease: Lease, now_text: str) -> None:
        if row is None:
            raise StaleFencingToken(f"lease no longer exists: {lease.resource_key}")
        if row["owner_instance_id"] != lease.owner_instance_id or row["task_uid"] != lease.task_uid or int(row["fencing_token"]) != lease.fencing_token or str(row["expires_at"]) <= now_text:
            raise StaleFencingToken(f"stale lease: {lease.resource_key}")
