from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pytest

from commercial_v1.execution.write_ahead import (
    APPROVED,
    ATTEMPT_ACCEPTED,
    ATTEMPT_NOT_SENT,
    ATTEMPT_REJECTED,
    ATTEMPT_UNKNOWN,
    CONFIRMED_FAILED,
    CONFIRMED_SUCCESS,
    RECON_PROVEN_NOT_EXECUTED,
    SUBMITTED,
    UNKNOWN_REQUIRES_REVIEW,
    ExecutionReconciler,
    PreparedWriteRequest,
    ReconcileObservation,
    WriteAheadExecutor,
    WriteGateBlocked,
    WriteResponse,
    WriteTransportError,
)
from commercial_v1.storage.database import Database, DatabaseConfig
from commercial_v1.storage.schema import create_schema_v1
from commercial_v1.storage.writer import StorageWriter

NOW = "2026-09-01T05:00:00+00:00"


class FixedClock:
    def __call__(self):
        from datetime import datetime

        return datetime.fromisoformat(NOW)


class FakeAdapter:
    def __init__(self, outcomes: list[Any], *, on_send=None) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[PreparedWriteRequest] = []
        self.on_send = on_send

    def send(self, request: PreparedWriteRequest) -> WriteResponse:
        self.calls.append(request)
        if self.on_send is not None:
            self.on_send(request)
        if not self.outcomes:
            raise AssertionError("unexpected extra POST")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeReader:
    def __init__(self, observations: list[Any]) -> None:
        self.observations = list(observations)
        self.calls: list[Mapping[str, Any]] = []

    def check(self, execution: Mapping[str, Any]) -> ReconcileObservation:
        self.calls.append(dict(execution))
        if not self.observations:
            raise AssertionError("unexpected extra reconciliation")
        outcome = self.observations.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _env(tmp_path: Path):
    db = Database(DatabaseConfig(tmp_path / "runtime.db"))
    with db.connect() as conn:
        create_schema_v1(conn)
    writer = StorageWriter(db)
    writer.start()
    writer.execute(
        """INSERT INTO execution_task(
           execution_id,advertiser_id,ad_id,action_type,execution_mode,status,
           expected_before_json,expected_after_json,execution_params_json,created_at,approved_at
           ) VALUES('execution-1','111111','222222','CREATE_RETARGET','AUTO','APPROVED',
                    '{}','{"material_ids":["900001"]}','{"budget":"88.8"}',?,?)""",
        (NOW, NOW),
    ).result(timeout=5)
    return db, writer


def _request(*, budget: str = "88.8") -> PreparedWriteRequest:
    # 本测试只验证 Write-Ahead/Unknown/Compensation 机制，不宣称该 payload 已经是
    # 千川 create 正式请求契约；真正请求体将在动作适配器阶段单独封板。
    return PreparedWriteRequest(
        endpoint="/open_api/v1.0/qianchuan/uni_promotion/ad/control_task/create/",
        payload={
            "advertiser_id": "111111",
            "ad_id": "222222",
            "frozen_test_budget": budget,
            "material_ids": ["900001"],
        },
        advertiser_id="111111",
        action_type="CREATE_RETARGET",
    )


def test_write_ahead_is_committed_before_adapter_can_send(tmp_path: Path) -> None:
    db, writer = _env(tmp_path)
    try:
        observed: dict[str, Any] = {}

        def on_send(_request: PreparedWriteRequest) -> None:
            with db.connect(readonly=True) as conn:
                execution = conn.execute(
                    "SELECT status FROM execution_task WHERE execution_id='execution-1'"
                ).fetchone()
                attempt = conn.execute(
                    "SELECT * FROM execution_attempt WHERE execution_id='execution-1'"
                ).fetchone()
            observed["execution_status"] = execution["status"]
            observed["attempt_outcome"] = attempt["outcome"]
            observed["transport_status"] = attempt["transport_status"]
            observed["request_sent_at"] = attempt["request_sent_at"]

        adapter = FakeAdapter(
            [
                WriteResponse(
                    accepted=True,
                    http_status=200,
                    api_code="0",
                    request_id="req-1",
                    response_summary={"id": "700001"},
                    external_object_id="700001",
                )
            ],
            on_send=on_send,
        )
        service = WriteAheadExecutor(db, writer, adapter, clock=FixedClock())
        result = service.submit("execution-1", _request())

        assert observed == {
            "execution_status": "SUBMITTING",
            "attempt_outcome": "PENDING",
            "transport_status": "PREPARED",
            "request_sent_at": None,
        }
        assert result.execution_status == SUBMITTED
        assert result.attempt_outcome == ATTEMPT_ACCEPTED
        assert result.conservative_send_count == 1
        assert len(adapter.calls) == 1
        with db.connect(readonly=True) as conn:
            attempt = conn.execute(
                "SELECT * FROM execution_attempt WHERE execution_id='execution-1'"
            ).fetchone()
            execution = conn.execute(
                "SELECT * FROM execution_task WHERE execution_id='execution-1'"
            ).fetchone()
            reconciliation = conn.execute(
                "SELECT * FROM execution_reconciliation WHERE execution_id='execution-1'"
            ).fetchone()
        assert attempt["request_sent_at"] is not None
        assert attempt["outcome"] == ATTEMPT_ACCEPTED
        assert execution["status"] == SUBMITTED
        assert execution["external_object_id"] == "700001"
        assert reconciliation["status"] == "PENDING"
    finally:
        writer.close()


def test_unknown_transport_never_allows_normal_second_send(tmp_path: Path) -> None:
    db, writer = _env(tmp_path)
    try:
        adapter = FakeAdapter(
            [WriteTransportError("timeout after request write", may_have_been_sent=True)]
        )
        service = WriteAheadExecutor(db, writer, adapter, clock=FixedClock())
        first = service.submit("execution-1", _request())
        assert first.execution_status == UNKNOWN_REQUIRES_REVIEW
        assert first.attempt_outcome == ATTEMPT_UNKNOWN
        assert first.conservative_send_count == 1

        with pytest.raises(WriteGateBlocked):
            service.submit("execution-1", _request())
        assert len(adapter.calls) == 1
        with db.connect(readonly=True) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM execution_attempt WHERE request_sent_at IS NOT NULL"
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT status FROM execution_task WHERE execution_id='execution-1'"
            ).fetchone()[0] == UNKNOWN_REQUIRES_REVIEW
    finally:
        writer.close()


def test_explicit_pre_send_failure_does_not_consume_send_budget(tmp_path: Path) -> None:
    db, writer = _env(tmp_path)
    try:
        adapter = FakeAdapter(
            [
                WriteTransportError("local socket could not open", may_have_been_sent=False),
                WriteResponse(accepted=True, http_status=200, api_code="0", request_id="req-2"),
            ]
        )
        service = WriteAheadExecutor(db, writer, adapter, clock=FixedClock())
        first = service.submit("execution-1", _request())
        assert first.execution_status == APPROVED
        assert first.attempt_outcome == ATTEMPT_NOT_SENT
        assert first.conservative_send_count == 0

        second = service.submit("execution-1", _request())
        assert second.execution_status == SUBMITTED
        assert second.conservative_send_count == 1
        with db.connect(readonly=True) as conn:
            attempts = conn.execute(
                "SELECT attempt_no,outcome,request_sent_at FROM execution_attempt ORDER BY attempt_no"
            ).fetchall()
        assert [row["attempt_no"] for row in attempts] == [1, 2]
        assert attempts[0]["outcome"] == ATTEMPT_NOT_SENT
        assert attempts[0]["request_sent_at"] is None
        assert attempts[1]["outcome"] == ATTEMPT_ACCEPTED
        assert attempts[1]["request_sent_at"] is not None
    finally:
        writer.close()


def test_compensation_requires_proof_same_hash_and_never_exceeds_two_sends(tmp_path: Path) -> None:
    db, writer = _env(tmp_path)
    try:
        adapter = FakeAdapter(
            [
                WriteTransportError("connection dropped", may_have_been_sent=True),
                WriteResponse(accepted=True, http_status=200, api_code="0", request_id="req-2"),
            ]
        )
        service = WriteAheadExecutor(db, writer, adapter, clock=FixedClock())
        first = service.submit("execution-1", _request())
        assert first.execution_status == UNKNOWN_REQUIRES_REVIEW

        with pytest.raises(WriteGateBlocked):
            service.submit("execution-1", _request(), compensation=True)

        reader = FakeReader(
            [
                ReconcileObservation(
                    outcome=RECON_PROVEN_NOT_EXECUTED,
                    evidence={"proof": "official GET found no matching task"},
                    request_ids=("get-1",),
                )
            ]
        )
        reconciler = ExecutionReconciler(db, writer, reader, clock=FixedClock())
        proof = reconciler.reconcile("execution-1")
        assert proof.execution_status == APPROVED
        assert proof.reconciliation_status == RECON_PROVEN_NOT_EXECUTED

        with pytest.raises(WriteGateBlocked, match="HASH_MISMATCH"):
            service.submit("execution-1", _request(budget="99"), compensation=True)
        assert len(adapter.calls) == 1

        second = service.submit("execution-1", _request(), compensation=True)
        assert second.execution_status == SUBMITTED
        assert second.conservative_send_count == 2
        assert len(adapter.calls) == 2

        # 即使以后有人错误地把状态改回 APPROVED，也不能突破两次发送硬上限。
        writer.execute(
            "UPDATE execution_task SET status='APPROVED' WHERE execution_id='execution-1'"
        ).result(timeout=5)
        with pytest.raises(WriteGateBlocked, match="SEND_LIMIT"):
            service.submit("execution-1", _request(), compensation=True)
        assert len(adapter.calls) == 2
    finally:
        writer.close()


def test_reconciliation_success_confirms_without_second_post(tmp_path: Path) -> None:
    db, writer = _env(tmp_path)
    try:
        adapter = FakeAdapter(
            [WriteResponse(accepted=True, http_status=200, api_code="0", request_id="post-1")]
        )
        service = WriteAheadExecutor(db, writer, adapter, clock=FixedClock())
        submitted = service.submit("execution-1", _request())
        assert submitted.execution_status == SUBMITTED

        reader = FakeReader(
            [
                ReconcileObservation(
                    outcome=CONFIRMED_SUCCESS,
                    evidence={"task_status": "PROCESSING"},
                    request_ids=("get-1",),
                    external_object_id="700001",
                )
            ]
        )
        reconciler = ExecutionReconciler(db, writer, reader, clock=FixedClock())
        result = reconciler.reconcile("execution-1")
        assert result.execution_status == CONFIRMED_SUCCESS
        assert result.conservative_send_count == 1
        assert len(adapter.calls) == 1
        with db.connect(readonly=True) as conn:
            execution = conn.execute(
                "SELECT status,external_object_id FROM execution_task WHERE execution_id='execution-1'"
            ).fetchone()
            reconciliation = conn.execute(
                "SELECT status FROM execution_reconciliation WHERE execution_id='execution-1'"
            ).fetchone()
        assert execution["status"] == CONFIRMED_SUCCESS
        assert execution["external_object_id"] == "700001"
        assert reconciliation["status"] == "RESOLVED_SUCCESS"
    finally:
        writer.close()


def test_known_rejection_is_terminal_and_does_not_create_reconciliation(tmp_path: Path) -> None:
    db, writer = _env(tmp_path)
    try:
        adapter = FakeAdapter(
            [
                WriteResponse(
                    accepted=False,
                    http_status=200,
                    api_code="40001",
                    request_id="post-reject",
                    message="invalid params",
                )
            ]
        )
        service = WriteAheadExecutor(db, writer, adapter, clock=FixedClock())
        result = service.submit("execution-1", _request())
        assert result.execution_status == CONFIRMED_FAILED
        assert result.attempt_outcome == ATTEMPT_REJECTED
        with db.connect(readonly=True) as conn:
            assert conn.execute(
                "SELECT status FROM execution_task WHERE execution_id='execution-1'"
            ).fetchone()[0] == CONFIRMED_FAILED
            assert conn.execute(
                "SELECT COUNT(*) FROM execution_reconciliation"
            ).fetchone()[0] == 0
    finally:
        writer.close()


def test_ambiguous_response_requires_reconciliation_and_no_blind_retry(tmp_path: Path) -> None:
    db, writer = _env(tmp_path)
    try:
        adapter = FakeAdapter(
            [WriteResponse(accepted=None, http_status=200, api_code="0", request_id="post-ambiguous")]
        )
        service = WriteAheadExecutor(db, writer, adapter, clock=FixedClock())
        result = service.submit("execution-1", _request())
        assert result.execution_status == UNKNOWN_REQUIRES_REVIEW
        assert result.attempt_outcome == ATTEMPT_UNKNOWN
        with pytest.raises(WriteGateBlocked):
            service.submit("execution-1", _request())
        assert len(adapter.calls) == 1
    finally:
        writer.close()
