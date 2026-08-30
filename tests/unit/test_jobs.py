from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from commercial_v1.runtime.jobs import PersistentJobStore, StaleJobFencingToken
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
    return db, writer, clock, PersistentJobStore(db, writer, clock=clock)


def test_claim_complete_persistent_job(tmp_path: Path) -> None:
    _, writer, _, jobs = _env(tmp_path)
    uid = jobs.enqueue("SETTLEMENT", {"date":"2026-08-29"})
    job = jobs.claim_next("instance-a")
    assert job and job.job_uid == uid and job.fencing_token == 1
    jobs.complete(job, {"ok": True})
    assert jobs.get(uid)["status"] == "SUCCESS"
    writer.close()


def test_expired_jobs_use_explicit_recovery_policy(tmp_path: Path) -> None:
    _, writer, clock, jobs = _env(tmp_path)
    realtime = jobs.enqueue("MATERIAL_5M", {})
    settlement = jobs.enqueue("DAILY_SETTLEMENT", {})
    j1 = jobs.claim_next("instance-a", lease_seconds=10)
    j2 = jobs.claim_next("instance-a", lease_seconds=10)
    assert {j1.job_uid, j2.job_uid} == {realtime, settlement}
    clock.advance(11)
    counts = jobs.recover_expired({"MATERIAL_5M":"abort", "DAILY_SETTLEMENT":"requeue"})
    assert counts == {"requeue":1, "abort":1, "block":0}
    assert jobs.get(realtime)["status"] == "ABORTED_BY_RESTART"
    assert jobs.get(settlement)["status"] == "QUEUED"
    writer.close()


def test_unconfigured_expired_job_blocks_instead_of_requeue(tmp_path: Path) -> None:
    _, writer, clock, jobs = _env(tmp_path)
    uid = jobs.enqueue("UNKNOWN_JOB", {})
    old = jobs.claim_next("instance-a", lease_seconds=5)
    clock.advance(6)
    assert jobs.recover_expired({})["block"] == 1
    assert jobs.get(uid)["status"] == "BLOCKED"
    with pytest.raises(StaleJobFencingToken):
        jobs.complete(old)
    writer.close()
