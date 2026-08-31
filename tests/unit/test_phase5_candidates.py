from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from commercial_v1.candidate import (
    APPROVED,
    CANDIDATE_BUILD,
    EXPIRED,
    MERGED,
    REJECTED,
    WAITING_CONFIRMATION,
    CandidateBuildEnqueuer,
    CandidateBuildHandler,
    CandidateService,
)
from commercial_v1.runtime.jobs import ClaimedJob, PersistentJobStore
from commercial_v1.runtime.recovery import StartupRecoveryService
from commercial_v1.storage.database import Database, DatabaseConfig
from commercial_v1.storage.schema import create_schema_v1
from commercial_v1.storage.writer import StorageWriter
from commercial_v1.strategy import StrategyStore, StrategyVersion

NOW_DT = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)
NOW = NOW_DT.isoformat(timespec="seconds")


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs) -> None:
        self.value = self.value + timedelta(**kwargs)


def _env(tmp_path: Path, clock: MutableClock | None = None):
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
    store = StrategyStore(db, writer)
    service = CandidateService(db, writer, clock=clock or MutableClock(NOW_DT))
    jobs = PersistentJobStore(db, writer)
    return db, writer, store, service, jobs


def _condition(field: str):
    return {"logic": "AND", "conditions": [{"field": field, "op": "GTE", "value": "1"}]}


def _strategy(
    store: StrategyStore,
    *,
    name: str = "retarget",
    strategy_type: str = "MATERIAL_RETARGET",
    execution_mode: str = "MANUAL",
    grouping_mode: str = "SEPARATE",
    action_config: dict | None = None,
) -> StrategyVersion:
    field = "overall_cost_decimal" if strategy_type == "MATERIAL_RETARGET" else "assist_cost_decimal"
    return store.create_strategy(
        strategy_name=name,
        strategy_type=strategy_type,
        target_uid="target",
        execution_mode=execution_mode,
        priority=10,
        conditions=_condition(field),
        action_config=action_config or {"budget": "500", "duration": "2"},
        grouping_mode=grouping_mode,
    )


def _batch(writer: StorageWriter, batch_id: str, *, pipeline: str = "MATERIAL_5M") -> None:
    writer.execute(
        """INSERT INTO collection_batch(
           batch_id,account_uid,target_uid,advertiser_id,ad_id,pipeline_type,scheduled_at,
           started_at,finished_at,status,raw_row_count,unique_row_count,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,'SUCCESS',1,1,?)""",
        (batch_id, "acc", "target", "111111", "222222", pipeline, NOW, NOW, NOW, NOW),
    ).result(timeout=5)


def _hit(
    writer: StorageWriter,
    version: StrategyVersion,
    *,
    hit_id: str,
    batch_id: str,
    object_uid: str,
    material_id: str | None = None,
    control_task_id: str | None = None,
    metric_value: str = "10",
    suppression_reason: str | None = None,
) -> None:
    metric_field = "overall_cost_decimal" if version.object_type == "MATERIAL" else "assist_cost_decimal"
    writer.execute(
        """INSERT INTO strategy_hit(
           hit_id,strategy_id,strategy_version_id,target_uid,object_type,object_uid,
           advertiser_id,ad_id,material_id,control_task_id,evaluated_at,source_collected_at,
           source_batch_id,result,condition_snapshot_json,metric_snapshot_json,
           suppression_reason,winner_strategy_id
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'HIT',?,?,?,?)""",
        (
            hit_id,
            version.strategy_id,
            version.strategy_version_id,
            "target",
            version.object_type,
            object_uid,
            "111111",
            "222222",
            material_id,
            control_task_id,
            NOW,
            NOW,
            batch_id,
            json.dumps([{"field": metric_field, "actual": metric_value}], separators=(",", ":")),
            json.dumps({metric_field: metric_value}, separators=(",", ":")),
            suppression_reason,
            version.strategy_id,
        ),
    ).result(timeout=5)


def test_merged_retarget_freezes_parameters_and_splits_at_twenty(tmp_path: Path) -> None:
    db, writer, store, service, _ = _env(tmp_path)
    try:
        version = _strategy(
            store,
            grouping_mode=MERGED,
            action_config={"budget": "888.80", "duration": "3", "roi_goal": "2.5"},
        )
        _batch(writer, "batch-1")
        for index in range(21):
            material_id = str(900000 + index)
            _hit(
                writer,
                version,
                hit_id=f"hit-{index}",
                batch_id="batch-1",
                object_uid=f"material:111111:222222:{material_id}",
                material_id=material_id,
                metric_value=str(100 + index),
            )

        summary = service.build_from_source_batch("target", "batch-1")
        assert summary.eligible_hits == 21
        assert summary.built_candidates == 2
        assert summary.existing_candidates == 0
        with db.connect(readonly=True) as conn:
            candidates = conn.execute(
                "SELECT * FROM candidate_batch ORDER BY candidate_id"
            ).fetchall()
            assert len(candidates) == 2
            assert {row["status"] for row in candidates} == {WAITING_CONFIRMATION}
            assert {row["grouping_mode"] for row in candidates} == {MERGED}
            assert {
                json.loads(row["execution_params_json"])["budget"] for row in candidates
            } == {"888.80"}
            counts = sorted(
                conn.execute(
                    "SELECT COUNT(*) FROM candidate_item WHERE candidate_id=?",
                    (row["candidate_id"],),
                ).fetchone()[0]
                for row in candidates
            )
            assert counts == [1, 20]
            sample = conn.execute(
                "SELECT metric_snapshot_json,before_state_json FROM candidate_item ORDER BY object_uid LIMIT 1"
            ).fetchone()
            assert "overall_cost_decimal" in json.loads(sample["metric_snapshot_json"])
            assert json.loads(sample["before_state_json"])["source_batch_id"] == "batch-1"

        second = service.build_from_source_batch("target", "batch-1")
        assert second.built_candidates == 0
        assert second.existing_candidates == 2
        with db.connect(readonly=True) as conn:
            assert conn.execute("SELECT COUNT(*) FROM candidate_batch").fetchone()[0] == 2
            assert conn.execute("SELECT COUNT(*) FROM candidate_item").fetchone()[0] == 21
    finally:
        writer.close()


def test_auto_candidate_is_locally_approved_without_confirmation_expiry(tmp_path: Path) -> None:
    _, writer, store, service, _ = _env(tmp_path)
    try:
        version = _strategy(store, execution_mode="AUTO")
        _batch(writer, "batch-auto")
        _hit(
            writer,
            version,
            hit_id="hit-auto",
            batch_id="batch-auto",
            object_uid="material:111111:222222:900001",
            material_id="900001",
        )
        summary = service.build_from_source_batch("target", "batch-auto")
        candidate = service.get(summary.candidate_ids[0])
        assert candidate is not None
        assert candidate["status"] == APPROVED
        assert candidate["approved_at"] == NOW
        assert candidate["expires_at"] is None
    finally:
        writer.close()


def test_suppressed_hit_never_enters_candidate(tmp_path: Path) -> None:
    db, writer, store, service, _ = _env(tmp_path)
    try:
        version = _strategy(store)
        _batch(writer, "batch-suppressed")
        _hit(
            writer,
            version,
            hit_id="winner",
            batch_id="batch-suppressed",
            object_uid="material:111111:222222:900001",
            material_id="900001",
        )
        _hit(
            writer,
            version,
            hit_id="loser",
            batch_id="batch-suppressed",
            object_uid="material:111111:222222:900002",
            material_id="900002",
            suppression_reason="SUPPRESSED_BY_HIGHER_PRIORITY",
        )
        summary = service.build_from_source_batch("target", "batch-suppressed")
        assert summary.eligible_hits == 1
        assert summary.built_candidates == 1
        with db.connect(readonly=True) as conn:
            items = conn.execute("SELECT material_id FROM candidate_item").fetchall()
            assert [row["material_id"] for row in items] == ["900001"]
    finally:
        writer.close()


def test_active_tool_retarget_guards_material_but_ended_task_releases_next_cycle(tmp_path: Path) -> None:
    db, writer, store, service, _ = _env(tmp_path)
    try:
        version = _strategy(store)
        _batch(writer, "batch-guard")
        _hit(
            writer,
            version,
            hit_id="hit-guard",
            batch_id="batch-guard",
            object_uid="material:111111:222222:900001",
            material_id="900001",
        )
        writer.execute(
            """INSERT INTO control_task_registry(
               control_task_uid,advertiser_id,ad_id,control_task_id,first_seen_at,last_seen_at,
               first_processing_at,last_official_status,material_count,created_by_tool,created_at,updated_at
               ) VALUES('control:1','111111','222222','700001',?,?,?,'PROCESSING',1,1,?,?)""",
            (NOW, NOW, NOW, NOW, NOW),
        ).result(timeout=5)
        writer.execute(
            """INSERT INTO control_task_latest(
               control_task_uid,advertiser_id,ad_id,control_task_id,official_task_status,stat_date,
               collected_at,batch_id,sync_state,strategy_eligible,write_eligible,updated_at
               ) VALUES('control:1','111111','222222','700001','PROCESSING','2026-08-31',?,
                        'batch-guard','TRUSTED',1,1,?)""",
            (NOW, NOW),
        ).result(timeout=5)
        writer.execute(
            """INSERT INTO control_task_material(control_task_uid,material_uid,material_id,observed_at)
               VALUES('control:1',NULL,'900001',?)""",
            (NOW,),
        ).result(timeout=5)

        guarded = service.build_from_source_batch("target", "batch-guard")
        assert guarded.built_candidates == 0
        assert guarded.skipped_active_guard == 1

        writer.execute(
            "UPDATE control_task_latest SET official_task_status='DISABLE',strategy_eligible=0,write_eligible=0 WHERE control_task_uid='control:1'"
        ).result(timeout=5)
        _batch(writer, "batch-after-end")
        _hit(
            writer,
            version,
            hit_id="hit-after-end",
            batch_id="batch-after-end",
            object_uid="material:111111:222222:900001",
            material_id="900001",
        )
        released = service.build_from_source_batch("target", "batch-after-end")
        assert released.built_candidates == 1
        assert released.skipped_active_guard == 0
        with db.connect(readonly=True) as conn:
            assert conn.execute("SELECT COUNT(*) FROM candidate_batch").fetchone()[0] == 1
    finally:
        writer.close()


def test_reject_cooldown_blocks_only_same_strategy_object_until_expiry(tmp_path: Path) -> None:
    clock = MutableClock(NOW_DT)
    _, writer, store, service, _ = _env(tmp_path, clock)
    try:
        version = _strategy(store)
        _batch(writer, "batch-r1")
        _hit(
            writer,
            version,
            hit_id="hit-r1",
            batch_id="batch-r1",
            object_uid="material:111111:222222:900001",
            material_id="900001",
        )
        first = service.build_from_source_batch("target", "batch-r1")
        decision = service.reject(first.candidate_ids[0])
        assert decision.status == REJECTED
        assert decision.changed is True
        assert decision.reject_cooldown_until is not None
        assert service.reject(first.candidate_ids[0]).changed is False

        _batch(writer, "batch-r2")
        _hit(
            writer,
            version,
            hit_id="hit-r2",
            batch_id="batch-r2",
            object_uid="material:111111:222222:900001",
            material_id="900001",
        )
        blocked = service.build_from_source_batch("target", "batch-r2")
        assert blocked.built_candidates == 0
        assert blocked.skipped_reject_cooldown == 1

        clock.advance(minutes=31)
        _batch(writer, "batch-r3")
        _hit(
            writer,
            version,
            hit_id="hit-r3",
            batch_id="batch-r3",
            object_uid="material:111111:222222:900001",
            material_id="900001",
        )
        allowed = service.build_from_source_batch("target", "batch-r3")
        assert allowed.built_candidates == 1
    finally:
        writer.close()


def test_manual_confirmation_is_idempotent_and_expired_click_cannot_approve(tmp_path: Path) -> None:
    clock = MutableClock(NOW_DT)
    _, writer, store, service, _ = _env(tmp_path, clock)
    try:
        version = _strategy(store)
        _batch(writer, "batch-approve")
        _hit(
            writer,
            version,
            hit_id="hit-approve",
            batch_id="batch-approve",
            object_uid="material:111111:222222:900001",
            material_id="900001",
        )
        first = service.build_from_source_batch("target", "batch-approve")
        approved = service.approve(first.candidate_ids[0])
        assert approved.status == APPROVED
        assert approved.changed is True
        assert service.approve(first.candidate_ids[0]).changed is False

        _batch(writer, "batch-expire")
        _hit(
            writer,
            version,
            hit_id="hit-expire",
            batch_id="batch-expire",
            object_uid="material:111111:222222:900002",
            material_id="900002",
        )
        expiring = service.build_from_source_batch("target", "batch-expire")
        candidate_id = expiring.candidate_ids[0]
        assert service.get(candidate_id)["status"] == WAITING_CONFIRMATION  # type: ignore[index]
        clock.advance(minutes=31)
        expired = service.approve(candidate_id)
        assert expired.status == EXPIRED
        assert service.get(candidate_id)["status"] == EXPIRED  # type: ignore[index]
    finally:
        writer.close()


def test_candidate_job_is_idempotent_license_gated_and_restart_recoverable(tmp_path: Path) -> None:
    db, writer, _, service, jobs = _env(tmp_path)
    try:
        enqueuer = CandidateBuildEnqueuer(jobs)
        uid = enqueuer("target", "batch-x")
        assert enqueuer("target", "batch-x") == uid
        with db.connect(readonly=True) as conn:
            assert conn.execute("SELECT COUNT(*) FROM background_job WHERE job_uid=?", (uid,)).fetchone()[0] == 1

        class NeverCallCandidateService:
            def build_from_source_batch(self, target_uid: str, source_batch_id: str):
                raise AssertionError("candidate build must not run while license is blocked")

        handler = CandidateBuildHandler(
            NeverCallCandidateService(),  # type: ignore[arg-type]
            business_allowed=lambda: False,
        )
        job = ClaimedJob(
            job_uid="blocked",
            job_type=CANDIDATE_BUILD,
            priority=70,
            payload={"target_uid": "target", "source_batch_id": "batch-x"},
            due_at=NOW,
            owner_instance_id="candidate-worker",
            fencing_token=1,
            lease_expires_at="2099-01-01T00:00:00+00:00",
        )
        assert handler(job)["skipped"] == "LICENSE_BLOCKED"

        writer.execute(
            """UPDATE background_job SET status='RUNNING',lease_owner='dead-worker',
               lease_expires_at='2000-01-01T00:00:00+00:00',fencing_token=9,started_at=?
               WHERE job_uid=?""",
            (NOW, uid),
        ).result(timeout=5)
        report = StartupRecoveryService(db, writer, jobs).run()
        assert report.job_recovery["requeue"] == 1
        with db.connect(readonly=True) as conn:
            assert conn.execute("SELECT status FROM background_job WHERE job_uid=?", (uid,)).fetchone()[0] == "QUEUED"
    finally:
        writer.close()
