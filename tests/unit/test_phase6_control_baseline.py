from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from commercial_v1.candidate import CandidateService
from commercial_v1.execution import CANCELLED, EXECUTION_APPROVED, ExecutionPreflightService, ExecutionService
from commercial_v1.qianchuan.client import ApiResponse
from commercial_v1.qianchuan.contracts import CONTROL_TASK_LIST
from commercial_v1.storage.database import Database, DatabaseConfig
from commercial_v1.storage.schema import create_schema_v1
from commercial_v1.storage.writer import StorageWriter

NOW_DT = datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc)
NOW = NOW_DT.isoformat(timespec="seconds")


class FakeTokens:
    def get_access_token(self, _auth_profile_id: str, *, force_refresh: bool = False) -> str:
        return "refreshed" if force_refresh else "token"


class FakePlans:
    def get_detail(self, _auth_profile_id, advertiser_id, ad_id, **_kwargs):
        return (
            SimpleNamespace(
                advertiser_id=str(advertiser_id),
                ad_id=str(ad_id),
                classification_status="VERIFIED",
                official_status="DELIVERY_OK",
                modify_time="2026-09-01 08:00:00",
                budget_decimal="1000",
            ),
            "plan-request",
        )


class FakeControlClient:
    def __init__(self, *, budget: str = "100", duration: str = "2") -> None:
        self.budget = budget
        self.duration = duration

    def get(self, endpoint, *, query, access_token, advertiser_id=""):
        assert endpoint == CONTROL_TASK_LIST
        row = {
            "id": "700001",
            "scene": "MATERIAL_ADD_BUDGET",
            "name": "task",
            "task_status": "PROCESSING",
            "budget": self.budget,
            "duration": self.duration,
            "bid": "1.2",
            "roi2_goal": "2.3",
            "material_list": [{"material_id": "900001"}],
            "metrics": {},
        }
        return ApiResponse(
            data={
                "task_list": [row],
                "page_info": {
                    "page": int(query.get("page", 1)),
                    "page_size": int(query.get("page_size", 100)),
                    "total_number": 1,
                },
            },
            raw={},
            request_id="control-request",
            code="0",
            message="",
            local_request_uid="local-control-request",
        )


def _env(tmp_path: Path):
    db = Database(DatabaseConfig(tmp_path / "runtime.db"))
    with db.connect() as conn:
        create_schema_v1(conn)
    writer = StorageWriter(db)
    writer.start()
    writer.execute(
        """INSERT INTO qianchuan_auth_profile(
           auth_profile_id,app_id,encrypted_app_secret,auth_status,created_at,updated_at
           ) VALUES('auth','123456','encrypted','ACTIVE',?,?)""",
        (NOW, NOW),
    ).result(timeout=5)
    writer.execute(
        """INSERT INTO qianchuan_account(
           account_uid,advertiser_id,enabled,auth_status,created_at,updated_at
           ) VALUES('acc','111111',1,'ACTIVE',?,?)""",
        (NOW, NOW),
    ).result(timeout=5)
    writer.execute(
        """INSERT INTO qianchuan_account_auth(
           account_uid,auth_profile_id,is_primary,bound_at,created_at
           ) VALUES('acc','auth',1,?,?)""",
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
    return db, writer


def _seed_control_candidate_source(
    writer: StorageWriter,
    *,
    action_type: str,
    batch_id: str = "batch-control",
    latest_batch_id: str | None = None,
    budget: str | None = "100",
    duration: str | None = "2",
) -> None:
    latest_batch = latest_batch_id or batch_id
    writer.execute(
        """INSERT INTO collection_batch(
           batch_id,account_uid,target_uid,advertiser_id,ad_id,pipeline_type,scheduled_at,
           started_at,finished_at,status,raw_row_count,unique_row_count,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,'SUCCESS',1,1,?)""",
        (batch_id, "acc", "target", "111111", "222222", "CONTROL_5M", NOW, NOW, NOW, NOW),
    ).result(timeout=5)
    if latest_batch != batch_id:
        writer.execute(
            """INSERT INTO collection_batch(
               batch_id,account_uid,target_uid,advertiser_id,ad_id,pipeline_type,scheduled_at,
               started_at,finished_at,status,raw_row_count,unique_row_count,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,'SUCCESS',1,1,?)""",
            (latest_batch, "acc", "target", "111111", "222222", "CONTROL_5M", NOW, NOW, NOW, NOW),
        ).result(timeout=5)

    writer.execute(
        """INSERT INTO strategy_config(
           strategy_id,strategy_name,strategy_type,target_scope,action_type,execution_mode,
           enabled,priority,current_version_id,created_at,updated_at
           ) VALUES('strategy','control','CONTROL_TEST','PLAN:target',?,'AUTO',1,10,'version',?,?)""",
        (action_type, NOW, NOW),
    ).result(timeout=5)
    writer.execute(
        """INSERT INTO strategy_version(
           strategy_version_id,strategy_id,version_no,condition_json,action_config_json,
           grouping_mode,priority,created_at,content_hash
           ) VALUES('version','strategy',1,'{}',?,'SEPARATE',10,?,'hash')""",
        (json.dumps({"budget": "200", "duration": "3"}, separators=(",", ":")), NOW),
    ).result(timeout=5)
    writer.execute(
        """INSERT INTO control_task_registry(
           control_task_uid,advertiser_id,ad_id,control_task_id,scene,task_name,first_seen_at,last_seen_at,
           first_processing_at,last_official_status,material_count,created_by_tool,created_at,updated_at
           ) VALUES('control:700001','111111','222222','700001','MATERIAL_ADD_BUDGET','task',?,?,?,
                    'PROCESSING',1,0,?,?)""",
        (NOW, NOW, NOW, NOW, NOW),
    ).result(timeout=5)
    writer.execute(
        """INSERT INTO control_task_latest(
           control_task_uid,advertiser_id,ad_id,control_task_id,official_task_status,
           budget_decimal,duration_decimal,bid_decimal,roi_goal_decimal,assist_cost_decimal,
           stat_date,collected_at,batch_id,request_id,sync_state,strategy_eligible,write_eligible,updated_at
           ) VALUES('control:700001','111111','222222','700001','PROCESSING',?,?,?,?,?,
                    '2026-09-01',?,?,?,'TRUSTED',1,1,?)""",
        (budget, duration, "1.2", "2.3", "10", NOW, latest_batch, "candidate-source-request", NOW),
    ).result(timeout=5)
    writer.execute(
        """INSERT INTO strategy_hit(
           hit_id,strategy_id,strategy_version_id,target_uid,object_type,object_uid,advertiser_id,ad_id,
           material_id,control_task_id,evaluated_at,source_collected_at,source_batch_id,result,
           condition_snapshot_json,metric_snapshot_json
           ) VALUES('hit','strategy','version','target','CONTROL_TASK','control:700001','111111','222222',
                    NULL,'700001',?,?,?,'HIT','[]','{\"assist_cost_decimal\":\"10\"}')""",
        (NOW, NOW, batch_id),
    ).result(timeout=5)


def _build_and_prepare(db: Database, writer: StorageWriter, *, action_type: str):
    candidate_service = CandidateService(db, writer, clock=lambda: NOW_DT)
    summary = candidate_service.build_from_source_batch("target", "batch-control")
    assert summary.built_candidates == 1
    candidate = candidate_service.get(summary.candidate_ids[0])
    assert candidate is not None
    before = json.loads(candidate["items"][0]["before_state_json"])
    assert before["control_state_baseline_complete"] is True
    assert before["control_state_snapshot"]["batch_id"] == "batch-control"
    execution = ExecutionService(db, writer, clock=lambda: NOW_DT).prepare_from_candidate(summary.candidate_ids[0])
    frozen = json.loads(ExecutionService(db, writer).get(execution.execution_id)["expected_before_json"])
    assert frozen["external_change_baseline_complete"] is True
    assert frozen["control_candidate_baseline"]["budget_decimal"] == "100"
    assert frozen["control_candidate_baseline"]["duration_decimal"] == "2"
    return execution.execution_id


def test_exact_batch_control_baseline_is_frozen_and_same_budget_allows_preflight(tmp_path: Path) -> None:
    db, writer = _env(tmp_path)
    try:
        _seed_control_candidate_source(writer, action_type="UPDATE_BUDGET")
        execution_id = _build_and_prepare(db, writer, action_type="UPDATE_BUDGET")
        result = ExecutionPreflightService(
            db,
            writer,
            FakeControlClient(budget="100.0", duration="2.00"),
            FakeTokens(),
            FakePlans(),
            clock=lambda: NOW_DT,
        ).preflight(execution_id)
        assert result.status == EXECUTION_APPROVED
        with db.connect(readonly=True) as conn:
            before = json.loads(
                conn.execute("SELECT expected_before_json FROM execution_task WHERE execution_id=?", (execution_id,)).fetchone()[0]
            )
            assert before["external_change_baseline_complete"] is True
            assert before["server_preflight"]["external_change_baseline_complete"] is True
            assert "post_blocker" not in before["server_preflight"]
            assert conn.execute("SELECT COUNT(*) FROM execution_attempt").fetchone()[0] == 0
    finally:
        writer.close()


def test_manual_budget_change_after_candidate_cancels_execution(tmp_path: Path) -> None:
    db, writer = _env(tmp_path)
    try:
        _seed_control_candidate_source(writer, action_type="UPDATE_BUDGET")
        execution_id = _build_and_prepare(db, writer, action_type="UPDATE_BUDGET")
        result = ExecutionPreflightService(
            db,
            writer,
            FakeControlClient(budget="150", duration="2"),
            FakeTokens(),
            FakePlans(),
            clock=lambda: NOW_DT,
        ).preflight(execution_id)
        assert result.status == CANCELLED
        assert result.reason == "CANCELLED_EXTERNAL_CHANGE_BUDGET"
        with db.connect(readonly=True) as conn:
            assert conn.execute("SELECT COUNT(*) FROM execution_attempt").fetchone()[0] == 0
    finally:
        writer.close()


def test_manual_duration_change_after_candidate_cancels_execution(tmp_path: Path) -> None:
    db, writer = _env(tmp_path)
    try:
        _seed_control_candidate_source(writer, action_type="UPDATE_DURATION")
        execution_id = _build_and_prepare(db, writer, action_type="UPDATE_DURATION")
        result = ExecutionPreflightService(
            db,
            writer,
            FakeControlClient(budget="100", duration="9"),
            FakeTokens(),
            FakePlans(),
            clock=lambda: NOW_DT,
        ).preflight(execution_id)
        assert result.status == CANCELLED
        assert result.reason == "CANCELLED_EXTERNAL_CHANGE_DURATION"
    finally:
        writer.close()


def test_control_candidate_is_not_built_when_latest_is_not_exact_source_batch(tmp_path: Path) -> None:
    db, writer = _env(tmp_path)
    try:
        _seed_control_candidate_source(
            writer,
            action_type="UPDATE_BUDGET",
            latest_batch_id="later-batch",
        )
        summary = CandidateService(db, writer, clock=lambda: NOW_DT).build_from_source_batch(
            "target", "batch-control"
        )
        assert summary.built_candidates == 0
        assert summary.skipped_missing_control_baseline == 1
        assert summary.candidate_ids == ()
        with db.connect(readonly=True) as conn:
            assert conn.execute("SELECT COUNT(*) FROM candidate_batch").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM execution_attempt").fetchone()[0] == 0
    finally:
        writer.close()


def test_null_budget_baseline_is_not_comparable_and_blocks_update(tmp_path: Path) -> None:
    db, writer = _env(tmp_path)
    try:
        _seed_control_candidate_source(writer, action_type="UPDATE_BUDGET", budget=None)
        candidate_service = CandidateService(db, writer, clock=lambda: NOW_DT)
        summary = candidate_service.build_from_source_batch("target", "batch-control")
        assert summary.built_candidates == 1
        execution_id = ExecutionService(db, writer, clock=lambda: NOW_DT).prepare_from_candidate(
            summary.candidate_ids[0]
        ).execution_id
        result = ExecutionPreflightService(
            db,
            writer,
            FakeControlClient(budget="100", duration="2"),
            FakeTokens(),
            FakePlans(),
            clock=lambda: NOW_DT,
        ).preflight(execution_id)
        assert result.status == CANCELLED
        assert result.reason == "CONTROL_BUDGET_BASELINE_NOT_COMPARABLE"
    finally:
        writer.close()
