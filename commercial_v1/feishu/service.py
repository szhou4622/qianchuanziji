"""Phase 5 飞书确认持久化域。

本模块定义本地候选确认卡片的冻结 payload、持久 Outbox/Inbox 与确认事件处理。
它不依赖具体飞书 SDK，也不主动联网；真正的长连接和消息发送适配器可在其上层接入。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from commercial_v1.candidate import EXPIRED, WAITING_CONFIRMATION, CandidateService
from commercial_v1.security.redaction import redact, sanitize_text
from commercial_v1.storage.database import Database
from commercial_v1.storage.writer import StorageWriter

Clock = Callable[[], datetime]

CANDIDATE_CONFIRM = "CANDIDATE_CONFIRM"

OUTBOX_QUEUED = "QUEUED"
OUTBOX_SENDING = "SENDING"
OUTBOX_SENT = "SENT"
OUTBOX_RETRY = "RETRY"
OUTBOX_FAILED = "FAILED"

INBOX_RECEIVED = "RECEIVED"
INBOX_PROCESSED = "PROCESSED"
INBOX_FAILED = "FAILED"

ACTION_APPROVE = "APPROVE"
ACTION_REJECT = "REJECT"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


def _stable_id(prefix: str, *parts: str) -> str:
    return prefix + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CandidateCardEnvelope:
    outbox_id: str
    notification_id: str
    candidate_id: str
    route_id: str
    payload: Mapping[str, Any]
    created: bool


@dataclass(frozen=True)
class ClaimedOutbox:
    outbox_id: str
    notification_type: str
    route_id: str | None
    related_candidate_id: str | None
    related_execution_id: str | None
    payload: Mapping[str, Any]
    attempt_count: int
    claim_owner: str
    claim_expires_at: str


@dataclass(frozen=True)
class InboxActionResult:
    event_id: str
    candidate_id: str
    action: str
    candidate_status: str
    changed: bool
    duplicate_event: bool


class FeishuStateError(RuntimeError):
    pass


class FeishuCandidateCardService:
    """把 WAITING_CONFIRMATION 候选冻结成持久 Outbox 消息。"""

    def __init__(
        self,
        database: Database,
        writer: StorageWriter,
        *,
        clock: Clock = utc_now,
    ) -> None:
        self._database = database
        self._writer = writer
        self._clock = clock

    def queue_candidate(self, candidate_id: str, route_id: str) -> CandidateCardEnvelope:
        candidate = str(candidate_id or "").strip()
        route = str(route_id or "").strip()
        if not candidate or not route:
            raise ValueError("candidate_id and route_id are required")
        now = _iso(self._clock())

        with self._database.connect(readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM candidate_batch WHERE candidate_id=?",
                (candidate,),
            ).fetchone()
            if row is None:
                raise FeishuStateError("candidate does not exist")
            if str(row["execution_mode"] or "") != "MANUAL":
                raise FeishuStateError("only MANUAL candidate can create a confirmation card")
            if str(row["status"] or "") != WAITING_CONFIRMATION:
                raise FeishuStateError("candidate is not waiting for confirmation")
            expires_at = str(row["expires_at"] or "")
            if not expires_at or expires_at <= now:
                raise FeishuStateError("candidate confirmation has expired")
            items = conn.execute(
                """SELECT candidate_item_id,hit_id,object_uid,material_id,control_task_id,
                          metric_snapshot_json,before_state_json
                   FROM candidate_item WHERE candidate_id=? ORDER BY object_uid""",
                (candidate,),
            ).fetchall()

        try:
            execution_params = json.loads(str(row["execution_params_json"] or "{}"))
        except Exception as exc:
            raise FeishuStateError("candidate execution params are invalid") from exc

        frozen_items = []
        for item in items:
            try:
                metrics = json.loads(str(item["metric_snapshot_json"] or "{}"))
                before = json.loads(str(item["before_state_json"] or "{}"))
            except Exception as exc:
                raise FeishuStateError("candidate item snapshot is invalid") from exc
            frozen_items.append(
                {
                    "candidate_item_id": item["candidate_item_id"],
                    "hit_id": item["hit_id"],
                    "object_uid": item["object_uid"],
                    "material_id": item["material_id"],
                    "control_task_id": item["control_task_id"],
                    "metric_snapshot": metrics,
                    "before_state": before,
                }
            )

        payload = {
            "contract_version": 1,
            "notification_type": CANDIDATE_CONFIRM,
            "candidate_id": candidate,
            "strategy_id": row["strategy_id"],
            "strategy_version_id": row["strategy_version_id"],
            "action_type": row["action_type"],
            "advertiser_id": row["advertiser_id"],
            "ad_id": row["ad_id"],
            "execution_mode": row["execution_mode"],
            "grouping_mode": row["grouping_mode"],
            "execution_params": execution_params,
            "items": frozen_items,
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "actions": [ACTION_APPROVE, ACTION_REJECT],
        }
        outbox_id = _stable_id("feishu_outbox_", CANDIDATE_CONFIRM, candidate, route)
        notification_id = _stable_id("notification_", "FEISHU", CANDIDATE_CONFIRM, candidate, route)
        payload_json = _json(payload)

        def work(conn):
            cursor = conn.execute(
                """INSERT OR IGNORE INTO feishu_outbox(
                   outbox_id,notification_type,route_id,related_candidate_id,payload_json,status,
                   attempt_count,next_attempt_at,created_at
                   ) VALUES(?,?,?,?,?,'QUEUED',0,?,?)""",
                (outbox_id, CANDIDATE_CONFIRM, route, candidate, payload_json, now, now),
            )
            created = cursor.rowcount == 1
            conn.execute(
                """INSERT OR IGNORE INTO notification_event(
                   notification_id,channel,notification_type,candidate_id,delivery_status,created_at
                   ) VALUES(?,'FEISHU',?,?, 'QUEUED',?)""",
                (notification_id, CANDIDATE_CONFIRM, candidate, now),
            )
            return bool(created)

        created = bool(self._writer.transaction(work).result(timeout=5))
        return CandidateCardEnvelope(
            outbox_id=outbox_id,
            notification_id=notification_id,
            candidate_id=candidate,
            route_id=route,
            payload=payload,
            created=created,
        )


class FeishuOutboxStore:
    """带 claim/lease 的持久 Outbox；网络发送必须发生在 claim 之后、数据库事务之外。"""

    def __init__(
        self,
        database: Database,
        writer: StorageWriter,
        *,
        clock: Clock = utc_now,
        max_attempts: int = 8,
    ) -> None:
        self._database = database
        self._writer = writer
        self._clock = clock
        self._max_attempts = max(1, int(max_attempts))

    def claim_next(self, owner: str, *, lease_seconds: int = 45) -> ClaimedOutbox | None:
        owner_id = str(owner or "").strip()
        if not owner_id:
            raise ValueError("claim owner is required")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now_dt = self._clock().astimezone(timezone.utc)
        now = _iso(now_dt)
        expires = _iso(now_dt + timedelta(seconds=lease_seconds))

        def work(conn):
            row = conn.execute(
                """SELECT * FROM feishu_outbox
                   WHERE status IN('QUEUED','RETRY')
                     AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                     AND (claim_expires_at IS NULL OR claim_expires_at<=?)
                   ORDER BY created_at ASC,outbox_id ASC LIMIT 1""",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            attempt = int(row["attempt_count"] or 0) + 1
            changed = conn.execute(
                """UPDATE feishu_outbox SET status='SENDING',attempt_count=?,claim_owner=?,
                   claim_expires_at=?,last_error_message=NULL
                   WHERE outbox_id=? AND status IN('QUEUED','RETRY')
                     AND (claim_expires_at IS NULL OR claim_expires_at<=?)""",
                (attempt, owner_id, expires, row["outbox_id"], now),
            ).rowcount
            if changed != 1:
                return None
            try:
                payload = json.loads(str(row["payload_json"]))
            except Exception as exc:
                raise FeishuStateError("outbox payload is invalid") from exc
            return ClaimedOutbox(
                outbox_id=str(row["outbox_id"]),
                notification_type=str(row["notification_type"]),
                route_id=str(row["route_id"]) if row["route_id"] else None,
                related_candidate_id=str(row["related_candidate_id"]) if row["related_candidate_id"] else None,
                related_execution_id=str(row["related_execution_id"]) if row["related_execution_id"] else None,
                payload=payload,
                attempt_count=attempt,
                claim_owner=owner_id,
                claim_expires_at=expires,
            )

        return self._writer.transaction(work).result(timeout=5)

    def mark_sent(self, claimed: ClaimedOutbox) -> None:
        now = _iso(self._clock())

        def work(conn):
            changed = conn.execute(
                """UPDATE feishu_outbox SET status='SENT',sent_at=?,claim_owner=NULL,claim_expires_at=NULL,
                   next_attempt_at=NULL,last_error_message=NULL
                   WHERE outbox_id=? AND status='SENDING' AND claim_owner=?""",
                (now, claimed.outbox_id, claimed.claim_owner),
            ).rowcount
            if changed != 1:
                raise FeishuStateError("outbox claim is stale")
            if claimed.related_candidate_id:
                conn.execute(
                    """UPDATE notification_event SET delivery_status='SENT',delivered_at=?
                       WHERE channel='FEISHU' AND notification_type=? AND candidate_id=?""",
                    (now, claimed.notification_type, claimed.related_candidate_id),
                )

        self._writer.transaction(work).result(timeout=5)

    def mark_failed(self, claimed: ClaimedOutbox, error_message: str, *, retryable: bool = True) -> str:
        now_dt = self._clock().astimezone(timezone.utc)
        message = sanitize_text(str(error_message or "send failed"))[:1000]
        retry = retryable and claimed.attempt_count < self._max_attempts
        status = OUTBOX_RETRY if retry else OUTBOX_FAILED
        delay_seconds = min(300, 2 ** min(claimed.attempt_count, 8)) if retry else 0
        next_attempt = _iso(now_dt + timedelta(seconds=delay_seconds)) if retry else None

        def work(conn):
            changed = conn.execute(
                """UPDATE feishu_outbox SET status=?,next_attempt_at=?,claim_owner=NULL,claim_expires_at=NULL,
                   last_error_message=?
                   WHERE outbox_id=? AND status='SENDING' AND claim_owner=?""",
                (status, next_attempt, message, claimed.outbox_id, claimed.claim_owner),
            ).rowcount
            if changed != 1:
                raise FeishuStateError("outbox claim is stale")
            if not retry and claimed.related_candidate_id:
                conn.execute(
                    """UPDATE notification_event SET delivery_status='FAILED',error_message=?
                       WHERE channel='FEISHU' AND notification_type=? AND candidate_id=?""",
                    (message, claimed.notification_type, claimed.related_candidate_id),
                )

        self._writer.transaction(work).result(timeout=5)
        return status

    def recover_expired_claims(self) -> int:
        now = _iso(self._clock())
        result = self._writer.execute(
            """UPDATE feishu_outbox SET status='RETRY',claim_owner=NULL,claim_expires_at=NULL,
               next_attempt_at=COALESCE(next_attempt_at,?)
               WHERE status='SENDING' AND claim_expires_at IS NOT NULL AND claim_expires_at<=?""",
            (now, now),
        ).result(timeout=5)
        return int(result.rowcount)


class FeishuInboxService:
    """消费长连接回调事件，并把 APPROVE/REJECT 映射到本地冻结候选。"""

    def __init__(
        self,
        database: Database,
        writer: StorageWriter,
        candidates: CandidateService,
        *,
        clock: Clock = utc_now,
    ) -> None:
        self._database = database
        self._writer = writer
        self._candidates = candidates
        self._clock = clock

    def receive_candidate_action(
        self,
        event_id: str,
        *,
        candidate_id: str,
        action: str,
        payload: Mapping[str, Any] | None = None,
    ) -> InboxActionResult:
        event = str(event_id or "").strip()
        candidate = str(candidate_id or "").strip()
        normalized_action = str(action or "").strip().upper()
        if not event or not candidate:
            raise ValueError("event_id and candidate_id are required")
        if normalized_action not in {ACTION_APPROVE, ACTION_REJECT}:
            raise ValueError("unsupported candidate action")
        now = _iso(self._clock())
        inbox_id = _stable_id("feishu_inbox_", event)
        redacted_payload = redact(dict(payload or {}))

        def insert_event(conn):
            cursor = conn.execute(
                """INSERT OR IGNORE INTO feishu_inbox(
                   inbox_id,event_id,event_type,received_at,payload_redacted_json,status
                   ) VALUES(?,?, 'CANDIDATE_ACTION',?,?, 'RECEIVED')""",
                (inbox_id, event, now, _json(redacted_payload)),
            )
            if cursor.rowcount == 1:
                return True
            existing = conn.execute(
                "SELECT status FROM feishu_inbox WHERE event_id=?",
                (event,),
            ).fetchone()
            if existing is None:
                raise FeishuStateError("duplicate event lookup failed")
            return False

        created = bool(self._writer.transaction(insert_event).result(timeout=5))
        if not created:
            with self._database.connect(readonly=True) as conn:
                row = conn.execute("SELECT * FROM candidate_batch WHERE candidate_id=?", (candidate,)).fetchone()
            if row is None:
                raise FeishuStateError("candidate does not exist")
            return InboxActionResult(
                event_id=event,
                candidate_id=candidate,
                action=normalized_action,
                candidate_status=str(row["status"]),
                changed=False,
                duplicate_event=True,
            )

        try:
            decision = (
                self._candidates.approve(candidate)
                if normalized_action == ACTION_APPROVE
                else self._candidates.reject(candidate)
            )
        except Exception as exc:
            message = sanitize_text(f"{type(exc).__name__}: {exc}")[:1000]
            self._writer.execute(
                "UPDATE feishu_inbox SET status='FAILED',processed_at=?,error_message=? WHERE event_id=?",
                (now, message, event),
            ).result(timeout=5)
            raise

        def finish(conn):
            conn.execute(
                "UPDATE feishu_inbox SET status='PROCESSED',processed_at=?,error_message=NULL WHERE event_id=?",
                (now, event),
            )
            if decision.status == EXPIRED:
                conn.execute(
                    """UPDATE notification_event SET delivery_status='EXPIRED',expired_at=?
                       WHERE channel='FEISHU' AND notification_type='CANDIDATE_CONFIRM' AND candidate_id=?""",
                    (now, candidate),
                )
            else:
                conn.execute(
                    """UPDATE notification_event SET clicked_at=?
                       WHERE channel='FEISHU' AND notification_type='CANDIDATE_CONFIRM' AND candidate_id=?""",
                    (now, candidate),
                )

        self._writer.transaction(finish).result(timeout=5)
        return InboxActionResult(
            event_id=event,
            candidate_id=candidate,
            action=normalized_action,
            candidate_status=decision.status,
            changed=decision.changed,
            duplicate_event=False,
        )
