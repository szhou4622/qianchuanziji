from pathlib import Path

from commercial_v1.runtime.jobs import PersistentJobStore
from commercial_v1.runtime.recovery import StartupRecoveryService
from commercial_v1.storage.database import Database, DatabaseConfig
from commercial_v1.storage.schema import create_schema_v1
from commercial_v1.storage.writer import StorageWriter


def _setup(tmp_path: Path):
    db = Database(DatabaseConfig(tmp_path / "runtime.db"))
    with db.connect() as conn:
        create_schema_v1(conn)
    writer = StorageWriter(db)
    writer.start()
    return db, writer, PersistentJobStore(db, writer)


def test_startup_recovery_aborts_hot_jobs_requeues_durable_and_blocks_unknown(tmp_path: Path) -> None:
    db, writer, jobs = _setup(tmp_path)
    try:
        expired = "2020-01-01T00:00:00+00:00"
        now = "2026-08-30T00:00:00+00:00"

        def seed(conn):
            conn.execute(
                "INSERT INTO collection_batch(batch_id,pipeline_type,started_at,status,created_at) VALUES(?,?,?,?,?)",
                ("batch-running", "MATERIAL", now, "RUNNING", now),
            )
            for uid, job_type in [
                ("hot", "MATERIAL_5M"),
                ("durable", "RECONCILE_EXECUTION"),
                ("unknown", "SOMETHING_NEW"),
            ]:
                conn.execute(
                    """INSERT INTO background_job(
                       job_uid,job_type,priority,payload_json,status,due_at,lease_owner,
                       lease_expires_at,fencing_token,created_at,started_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (uid, job_type, 100, "{}", "RUNNING", expired, "old-worker", expired, 1, expired, expired, expired),
                )
            conn.execute(
                """INSERT INTO execution_task(
                   execution_id,advertiser_id,ad_id,action_type,execution_mode,status,
                   execution_params_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                ("exec-1", "adv", "plan", "CREATE_RETARGET", "AUTO", "SUBMITTED", "{}", now),
            )

        writer.transaction(seed).result(timeout=5)

        report = StartupRecoveryService(db, writer, jobs).run()
        assert report.aborted_collection_batches == 1
        assert report.job_recovery == {"requeue": 1, "abort": 1, "block": 1}
        assert report.unresolved_execution_ids == ("exec-1",)

        with db.connect(readonly=True) as conn:
            assert conn.execute("SELECT status FROM collection_batch WHERE batch_id='batch-running'").fetchone()[0] == "ABORTED_BY_RESTART"
            statuses = dict(conn.execute("SELECT job_uid,status FROM background_job").fetchall())
            assert statuses == {"hot": "ABORTED_BY_RESTART", "durable": "QUEUED", "unknown": "BLOCKED"}
            # Recovery 绝不自行把已提交 Execution 改回待执行，也不触发补发。
            assert conn.execute("SELECT status FROM execution_task WHERE execution_id='exec-1'").fetchone()[0] == "SUBMITTED"
    finally:
        writer.close()
