from pathlib import Path

from commercial_v1.candidate import CANDIDATE_BUILD
from commercial_v1.diagnostics.service import DiagnosticsService
from commercial_v1.license.runtime_state import LicenseRuntimeStateStore
from commercial_v1.runtime.jobs import PersistentJobStore
from commercial_v1.storage.database import Database, DatabaseConfig
from commercial_v1.storage.health import DatabaseHealthService
from commercial_v1.storage.schema import create_schema_v1
from commercial_v1.storage.writer import StorageWriter


def test_candidate_diagnostics_expose_queue_status_and_recent_rows(tmp_path: Path) -> None:
    db = Database(DatabaseConfig(tmp_path / "runtime.db"))
    with db.connect() as conn:
        create_schema_v1(conn)
    writer = StorageWriter(db)
    writer.start()
    try:
        now = "2026-08-31T12:00:00+00:00"
        jobs = PersistentJobStore(db, writer)
        jobs.enqueue(
            CANDIDATE_BUILD,
            {"target_uid": "target", "source_batch_id": "batch"},
            job_uid="candidate-job",
        )
        writer.execute(
            """INSERT INTO candidate_batch(
               candidate_id,action_type,advertiser_id,ad_id,execution_mode,grouping_mode,
               execution_params_json,group_fingerprint,status,created_at,expires_at
               ) VALUES('candidate-1','CREATE_RETARGET','111111','222222','MANUAL','SEPARATE',
                        '{}','fingerprint','WAITING_CONFIRMATION',?,?)""",
            (now, "2026-08-31T12:30:00+00:00"),
        ).result(timeout=5)

        snapshot = DiagnosticsService(
            db,
            DatabaseHealthService(db),
            writer,
            jobs,
            LicenseRuntimeStateStore(db, writer),
        ).snapshot()
        assert snapshot["candidate"]["queued_or_running"] == 1
        assert snapshot["candidate"]["status_counts"] == {"WAITING_CONFIRMATION": 1}
        assert snapshot["candidate"]["recent_candidates"][0]["candidate_id"] == "candidate-1"
        assert snapshot["candidate"]["recent_candidates"][0]["action_type"] == "CREATE_RETARGET"
    finally:
        writer.close()
