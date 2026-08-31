from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from commercial_v1.execution import (
    CANCELLED,
    EXECUTION_APPROVED,
    EXECUTION_PREFLIGHT,
    EXECUTION_PREPARE,
    PENDING,
    ExecutionPreflightService,
    ExecutionScheduler,
    ExecutionService,
    ExecutionStateError,
    execution_id_for_candidate,
)
from commercial_v1.qianchuan.client import ApiResponse, OpenApiClient
from commercial_v1.qianchuan.contracts import CONTROL_TASK_CREATE, CONTROL_TASK_LIST, MATERIAL_GET
from commercial_v1.qianchuan.errors import OpenApiContractError, OpenApiNetworkError
from commercial_v1.runtime.jobs import PersistentJobStore
from commercial_v1.storage.database import Database, DatabaseConfig
from commercial_v1.storage.schema import create_schema_v1
from commercial_v1.storage.writer import StorageWriter

NOW_DT = datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc)
NOW = NOW_DT.isoformat(timespec="seconds")


class FakeTokenProvider:
    def get_access_token(self, _auth_profile_id: str, *, force_refresh: bool = False) -> str:
        return "token-refreshed" if force_refresh else "token"


class FakePlanCatalog:
    def __init__(self, *, status: str = "DELIVERY_OK", error: Exception | None = None) -> None:
        self.status = status
        self.error = error

    def get_detail(self, _auth_profile_id, advertiser_id, ad_id, **_kwargs):
        if self.error is not None:
            raise self.error
        return (
            SimpleNamespace(
                advertiser_id=str(advertiser_id),
                ad_id=str(ad_id),
                classification_status="VERIFIED",
                official_status=self.status,
                modify_time="2026-09-01 08:00:00",
                budget_decimal="1000",
            ),
            "plan-req",
        )


class FakeReadClient:
    def __init__(self, *, materials=None, controls=None, error: Exception | None = None) -> None:
        self.materials = list(materials or [])
        self.controls = list(controls or [])
        self.error = error
        self.calls: list[str] = []

    def get(self, endpoint, *, query, access_token, advertiser_id=""):
        self.calls.append(endpoint)
        if self.error is not None:
            raise self.error
        if endpoint == MATERIAL_GET:
            rows = self.materials
            key = "ad_material_infos"
        elif endpoint == CONTROL_TASK_LIST:
            rows = self.controls
            key = "task_list"
        else:
            raise AssertionError(f"unexpected endpoint {endpoint}")
        return ApiResponse(
            data={
                key: rows,
                "page_info": {
                    "page": int(query.get("page", 1)),
                    "page_size": int(query.get("page_size", 100)),
                    "total_number": len(rows),
                },
            },
            raw={},
            request_id=f"req-{len(self.calls)}",
            code="0",
            message="",
            local_request_uid=f"local-{len(self.calls)}",
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


def _candidate(
    writer: StorageWriter,
    *,
    candidate_id: str,
    action_type: str = "CREATE_RETARGET",
    status: str = "APPROVED",
    material_id: str | None = "900001",
    control_task_id: str | None = None,
    params: dict | None = None,
) -> None:
    writer.execute(
        """INSERT INTO candidate_batch(
           candidate_id,action_type,advertiser_id,ad_id,execution_mode,grouping_mode,
           execution_params_json,group_fingerprint,status,created_at,approved_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            candidate_id,
            action_type,
            "111111",
            "222222",
            "MANUAL",
            "SEPARATE",
            json.dumps(params or {"budget": "500", "duration": "2"}, separators=(",", ":")),
            f"fingerprint-{candidate_id}",
            status,
            NOW,
            NOW if status == "APPROVED" else None,
        ),
    ).result(timeout=5)
    writer.execute(
        """INSERT INTO candidate_item(
           candidate_item_id,candidate_id,object_uid,material_id,control_task_id,
           metric_snapshot_json,before_state_json,created_at
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (
            f"item-{candidate_id}",
            candidate_id,
            f"object-{candidate_id}",
            material_id,
            control_task_id,
            '{"overall_cost_decimal":"10"}',
            '{"source_batch_id":"batch"}',
            NOW,
        ),
    ).result(timeout=5)


def _material(material_id: str, status: str = "DELIVERY_OK") -> dict:
    return {
        "material_id": material_id,
        "material_status": status,
        "audit_status": "PASS",
        "stats_info": {},
    }


def _control(task_id: str, status: str = "PROCESSING") -> dict:
    return {
        "id": task_id,
        "scene": "MATERIAL_ADD_BUDGET",
        "name": "task",
        "task_status": status,
        "budget": "100",
        "duration": "2",
        "material_list": [{"material_id": "900001"}],
        "metrics": {},
    }


def test_approved_candidate_prepares_one_deterministic_execution_without_attempt(tmp_path: Path) -> None:
    db, writer = _env(tmp_path)
    try:
        _candidate(writer, candidate_id="candidate-1")
        service = ExecutionService(db, writer, clock=lambda: NOW_DT)
        first = service.prepare_from_candidate("candidate-1")
        second = service.prepare_from_candidate("candidate-1")
        assert first.created is True
        assert second.created is False
        assert first.execution_id == second.execution_id == execution_id_for_candidate("candidate-1")
        assert first.status == PENDING
        with db.connect(readonly=True) as conn:
            assert conn.execute("SELECT COUNT(*) FROM execution_task").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM execution_task_material").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM execution_attempt").fetchone()[0] == 0
            row = conn.execute("SELECT expected_before_json FROM execution_task").fetchone()
            before = json.loads(row["expected_before_json"])
            assert before["items"][0]["material_id"] == "900001"
    finally:
        writer.close()


def test_non_approved_candidate_cannot_prepare(tmp_path: Path) -> None:
    db, writer = _env(tmp_path)
    try:
        _candidate(writer, candidate_id="candidate-wait", status="WAITING_CONFIRMATION")
        with pytest.raises(ExecutionStateError):
            ExecutionService(db, writer).prepare_from_candidate("candidate-wait")
        with db.connect(readonly=True) as conn:
            assert conn.execute("SELECT COUNT(*) FROM execution_task").fetchone()[0] == 0
    finally:
        writer.close()


def test_successful_material_preflight_only_approves_local_execution(tmp_path: Path) -> None:
    db, writer = _env(tmp_path)
    try:
        _candidate(writer, candidate_id="candidate-ok")
        execution = ExecutionService(db, writer, clock=lambda: NOW_DT).prepare_from_candidate("candidate-ok")
        client = FakeReadClient(materials=[_material("900001")])
        result = ExecutionPreflightService(
            db, writer, client, FakeTokenProvider(), FakePlanCatalog(), clock=lambda: NOW_DT
        ).preflight(execution.execution_id)
        assert result.status == EXECUTION_APPROVED
        assert result.changed is True
        assert client.calls == [MATERIAL_GET]
        with db.connect(readonly=True) as conn:
            row = conn.execute("SELECT status,expected_before_json FROM execution_task").fetchone()
            assert row["status"] == EXECUTION_APPROVED
            evidence = json.loads(row["expected_before_json"])["server_preflight"]
            assert evidence["plan"]["official_status"] == "DELIVERY_OK"
            assert evidence["materials"][0]["official_material_status"] == "DELIVERY_OK"
            assert conn.execute("SELECT COUNT(*) FROM execution_attempt").fetchone()[0] == 0
    finally:
        writer.close()


@pytest.mark.parametrize(
    ("rows", "reason_prefix"),
    [([], "MATERIAL_NOT_FOUND"), ([_material("900001", "DELIVERY_NOT")], "MATERIAL_NOT_DELIVERY_OK")],
)
def test_missing_or_non_delivering_material_cancels_only_execution(tmp_path: Path, rows, reason_prefix: str) -> None:
    db, writer = _env(tmp_path)
    try:
        _candidate(writer, candidate_id=f"candidate-{reason_prefix}")
        execution = ExecutionService(db, writer, clock=lambda: NOW_DT).prepare_from_candidate(
            f"candidate-{reason_prefix}"
        )
        result = ExecutionPreflightService(
            db, writer, FakeReadClient(materials=rows), FakeTokenProvider(), FakePlanCatalog(), clock=lambda: NOW_DT
        ).preflight(execution.execution_id)
        assert result.status == CANCELLED
        assert (result.reason or "").startswith(reason_prefix)
        with db.connect(readonly=True) as conn:
            assert conn.execute("SELECT COUNT(*) FROM execution_attempt").fetchone()[0] == 0
    finally:
        writer.close()


def test_control_not_processing_cancels_execution(tmp_path: Path) -> None:
    db, writer = _env(tmp_path)
    try:
        _candidate(
            writer,
            candidate_id="candidate-control",
            action_type="PAUSE_CONTROL",
            material_id=None,
            control_task_id="700001",
        )
        execution = ExecutionService(db, writer, clock=lambda: NOW_DT).prepare_from_candidate("candidate-control")
        result = ExecutionPreflightService(
            db,
            writer,
            FakeReadClient(controls=[_control("700001", "DISABLE")]),
            FakeTokenProvider(),
            FakePlanCatalog(),
            clock=lambda: NOW_DT,
        ).preflight(execution.execution_id)
        assert result.status == CANCELLED
        assert (result.reason or "").startswith("CONTROL_TASK_NOT_PROCESSING")
    finally:
        writer.close()


def test_network_failure_keeps_execution_pending(tmp_path: Path) -> None:
    db, writer = _env(tmp_path)
    try:
        _candidate(writer, candidate_id="candidate-network")
        execution = ExecutionService(db, writer, clock=lambda: NOW_DT).prepare_from_candidate("candidate-network")
        preflight = ExecutionPreflightService(
            db,
            writer,
            FakeReadClient(materials=[_material("900001")]),
            FakeTokenProvider(),
            FakePlanCatalog(error=OpenApiNetworkError("offline", retryable=True)),
            clock=lambda: NOW_DT,
        )
        with pytest.raises(OpenApiNetworkError):
            preflight.preflight(execution.execution_id)
        with db.connect(readonly=True) as conn:
            row = conn.execute("SELECT status,cancel_reason FROM execution_task").fetchone()
            assert row["status"] == PENDING
            assert row["cancel_reason"] is None
            assert conn.execute("SELECT COUNT(*) FROM execution_attempt").fetchone()[0] == 0
    finally:
        writer.close()


def test_update_budget_preflight_records_blocking_missing_external_baseline(tmp_path: Path) -> None:
    db, writer = _env(tmp_path)
    try:
        _candidate(
            writer,
            candidate_id="candidate-budget",
            action_type="UPDATE_BUDGET",
            material_id=None,
            control_task_id="700001",
            params={"budget": "200"},
        )
        execution = ExecutionService(db, writer, clock=lambda: NOW_DT).prepare_from_candidate("candidate-budget")
        result = ExecutionPreflightService(
            db,
            writer,
            FakeReadClient(controls=[_control("700001")]),
            FakeTokenProvider(),
            FakePlanCatalog(),
            clock=lambda: NOW_DT,
        ).preflight(execution.execution_id)
        assert result.status == EXECUTION_APPROVED
        with db.connect(readonly=True) as conn:
            before = json.loads(conn.execute("SELECT expected_before_json FROM execution_task").fetchone()[0])
            assert before["external_change_baseline_complete"] is False
            assert before["server_preflight"]["external_change_baseline_complete"] is False
            assert "MISSING_CONTROL_BUDGET_DURATION_BASELINE" in before["server_preflight"]["post_blocker"]
    finally:
        writer.close()


def test_scheduler_detects_approved_candidate_then_pending_execution_without_spam(tmp_path: Path) -> None:
    db, writer = _env(tmp_path)
    try:
        _candidate(writer, candidate_id="candidate-scheduler")
        jobs = PersistentJobStore(db, writer)
        scheduler = ExecutionScheduler(
            db,
            jobs,
            business_allowed=lambda: True,
            interval_seconds=10,
            retry_delay_seconds=60,
        )
        first = scheduler.run_once()
        assert first["prepare_enqueued"] == 1
        assert scheduler.run_once()["prepare_enqueued"] == 0
        with db.connect(readonly=True) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM background_job WHERE job_type=?", (EXECUTION_PREPARE,)
            ).fetchone()[0] == 1

        execution = ExecutionService(db, writer).prepare_from_candidate("candidate-scheduler")
        second = scheduler.run_once()
        assert second["preflight_enqueued"] == 1
        assert scheduler.run_once()["preflight_enqueued"] == 0
        with db.connect(readonly=True) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM background_job WHERE job_type=?", (EXECUTION_PREFLIGHT,)
            ).fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM execution_attempt").fetchone()[0] == 0
        assert execution.status == PENDING
    finally:
        writer.close()


def test_business_post_is_still_blocked_in_open_api_client() -> None:
    client = OpenApiClient()
    with pytest.raises(OpenApiContractError) as exc:
        client.post_oauth(CONTROL_TASK_CREATE, {"advertiser_id": "111111"})
    assert exc.value.code == "PHASE2_POST_FORBIDDEN"
