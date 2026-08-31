from __future__ import annotations

from pathlib import Path

import pytest

from commercial_v1.diagnostics.service import DiagnosticsService
from commercial_v1.license.runtime_state import LicenseRuntimeStateStore
from commercial_v1.runtime.jobs import ClaimedJob, PersistentJobStore
from commercial_v1.runtime.recovery import StartupRecoveryService
from commercial_v1.storage.database import Database, DatabaseConfig
from commercial_v1.storage.health import DatabaseHealthService
from commercial_v1.storage.schema import create_schema_v1
from commercial_v1.storage.writer import StorageWriter
from commercial_v1.strategy import (
    STRATEGY_MATERIAL_EVALUATE,
    StrategyEvaluationEnqueuer,
    StrategyEvaluationHandler,
    StrategyEvaluationService,
    StrategyStore,
)

NOW = "2026-08-30T08:00:00+00:00"


def _env(tmp_path: Path):
    db = Database(DatabaseConfig(tmp_path / "runtime.db"))
    with db.connect() as conn:
        create_schema_v1(conn)
    writer = StorageWriter(db)
    writer.start()
    writer.execute(
        """INSERT INTO qianchuan_account(
           account_uid,advertiser_id,enabled,auth_status,created_at,updated_at
           ) VALUES('acc','111111',1,'ACTIVE',?,?)""",
        (NOW, NOW),
    ).result(timeout=5)
    writer.execute(
        """INSERT INTO monitor_plan(
           target_uid,account_uid,advertiser_id,ad_id,plan_name,plan_system,promotion_scene,
           official_status,monitor_enabled,lifecycle_state,collection_active,strategy_eligible,
           write_eligible,sync_state,created_at,updated_at
           ) VALUES('target','acc','111111','222222','plan','UNI_PROJECT','VIDEO_PROM_GOODS',
                    'DELIVERY_OK',1,'ACTIVE_COLLECTING',1,1,1,'TRUSTED',?,?)""",
        (NOW, NOW),
    ).result(timeout=5)
    jobs = PersistentJobStore(db, writer)
    store = StrategyStore(db, writer)
    service = StrategyEvaluationService(db, writer, store)
    return db, writer, jobs, store, service


def _condition(field: str, op: str, value: str):
    return {"logic": "AND", "conditions": [{"field": field, "op": op, "value": value}]}


def _insert_material_source(
    writer: StorageWriter,
    *,
    batch_id: str,
    batch_status: str = "SUCCESS",
    sync_state: str = "TRUSTED",
    strategy_eligible: int = 1,
) -> None:
    writer.execute(
        """INSERT INTO collection_batch(
           batch_id,account_uid,target_uid,advertiser_id,ad_id,pipeline_type,scheduled_at,
           started_at,finished_at,status,raw_row_count,unique_row_count,created_at
           ) VALUES(?,?,?,?,?,'MATERIAL_5M',?,?,?,?,1,1,?)""",
        (batch_id, "acc", "target", "111111", "222222", NOW, NOW, NOW, batch_status, NOW),
    ).result(timeout=5)
    if batch_status != "SUCCESS":
        return
    writer.execute(
        """INSERT INTO material_registry(
           material_uid,advertiser_id,ad_id,material_id,first_seen_at,last_seen_at,first_active_at,
           last_active_at,last_official_status,created_at,updated_at
           ) VALUES('material:111111:222222:900001','111111','222222','900001',?,?,?,?,
                    'DELIVERY_OK',?,?)""",
        (NOW, NOW, NOW, NOW, NOW, NOW),
    ).result(timeout=5)
    writer.execute(
        """INSERT INTO material_latest(
           material_uid,advertiser_id,ad_id,material_id,official_material_status,official_audit_status,
           overall_cost_decimal,net_settle_amount_decimal,net_settle_roi_decimal,net_settle_order_count,
           overall_order_count,overall_gmv_decimal,overall_pay_roi_decimal,stat_date,collected_at,batch_id,
           request_id,sync_state,strategy_eligible,updated_at
           ) VALUES('material:111111:222222:900001','111111','222222','900001','DELIVERY_OK','PASS',
                    '120','150','1.5',2,3,'200','1.66','2026-08-30',?,?, 'rid',?,?,?)""",
        (NOW, batch_id, sync_state, strategy_eligible, NOW),
    ).result(timeout=5)


def test_strategy_enqueuer_only_creates_jobs_for_enabled_matching_strategy(tmp_path: Path) -> None:
    db, writer, jobs, store, _ = _env(tmp_path)
    try:
        enqueuer = StrategyEvaluationEnqueuer(jobs, store)
        assert enqueuer("target", "MATERIAL_5M", "batch-0") is None

        store.create_strategy(
            strategy_name="control-only",
            strategy_type="CONTROL_STOP",
            target_uid="target",
            execution_mode="MANUAL",
            priority=10,
            conditions=_condition("assist_cost_decimal", "GTE", "100"),
        )
        assert enqueuer("target", "MATERIAL_5M", "batch-1") is None

        material = store.create_strategy(
            strategy_name="material",
            strategy_type="MATERIAL_RETARGET",
            target_uid="target",
            execution_mode="MANUAL",
            priority=20,
            conditions=_condition("overall_cost_decimal", "GTE", "100"),
        )
        uid = enqueuer("target", "MATERIAL_5M", "batch-2")
        assert uid is not None
        assert enqueuer("target", "MATERIAL_5M", "batch-2") == uid
        with db.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT job_uid,job_type,status FROM background_job ORDER BY created_at,job_uid"
            ).fetchall()
            assert len(rows) == 1
            assert rows[0]["job_uid"] == uid
            assert rows[0]["job_type"] == STRATEGY_MATERIAL_EVALUATE
            assert rows[0]["status"] == "QUEUED"

        store.set_enabled(material.strategy_id, False)
        assert enqueuer("target", "MATERIAL_5M", "batch-3") is None
    finally:
        writer.close()


def test_strategy_handler_does_not_evaluate_when_license_is_blocked() -> None:
    class NeverCallService:
        def evaluate_material_batch(self, target_uid: str, source_batch_id: str):
            raise AssertionError("strategy evaluation must not run while license is blocked")

    handler = StrategyEvaluationHandler(
        NeverCallService(),  # type: ignore[arg-type]
        STRATEGY_MATERIAL_EVALUATE,
        business_allowed=lambda: False,
    )
    job = ClaimedJob(
        job_uid="job",
        job_type=STRATEGY_MATERIAL_EVALUATE,
        priority=60,
        payload={"target_uid": "target", "source_batch_id": "batch"},
        due_at=NOW,
        owner_instance_id="worker",
        fencing_token=1,
        lease_expires_at="2099-01-01T00:00:00+00:00",
    )
    result = handler(job)
    assert result["skipped"] == "LICENSE_BLOCKED"


def test_untrusted_latest_never_produces_strategy_hit(tmp_path: Path) -> None:
    db, writer, _, store, service = _env(tmp_path)
    try:
        _insert_material_source(
            writer,
            batch_id="untrusted-batch",
            sync_state="SUSPICIOUS_EMPTY",
            strategy_eligible=0,
        )
        store.create_strategy(
            strategy_name="retarget",
            strategy_type="MATERIAL_RETARGET",
            target_uid="target",
            execution_mode="AUTO",
            priority=10,
            conditions=_condition("overall_cost_decimal", "GTE", "1"),
        )
        summary = service.evaluate_material_batch("target", "untrusted-batch")
        assert summary.evaluated == 0
        assert summary.hit == 0
        assert summary.persisted_hits == 0
        with db.connect(readonly=True) as conn:
            assert conn.execute("SELECT COUNT(*) FROM strategy_hit").fetchone()[0] == 0
    finally:
        writer.close()


def test_non_success_hot_batch_is_rejected_as_strategy_source(tmp_path: Path) -> None:
    _, writer, _, store, service = _env(tmp_path)
    try:
        _insert_material_source(writer, batch_id="bad-batch", batch_status="SUSPICIOUS_EMPTY")
        store.create_strategy(
            strategy_name="retarget",
            strategy_type="MATERIAL_RETARGET",
            target_uid="target",
            execution_mode="MANUAL",
            priority=10,
            conditions=_condition("overall_cost_decimal", "GTE", "1"),
        )
        with pytest.raises(ValueError, match="trusted successful hot batch"):
            service.evaluate_material_batch("target", "bad-batch")
    finally:
        writer.close()


def test_startup_recovery_requeues_expired_strategy_job(tmp_path: Path) -> None:
    db, writer, jobs, _, _ = _env(tmp_path)
    try:
        writer.execute(
            """INSERT INTO background_job(
               job_uid,job_type,priority,payload_json,status,due_at,lease_owner,lease_expires_at,
               fencing_token,created_at,started_at,updated_at
               ) VALUES('strategy-running',?,60,'{}','RUNNING',?,'old-worker',
                        '2000-01-01T00:00:00+00:00',7,?,?,?)""",
            (STRATEGY_MATERIAL_EVALUATE, NOW, NOW, NOW, NOW),
        ).result(timeout=5)
        report = StartupRecoveryService(db, writer, jobs).run()
        assert report.job_recovery["requeue"] == 1
        with db.connect(readonly=True) as conn:
            row = conn.execute(
                "SELECT status,lease_owner,lease_expires_at FROM background_job WHERE job_uid='strategy-running'"
            ).fetchone()
            assert row["status"] == "QUEUED"
            assert row["lease_owner"] is None
            assert row["lease_expires_at"] is None
    finally:
        writer.close()


def test_diagnostics_exposes_strategy_queue_hits_and_suppression(tmp_path: Path) -> None:
    db, writer, jobs, store, _ = _env(tmp_path)
    try:
        version = store.create_strategy(
            strategy_name="retarget",
            strategy_type="MATERIAL_RETARGET",
            target_uid="target",
            execution_mode="MANUAL",
            priority=10,
            conditions=_condition("overall_cost_decimal", "GTE", "1"),
        )
        jobs.enqueue(
            STRATEGY_MATERIAL_EVALUATE,
            {"target_uid": "target", "source_batch_id": "future-batch"},
            priority=60,
            job_uid="strategy-queued",
        )
        writer.execute(
            """INSERT INTO strategy_hit(
               hit_id,strategy_id,strategy_version_id,target_uid,object_type,object_uid,
               advertiser_id,ad_id,material_id,evaluated_at,source_collected_at,result,
               condition_snapshot_json,metric_snapshot_json,suppression_reason,winner_strategy_id
               ) VALUES('hit-1',?,?, 'target','MATERIAL','material:111111:222222:900001',
                        '111111','222222','900001',?,?,'HIT','[]','{}',
                        'SUPPRESSED_BY_HIGHER_PRIORITY',?)""",
            (version.strategy_id, version.strategy_version_id, NOW, NOW, version.strategy_id),
        ).result(timeout=5)

        snapshot = DiagnosticsService(
            db,
            DatabaseHealthService(db),
            writer,
            jobs,
            LicenseRuntimeStateStore(db, writer),
        ).snapshot()
        assert snapshot["strategy"]["enabled_strategies"] == 1
        assert snapshot["strategy"]["queued_or_running_by_type"][STRATEGY_MATERIAL_EVALUATE] == 1
        assert snapshot["strategy"]["hit_rows"] == 1
        assert snapshot["strategy"]["suppressed_hit_rows"] == 1
        assert snapshot["strategy"]["recent_hits"][0]["hit_id"] == "hit-1"
    finally:
        writer.close()
