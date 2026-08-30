from pathlib import Path

from commercial_v1.runtime.jobs import PersistentJobStore
from commercial_v1.runtime.workers import JobWorker, LeaseRecoveryWorker
from commercial_v1.storage.database import Database, DatabaseConfig
from commercial_v1.storage.schema import create_schema_v1
from commercial_v1.storage.writer import StorageWriter


def _env(tmp_path: Path):
    db = Database(DatabaseConfig(tmp_path / "runtime.db"))
    with db.connect() as conn:
        create_schema_v1(conn)
    writer = StorageWriter(db)
    writer.start()
    return db, writer, PersistentJobStore(db, writer)


def test_job_worker_only_claims_registered_job_types(tmp_path: Path) -> None:
    _, writer, jobs = _env(tmp_path)
    allowed = jobs.enqueue("ALLOWED", {"value": 1})
    blocked = jobs.enqueue("UNREGISTERED", {"value": 2})
    worker = JobWorker(jobs, {"ALLOWED": lambda job: {"seen": job.payload["value"]}}, instance_id="worker", poll_seconds=0.01)
    assert worker.run_once() is True
    assert jobs.get(allowed)["status"] == "SUCCESS"
    assert jobs.get(blocked)["status"] == "QUEUED"
    assert worker.run_once() is False
    writer.close()


def test_job_worker_marks_handler_failure_without_crashing_queue(tmp_path: Path) -> None:
    _, writer, jobs = _env(tmp_path)
    uid = jobs.enqueue("FAIL", {})
    def fail(_job):
        raise RuntimeError("access_token=super-secret")
    worker = JobWorker(jobs, {"FAIL": fail}, instance_id="worker", poll_seconds=0.01)
    assert worker.run_once() is True
    row = jobs.get(uid)
    assert row["status"] == "FAILED"
    assert "super-secret" not in row["error_message"]
    writer.close()


def test_recovery_worker_uses_explicit_policy(tmp_path: Path) -> None:
    _, writer, jobs = _env(tmp_path)
    worker = LeaseRecoveryWorker(jobs, {"MATERIAL_5M": "abort", "DAILY_SETTLEMENT": "requeue"})
    assert worker.run_once() == {"requeue": 0, "abort": 0, "block": 0}
    writer.close()
