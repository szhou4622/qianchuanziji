from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from commercial_v1.execution import (
    EXECUTION_PREFLIGHT,
    ExecutionJobEnqueuer,
    ExecutionPreflightHandler,
    ExecutionPreflightService,
    ExecutionScheduler,
    ExecutionService,
)
from commercial_v1.qianchuan.errors import OpenApiNetworkError
from commercial_v1.runtime.jobs import PersistentJobStore
from commercial_v1.runtime.recovery import DEFAULT_RECOVERY_POLICY
from commercial_v1.runtime.workers import JobWorker
from commercial_v1.storage.database import Database, DatabaseConfig
from commercial_v1.storage.schema import create_schema_v1
from commercial_v1.storage.writer import StorageWriter

NOW = "2026-09-01T00:00:00+00:00"


class FakeTokens:
    def get_access_token(self, *_args, **_kwargs):
        return "token"


class OfflinePlans:
    def get_detail(self, *_args, **_kwargs):
        raise OpenApiNetworkError("offline", retryable=True)


class UnusedClient:
    def get(self, *_args, **_kwargs):
        raise AssertionError("material/control GET should not be reached when plan preflight is offline")


def _prepare_pending(tmp_path: Path):
    db = Database(DatabaseConfig(tmp_path / "runtime.db"))
    with db.connect() as conn:
        create_schema_v1(conn)
    writer = StorageWriter(db)
    writer.start()
    writer.execute(
        """INSERT INTO qianchuan_auth_profile(auth_profile_id,app_id,encrypted_app_secret,auth_status,created_at,updated_at)
           VALUES('auth','123456','encrypted','ACTIVE',?,?)""",
        (NOW, NOW),
    ).result(timeout=5)
    writer.execute(
        """INSERT INTO qianchuan_account(account_uid,advertiser_id,enabled,auth_status,created_at,updated_at)
           VALUES('acc','111111',1,'ACTIVE',?,?)""",
        (NOW, NOW),
    ).result(timeout=5)
    writer.execute(
        """INSERT INTO qianchuan_account_auth(account_uid,auth_profile_id,is_primary,bound_at,created_at)
           VALUES('acc','auth',1,?,?)""",
        (NOW, NOW),
    ).result(timeout=5)
    writer.execute(
        """INSERT INTO monitor_plan(
           target_uid,account_uid,advertiser_id,ad_id,plan_name,plan_system,promotion_scene,
           official_status,monitor_enabled,lifecycle_state,collection_active,strategy_eligible,write_eligible,
           sync_state,created_at,updated_at)
           VALUES('target','acc','111111','222222','plan','UNI_PROJECT','VIDEO_PROM_GOODS','DELIVERY_OK',
                  1,'ACTIVE_COLLECTING',1,1,1,'TRUSTED',?,?)""",
        (NOW, NOW),
    ).result(timeout=5)
    writer.execute(
        """INSERT INTO candidate_batch(
           candidate_id,action_type,advertiser_id,ad_id,execution_mode,grouping_mode,execution_params_json,
           group_fingerprint,status,created_at,approved_at)
           VALUES('candidate','CREATE_RETARGET','111111','222222','AUTO','SEPARATE','{}','fp','APPROVED',?,?)""",
        (NOW, NOW),
    ).result(timeout=5)
    writer.execute(
        """INSERT INTO candidate_item(
           candidate_item_id,candidate_id,object_uid,material_id,metric_snapshot_json,before_state_json,created_at)
           VALUES('item','candidate','material:111111:222222:900001','900001','{}','{}',?)""",
        (NOW,),
    ).result(timeout=5)
    execution = ExecutionService(db, writer).prepare_from_candidate("candidate")
    return db, writer, execution.execution_id


def test_offline_preflight_job_fails_but_execution_stays_pending_and_can_be_requeued(tmp_path: Path) -> None:
    db, writer, execution_id = _prepare_pending(tmp_path)
    try:
        jobs = PersistentJobStore(db, writer)
        enqueuer = ExecutionJobEnqueuer(jobs)
        uid = enqueuer.preflight(execution_id)
        preflight = ExecutionPreflightService(
            db,
            writer,
            UnusedClient(),
            FakeTokens(),
            OfflinePlans(),
        )
        worker = JobWorker(
            jobs,
            handlers={EXECUTION_PREFLIGHT: ExecutionPreflightHandler(preflight)},
            instance_id="phase6-test-worker",
        )
        assert worker.run_once() is True
        assert jobs.get(uid)["status"] == "FAILED"  # type: ignore[index]
        with db.connect(readonly=True) as conn:
            assert conn.execute("SELECT status FROM execution_task WHERE execution_id=?", (execution_id,)).fetchone()[0] == "PENDING"
            assert conn.execute("SELECT COUNT(*) FROM execution_attempt").fetchone()[0] == 0

        # 把失败时间设置到退避窗口之前；Scheduler 应重排同一个 Job UID，而不是制造新记录。
        writer.execute(
            "UPDATE background_job SET updated_at='2000-01-01T00:00:00+00:00' WHERE job_uid=?",
            (uid,),
        ).result(timeout=5)
        scheduler = ExecutionScheduler(
            db,
            jobs,
            business_allowed=lambda: True,
            retry_delay_seconds=1,
            interval_seconds=10,
        )
        summary = scheduler.run_once()
        assert summary["preflight_requeued"] == 1
        assert jobs.get(uid)["status"] == "QUEUED"  # type: ignore[index]
        with db.connect(readonly=True) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM background_job WHERE job_type='EXECUTION_PREFLIGHT'"
            ).fetchone()[0] == 1
    finally:
        writer.close()


def test_execution_durable_jobs_are_explicitly_recoverable() -> None:
    assert DEFAULT_RECOVERY_POLICY["EXECUTION_PREPARE"] == "requeue"
    assert DEFAULT_RECOVERY_POLICY["EXECUTION_PREFLIGHT"] == "requeue"
