from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from commercial_v1.candidate.jobs import CandidateBuildHandler
from commercial_v1.candidate.service import CandidateService
from commercial_v1.feishu import CandidateFeishuNotifier, FeishuCandidateCardService
from commercial_v1.runtime.jobs import ClaimedJob
from commercial_v1.storage.database import Database, DatabaseConfig
from commercial_v1.storage.schema import create_schema_v1
from commercial_v1.storage.writer import StorageWriter


NOW = "2026-08-31T13:00:00+00:00"
FUTURE = "2099-01-01T00:00:00+00:00"


def _env(tmp_path: Path):
    db = Database(DatabaseConfig(tmp_path / "runtime.db"))
    with db.connect() as conn:
        create_schema_v1(conn)
    writer = StorageWriter(db)
    writer.start()
    candidates = CandidateService(db, writer)
    cards = FeishuCandidateCardService(db, writer)
    return db, writer, candidates, cards


def _candidate(writer: StorageWriter, candidate_id: str, *, mode: str, status: str, ad_id: str = "222222") -> None:
    writer.execute(
        """INSERT INTO candidate_batch(
           candidate_id,action_type,advertiser_id,ad_id,execution_mode,grouping_mode,
           execution_params_json,group_fingerprint,status,created_at,expires_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            candidate_id,
            "CREATE_RETARGET",
            "111111",
            ad_id,
            mode,
            "SEPARATE",
            '{"budget":"88.8"}',
            f"fingerprint-{candidate_id}",
            status,
            NOW,
            FUTURE if status == "WAITING_CONFIRMATION" else None,
        ),
    ).result(timeout=5)


def test_manual_waiting_candidate_is_queued_idempotently(tmp_path: Path) -> None:
    db, writer, candidates, cards = _env(tmp_path)
    try:
        _candidate(writer, "candidate-manual", mode="MANUAL", status="WAITING_CONFIRMATION")
        notifier = CandidateFeishuNotifier(
            candidates,
            cards,
            route_resolver=lambda candidate: f"route-{candidate['advertiser_id']}",
        )
        first = notifier.notify_candidates(["candidate-manual"])
        second = notifier.notify_candidates(["candidate-manual"])

        assert first.queued == 1
        assert first.existing == 0
        assert second.queued == 0
        assert second.existing == 1
        assert first.outbox_ids == second.outbox_ids
        with db.connect(readonly=True) as conn:
            row = conn.execute("SELECT * FROM feishu_outbox").fetchone()
            assert row is not None
            assert row["route_id"] == "route-111111"
            assert row["related_candidate_id"] == "candidate-manual"
            assert row["status"] == "QUEUED"
    finally:
        writer.close()


def test_auto_and_non_waiting_candidates_never_create_confirmation_card(tmp_path: Path) -> None:
    db, writer, candidates, cards = _env(tmp_path)
    try:
        _candidate(writer, "candidate-auto", mode="AUTO", status="APPROVED")
        _candidate(writer, "candidate-rejected", mode="MANUAL", status="REJECTED")
        notifier = CandidateFeishuNotifier(candidates, cards, route_resolver=lambda _candidate: "route-main")
        result = notifier.notify_candidates(["candidate-auto", "candidate-rejected"])
        assert result.skipped_auto == 1
        assert result.skipped_status == 1
        assert result.queued == 0
        with db.connect(readonly=True) as conn:
            assert conn.execute("SELECT COUNT(*) FROM feishu_outbox").fetchone()[0] == 0
    finally:
        writer.close()


def test_missing_route_blocks_only_notification_capability(tmp_path: Path) -> None:
    db, writer, candidates, cards = _env(tmp_path)
    try:
        _candidate(writer, "candidate-no-route", mode="MANUAL", status="WAITING_CONFIRMATION")
        notifier = CandidateFeishuNotifier(candidates, cards, route_resolver=lambda _candidate: None)
        result = notifier.notify_candidates(["candidate-no-route"])
        assert result.skipped_no_route == 1
        assert candidates.get("candidate-no-route")["status"] == "WAITING_CONFIRMATION"  # type: ignore[index]
        with db.connect(readonly=True) as conn:
            assert conn.execute("SELECT COUNT(*) FROM feishu_outbox").fetchone()[0] == 0
    finally:
        writer.close()


def test_candidate_build_handler_calls_local_notification_after_persistence() -> None:
    calls: list[tuple[str, ...]] = []

    class FakeCandidateService:
        def build_from_source_batch(self, target_uid: str, source_batch_id: str):
            assert target_uid == "target-1"
            assert source_batch_id == "batch-1"
            return SimpleNamespace(
                target_uid=target_uid,
                source_batch_id=source_batch_id,
                eligible_hits=2,
                built_candidates=1,
                existing_candidates=1,
                skipped_active_guard=0,
                skipped_reject_cooldown=0,
                candidate_ids=("candidate-1", "candidate-2"),
            )

    def notify(candidate_ids):
        frozen = tuple(candidate_ids)
        calls.append(frozen)
        return {"queued": 1, "existing": 1}

    handler = CandidateBuildHandler(
        FakeCandidateService(),  # type: ignore[arg-type]
        on_candidates_ready=notify,
    )
    job = ClaimedJob(
        job_uid="job-1",
        job_type="CANDIDATE_BUILD",
        priority=70,
        payload={"target_uid": "target-1", "source_batch_id": "batch-1"},
        due_at=NOW,
        owner_instance_id="candidate-worker",
        fencing_token=1,
        lease_expires_at=FUTURE,
    )
    result = handler(job)
    assert calls == [("candidate-1", "candidate-2")]
    assert result["notification"] == {"queued": 1, "existing": 1}


def test_license_blocked_candidate_job_never_calls_notification() -> None:
    called = False

    class FakeCandidateService:
        def build_from_source_batch(self, _target_uid: str, _source_batch_id: str):
            raise AssertionError("candidate build must not run while license blocked")

    def notify(_candidate_ids):
        nonlocal called
        called = True

    handler = CandidateBuildHandler(
        FakeCandidateService(),  # type: ignore[arg-type]
        business_allowed=lambda: False,
        on_candidates_ready=notify,
    )
    job = ClaimedJob(
        job_uid="job-1",
        job_type="CANDIDATE_BUILD",
        priority=70,
        payload={"target_uid": "target-1", "source_batch_id": "batch-1"},
        due_at=NOW,
        owner_instance_id="candidate-worker",
        fencing_token=1,
        lease_expires_at=FUTURE,
    )
    result = handler(job)
    assert result["skipped"] == "LICENSE_BLOCKED"
    assert called is False
