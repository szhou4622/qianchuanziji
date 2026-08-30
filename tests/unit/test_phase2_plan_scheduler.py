from pathlib import Path

import pytest

from commercial_v1.qianchuan.normalizers import normalize_plan
from commercial_v1.qianchuan.plans import MonitorPlanStore
from commercial_v1.qianchuan.scheduler import (
    PLAN_STATUS_CHECK,
    PlanStateCheckHandler,
    PlanStateScheduler,
)
from commercial_v1.runtime.jobs import PersistentJobStore
from commercial_v1.storage.database import Database, DatabaseConfig
from commercial_v1.storage.schema import create_schema_v1
from commercial_v1.storage.writer import StorageWriter


def _database(tmp_path: Path) -> Database:
    db = Database(DatabaseConfig(tmp_path / "runtime.db"))
    with db.connect() as conn:
        create_schema_v1(conn)
    return db


def _seed_account(db: Database, writer: StorageWriter, advertiser_id: str = "222222") -> None:
    now = "2026-08-30T00:00:00+00:00"
    writer.execute(
        """INSERT INTO qianchuan_auth_profile(
           auth_profile_id,app_id,encrypted_app_secret,auth_status,created_at,updated_at
           ) VALUES('auth-1','123456','cipher','ACTIVE',?,?)""",
        (now, now),
    ).result(timeout=5)
    writer.execute(
        """INSERT INTO qianchuan_account(
           account_uid,advertiser_id,account_name,account_type,enabled,auth_status,created_at,updated_at
           ) VALUES(?,?,?,'QIANCHUAN',1,'ACTIVE',?,?)""",
        (f"qc:{advertiser_id}", advertiser_id, "account", now, now),
    ).result(timeout=5)
    writer.execute(
        """INSERT INTO qianchuan_account_auth(
           account_uid,auth_profile_id,is_primary,bound_at,created_at
           ) VALUES(?,?,1,?,?)""",
        (f"qc:{advertiser_id}", "auth-1", now, now),
    ).result(timeout=5)


def _plan(ad_id: str, status: str, *, goal: str = "VIDEO_PROM_GOODS"):
    return normalize_plan(
        {
            "ad_id": ad_id,
            "name": ad_id,
            "marketing_goal": goal,
            "adlab_scene": "OVERALL_PROJECT",
            "status": status,
        },
        advertiser_id="222222",
        expected_marketing_goal=goal,
        expected_adlab_scene="OVERALL_PROJECT",
    )


def test_scheduler_enqueues_one_due_watching_job_and_deduplicates(tmp_path: Path) -> None:
    db = _database(tmp_path)
    writer = StorageWriter(db)
    writer.start()
    try:
        _seed_account(db, writer)
        store = MonitorPlanStore(db, writer)
        target_uid = store.enroll_verified(_plan("910001", "OFFLINE_BUDGET"))
        writer.execute(
            "UPDATE monitor_plan SET next_status_check_at='2000-01-01T00:00:00+00:00' WHERE target_uid=?",
            (target_uid,),
        ).result(timeout=5)

        scheduler = PlanStateScheduler(db, writer)
        assert scheduler.run_once() == 1
        assert scheduler.run_once() == 0

        with db.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT job_type,status,payload_json FROM background_job WHERE job_type=?",
                (PLAN_STATUS_CHECK,),
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["status"] == "QUEUED"
        assert target_uid in rows[0]["payload_json"]
    finally:
        writer.close()


def test_scheduler_ignores_active_terminal_and_disabled_targets(tmp_path: Path) -> None:
    db = _database(tmp_path)
    writer = StorageWriter(db)
    writer.start()
    try:
        _seed_account(db, writer)
        store = MonitorPlanStore(db, writer)
        active = store.enroll_verified(_plan("910002", "DELIVERY_OK"))
        terminal = store.enroll_verified(_plan("910003", "DELETED"))
        disabled = store.enroll_verified(_plan("910004", "OFFLINE_BUDGET"))
        store.set_monitor_enabled(disabled, False)
        writer.execute(
            "UPDATE monitor_plan SET next_status_check_at='2000-01-01T00:00:00+00:00' WHERE target_uid IN(?,?,?)",
            (active, terminal, disabled),
        ).result(timeout=5)

        scheduler = PlanStateScheduler(db, writer)
        assert scheduler.run_once() == 0
    finally:
        writer.close()


def test_handler_success_moves_watching_to_active_and_stops_ten_minute_check(tmp_path: Path) -> None:
    db = _database(tmp_path)
    writer = StorageWriter(db)
    writer.start()
    try:
        _seed_account(db, writer)
        store = MonitorPlanStore(db, writer)
        target_uid = store.enroll_verified(_plan("910005", "OFFLINE_BUDGET"))
        jobs = PersistentJobStore(db, writer)
        job_uid = jobs.enqueue(PLAN_STATUS_CHECK, {"target_uid": target_uid})
        claimed = jobs.claim_next("worker-1", job_types=[PLAN_STATUS_CHECK])
        assert claimed is not None and claimed.job_uid == job_uid

        class Monitor:
            def check_active_state(self, requested_target_uid):
                assert requested_target_uid == target_uid
                now = "2026-08-30T00:01:00+00:00"
                writer.execute(
                    """UPDATE monitor_plan SET official_status='DELIVERY_OK',lifecycle_state='ACTIVE_COLLECTING',
                       collection_active=1,strategy_eligible=1,write_eligible=1,sync_state='TRUSTED',
                       next_status_check_at=NULL,next_hot_collect_at=?,updated_at=? WHERE target_uid=?""",
                    (now, now, target_uid),
                ).result(timeout=5)
                return _plan("910005", "DELIVERY_OK")

        handler = PlanStateCheckHandler(db, writer, store, Monitor())  # type: ignore[arg-type]
        result = handler(claimed)
        assert result["lifecycle_state"] == "ACTIVE_COLLECTING"
        target = store.get_target(target_uid)
        assert target["collection_active"] == 1
        assert target["next_status_check_at"] is None
        assert target["next_hot_collect_at"] is not None
    finally:
        writer.close()


def test_handler_error_freezes_only_plan_and_records_error_event(tmp_path: Path) -> None:
    db = _database(tmp_path)
    writer = StorageWriter(db)
    writer.start()
    try:
        _seed_account(db, writer)
        store = MonitorPlanStore(db, writer)
        target_uid = store.enroll_verified(_plan("910006", "OFFLINE_BUDGET"))
        jobs = PersistentJobStore(db, writer)
        jobs.enqueue(PLAN_STATUS_CHECK, {"target_uid": target_uid})
        claimed = jobs.claim_next("worker-1", job_types=[PLAN_STATUS_CHECK])
        assert claimed is not None

        class RetryableError(RuntimeError):
            retryable = True
            code = "40130"

        class Monitor:
            def check_active_state(self, requested_target_uid):
                raise RetryableError("temporary failure")

        handler = PlanStateCheckHandler(db, writer, store, Monitor())  # type: ignore[arg-type]
        with pytest.raises(RetryableError):
            handler(claimed)

        target = store.get_target(target_uid)
        assert target["lifecycle_state"] == "WATCHING"
        assert target["collection_active"] == 0
        assert target["strategy_eligible"] == 0
        assert target["write_eligible"] == 0
        assert target["sync_state"] == "STATUS_CHECK_ERROR"
        assert target["next_status_check_at"] is not None
        # 上次可信平台状态不能被本地错误字符串覆盖。
        assert target["official_status"] == "OFFLINE_BUDGET"

        with db.connect(readonly=True) as conn:
            error = conn.execute(
                "SELECT * FROM api_error_event WHERE advertiser_id='222222' AND ad_id='910006'"
            ).fetchone()
        assert error is not None
        assert error["module"] == "qianchuan.plan_state"
        assert error["api_code"] == "40130"
        assert error["retryable"] == 1
        assert "CREATE_RETARGET" in error["blocked_capabilities_json"]
    finally:
        writer.close()
