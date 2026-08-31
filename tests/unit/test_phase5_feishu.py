from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from commercial_v1.candidate import APPROVED, EXPIRED, WAITING_CONFIRMATION, CandidateService
from commercial_v1.feishu import (
    ACTION_APPROVE,
    OUTBOX_RETRY,
    OUTBOX_SENT,
    FeishuCandidateCardService,
    FeishuInboxService,
    FeishuOutboxStore,
)
from commercial_v1.storage.database import Database, DatabaseConfig
from commercial_v1.storage.schema import create_schema_v1
from commercial_v1.storage.writer import StorageWriter

NOW_DT = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)
NOW = NOW_DT.isoformat(timespec="seconds")


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs) -> None:
        self.value = self.value + timedelta(**kwargs)


def _env(tmp_path: Path):
    db = Database(DatabaseConfig(tmp_path / "runtime.db"))
    with db.connect() as conn:
        create_schema_v1(conn)
    writer = StorageWriter(db)
    writer.start()
    clock = MutableClock(NOW_DT)
    candidates = CandidateService(db, writer, clock=clock)
    cards = FeishuCandidateCardService(db, writer, clock=clock)
    outbox = FeishuOutboxStore(db, writer, clock=clock, max_attempts=3)
    inbox = FeishuInboxService(db, writer, candidates, clock=clock)
    return db, writer, clock, candidates, cards, outbox, inbox


def _candidate(writer: StorageWriter, candidate_id: str, *, material_id: str = "900001") -> None:
    writer.execute(
        """INSERT INTO candidate_batch(
           candidate_id,action_type,advertiser_id,ad_id,execution_mode,grouping_mode,
           execution_params_json,group_fingerprint,status,created_at,expires_at
           ) VALUES(?,'CREATE_RETARGET','111111','222222','MANUAL','SEPARATE',
                    '{"budget":"500","duration":"2"}',?, 'WAITING_CONFIRMATION',?,?)""",
        (candidate_id, f"fingerprint-{candidate_id}", NOW, "2026-08-31T12:30:00+00:00"),
    ).result(timeout=5)
    writer.execute(
        """INSERT INTO candidate_item(
           candidate_item_id,candidate_id,object_uid,material_id,metric_snapshot_json,
           before_state_json,created_at
           ) VALUES(?,?,?,?,?,?,?)""",
        (
            f"item-{candidate_id}",
            candidate_id,
            f"material:111111:222222:{material_id}",
            material_id,
            '{"overall_cost_decimal":"120","overall_pay_roi_decimal":"2"}',
            '{"source_batch_id":"batch-1","source_collected_at":"2026-08-31T12:00:00+00:00"}',
            NOW,
        ),
    ).result(timeout=5)


def test_candidate_card_freezes_snapshot_and_queue_is_idempotent(tmp_path: Path) -> None:
    db, writer, _, _, cards, _, _ = _env(tmp_path)
    try:
        _candidate(writer, "candidate-1")
        first = cards.queue_candidate("candidate-1", "route-main")
        second = cards.queue_candidate("candidate-1", "route-main")
        assert first.created is True
        assert second.created is False
        assert first.outbox_id == second.outbox_id
        assert first.payload["candidate_id"] == "candidate-1"
        assert first.payload["execution_params"] == {"budget": "500", "duration": "2"}
        assert first.payload["items"][0]["material_id"] == "900001"
        assert first.payload["items"][0]["metric_snapshot"]["overall_cost_decimal"] == "120"
        assert first.payload["actions"] == ["APPROVE", "REJECT"]
        assert first.payload["expires_at"] == "2026-08-31T12:30:00+00:00"

        with db.connect(readonly=True) as conn:
            assert conn.execute("SELECT COUNT(*) FROM feishu_outbox").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM notification_event").fetchone()[0] == 1
            stored = json.loads(conn.execute("SELECT payload_json FROM feishu_outbox").fetchone()[0])
            assert stored == first.payload
    finally:
        writer.close()


def test_outbox_claim_retry_and_sent_use_persistent_attempt_count(tmp_path: Path) -> None:
    db, writer, clock, _, cards, outbox, _ = _env(tmp_path)
    try:
        _candidate(writer, "candidate-2")
        card = cards.queue_candidate("candidate-2", "route-main")
        claimed = outbox.claim_next("sender-1")
        assert claimed is not None
        assert claimed.outbox_id == card.outbox_id
        assert claimed.attempt_count == 1
        assert outbox.mark_failed(claimed, "temporary network error", retryable=True) == OUTBOX_RETRY
        assert outbox.claim_next("sender-1") is None

        clock.advance(seconds=3)
        second = outbox.claim_next("sender-2")
        assert second is not None
        assert second.attempt_count == 2
        outbox.mark_sent(second)

        with db.connect(readonly=True) as conn:
            row = conn.execute(
                "SELECT status,attempt_count,sent_at,claim_owner FROM feishu_outbox WHERE outbox_id=?",
                (card.outbox_id,),
            ).fetchone()
            assert row["status"] == OUTBOX_SENT
            assert row["attempt_count"] == 2
            assert row["sent_at"] is not None
            assert row["claim_owner"] is None
            notification = conn.execute(
                "SELECT delivery_status,delivered_at FROM notification_event WHERE candidate_id='candidate-2'"
            ).fetchone()
            assert notification["delivery_status"] == "SENT"
            assert notification["delivered_at"] is not None
    finally:
        writer.close()


def test_inbox_approve_is_event_idempotent_and_redacts_payload(tmp_path: Path) -> None:
    db, writer, _, _, cards, _, inbox = _env(tmp_path)
    try:
        _candidate(writer, "candidate-3")
        cards.queue_candidate("candidate-3", "route-main")
        result = inbox.receive_candidate_action(
            "event-1",
            candidate_id="candidate-3",
            action=ACTION_APPROVE,
            payload={"candidate_id": "candidate-3", "access_token": "super-secret-token"},
        )
        assert result.candidate_status == APPROVED
        assert result.changed is True
        assert result.duplicate_event is False

        duplicate = inbox.receive_candidate_action(
            "event-1",
            candidate_id="candidate-3",
            action=ACTION_APPROVE,
            payload={"candidate_id": "candidate-3"},
        )
        assert duplicate.candidate_status == APPROVED
        assert duplicate.changed is False
        assert duplicate.duplicate_event is True

        with db.connect(readonly=True) as conn:
            assert conn.execute("SELECT COUNT(*) FROM feishu_inbox").fetchone()[0] == 1
            stored = conn.execute(
                "SELECT status,payload_redacted_json FROM feishu_inbox WHERE event_id='event-1'"
            ).fetchone()
            assert stored["status"] == "PROCESSED"
            assert "super-secret-token" not in stored["payload_redacted_json"]
            notification = conn.execute(
                "SELECT clicked_at FROM notification_event WHERE candidate_id='candidate-3'"
            ).fetchone()
            assert notification["clicked_at"] is not None
    finally:
        writer.close()


def test_expired_confirmation_click_never_approves_candidate(tmp_path: Path) -> None:
    db, writer, clock, _, cards, _, inbox = _env(tmp_path)
    try:
        _candidate(writer, "candidate-4")
        cards.queue_candidate("candidate-4", "route-main")
        clock.advance(minutes=31)
        result = inbox.receive_candidate_action(
            "event-expired",
            candidate_id="candidate-4",
            action=ACTION_APPROVE,
            payload={"candidate_id": "candidate-4"},
        )
        assert result.candidate_status == EXPIRED
        assert result.changed is True
        with db.connect(readonly=True) as conn:
            candidate = conn.execute(
                "SELECT status,approved_at FROM candidate_batch WHERE candidate_id='candidate-4'"
            ).fetchone()
            assert candidate["status"] == EXPIRED
            assert candidate["approved_at"] is None
            notification = conn.execute(
                "SELECT delivery_status,expired_at FROM notification_event WHERE candidate_id='candidate-4'"
            ).fetchone()
            assert notification["delivery_status"] == "EXPIRED"
            assert notification["expired_at"] is not None
    finally:
        writer.close()


def test_expired_outbox_claim_can_be_recovered_without_duplicate_row(tmp_path: Path) -> None:
    db, writer, clock, _, cards, outbox, _ = _env(tmp_path)
    try:
        _candidate(writer, "candidate-5")
        card = cards.queue_candidate("candidate-5", "route-main")
        claimed = outbox.claim_next("dead-sender", lease_seconds=5)
        assert claimed is not None
        clock.advance(seconds=6)
        assert outbox.recover_expired_claims() == 1
        recovered = outbox.claim_next("new-sender")
        assert recovered is not None
        assert recovered.outbox_id == card.outbox_id
        assert recovered.attempt_count == 2
        with db.connect(readonly=True) as conn:
            assert conn.execute("SELECT COUNT(*) FROM feishu_outbox").fetchone()[0] == 1
    finally:
        writer.close()
