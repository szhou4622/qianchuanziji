from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from commercial_v1.runtime.leases import LeaseConflict, LeaseManager, StaleFencingToken
from commercial_v1.storage.database import Database, DatabaseConfig
from commercial_v1.storage.schema import create_schema_v1
from commercial_v1.storage.writer import StorageWriter


class FakeClock:
    def __init__(self):
        self.value = datetime(2026, 8, 30, tzinfo=timezone.utc)
    def __call__(self):
        return self.value
    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def _env(tmp_path: Path):
    db = Database(DatabaseConfig(tmp_path / "runtime.db"))
    with db.connect() as conn:
        create_schema_v1(conn)
    writer = StorageWriter(db)
    writer.start()
    clock = FakeClock()
    return db, writer, clock, LeaseManager(writer, clock=clock)


def test_expired_lease_takeover_increments_fencing_token(tmp_path: Path) -> None:
    _, writer, clock, leases = _env(tmp_path)
    a = leases.acquire("plan:1", owner_instance_id="A", task_uid="task-a", ttl_seconds=45)
    assert a.fencing_token == 1
    with pytest.raises(LeaseConflict):
        leases.acquire("plan:1", owner_instance_id="B", task_uid="task-b", ttl_seconds=45)
    clock.advance(46)
    b = leases.acquire("plan:1", owner_instance_id="B", task_uid="task-b", ttl_seconds=45)
    assert b.fencing_token == 2
    with pytest.raises(StaleFencingToken):
        leases.assert_current(a)
    leases.assert_current(b)
    writer.close()


def test_heartbeat_keeps_same_token(tmp_path: Path) -> None:
    _, writer, clock, leases = _env(tmp_path)
    lease = leases.acquire("x", owner_instance_id="A", task_uid="t", ttl_seconds=45)
    clock.advance(10)
    renewed = leases.heartbeat(lease, ttl_seconds=45)
    assert renewed.fencing_token == lease.fencing_token
    writer.close()
