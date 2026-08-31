"""Phase 5 候选冻结与本地确认状态机。

候选层只消费 Phase 4 已持久化、未被优先级压制的 HIT。它不会访问千川网络，也不会
执行任何平台 POST。候选一旦创建即冻结策略版本、指标快照、执行参数与对象集合。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from commercial_v1.storage.database import Database
from commercial_v1.storage.writer import StorageWriter

Clock = Callable[[], datetime]

WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
EXPIRED = "EXPIRED"
CANCELLED = "CANCELLED"

CREATE_RETARGET = "CREATE_RETARGET"
PAUSE_CONTROL = "PAUSE_CONTROL"
UPDATE_BUDGET = "UPDATE_BUDGET"
UPDATE_DURATION = "UPDATE_DURATION"

MANUAL = "MANUAL"
AUTO = "AUTO"
SEPARATE = "SEPARATE"
MERGED = "MERGED"

MAX_RETARGET_MATERIALS = 20
DEFAULT_CONFIRM_TTL_MINUTES = 30
DEFAULT_REJECT_COOLDOWN_MINUTES = 30


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


def _hash(prefix: str, *parts: str) -> str:
    raw = "|".join(parts).encode("utf-8")
    return prefix + hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class CandidateBuildSummary:
    target_uid: str
    source_batch_id: str
    eligible_hits: int
    built_candidates: int
    existing_candidates: int
    skipped_active_guard: int
    skipped_reject_cooldown: int
    candidate_ids: tuple[str, ...]


@dataclass(frozen=True)
class CandidateDecision:
    candidate_id: str
    status: str
    changed: bool
    expires_at: str | None
    reject_cooldown_until: str | None


class CandidateStateError(RuntimeError):
    pass


class CandidateService:
    def __init__(
        self,
        database: Database,
        writer: StorageWriter,
        *,
        clock: Clock = utc_now,
        confirm_ttl_minutes: int = DEFAULT_CONFIRM_TTL_MINUTES,
        reject_cooldown_minutes: int = DEFAULT_REJECT_COOLDOWN_MINUTES,
    ) -> None:
        self._database = database
        self._writer = writer
        self._clock = clock
        self._confirm_ttl = max(1, int(confirm_ttl_minutes))
        self._reject_cooldown = max(1, int(reject_cooldown_minutes))

    def build_from_source_batch(self, target_uid: str, source_batch_id: str) -> CandidateBuildSummary:
        target = str(target_uid or "").strip()
        batch = str(source_batch_id or "").strip()
        if not target or not batch:
            raise ValueError("target_uid and source_batch_id are required")
        now_dt = self._clock().astimezone(timezone.utc)
        now = _iso(now_dt)
        expires = _iso(now_dt + timedelta(minutes=self._confirm_ttl))

        with self._database.connect(readonly=True) as conn:
            batch_row = conn.execute(
                """SELECT batch_id,target_uid,status,pipeline_type FROM collection_batch
                   WHERE batch_id=? AND target_uid=? AND status='SUCCESS'
                     AND pipeline_type IN('MATERIAL_5M','CONTROL_5M')""",
                (batch, target),
            ).fetchone()
            if batch_row is None:
                raise ValueError("candidate source must be a trusted successful hot batch")
            rows = conn.execute(
                """SELECT h.*,c.action_type AS action_type,c.execution_mode,c.enabled,
                          v.action_config_json,v.grouping_mode,v.content_hash
                   FROM strategy_hit h
                   JOIN strategy_config c ON c.strategy_id=h.strategy_id
                   JOIN strategy_version v ON v.strategy_version_id=h.strategy_version_id
                   WHERE h.target_uid=? AND h.source_batch_id=? AND h.result='HIT'
                     AND h.suppression_reason IS NULL AND c.enabled=1
                   ORDER BY h.strategy_version_id,h.object_uid,h.hit_id""",
                (target, batch),
            ).fetchall()

        grouped: dict[tuple[str, str, str, str], list[Mapping[str, Any]]] = {}
        for row in rows:
            key = (
                str(row["strategy_version_id"]),
                str(row["action_type"]),
                str(row["advertiser_id"]),
                str(row["ad_id"]),
            )
            grouped.setdefault(key, []).append(row)

        built = 0
        existing = 0
        skipped_guard = 0
        skipped_cooldown = 0
        candidate_ids: list[str] = []

        for (_version_id, action_type, advertiser_id, ad_id), group_rows in grouped.items():
            first = group_rows[0]
            execution_mode = str(first["execution_mode"] or "").upper()
            grouping_mode = str(first["grouping_mode"] or SEPARATE).upper()
            action_config_json = str(first["action_config_json"] or "{}")
            try:
                action_config = json.loads(action_config_json)
            except Exception as exc:
                raise CandidateStateError("stored strategy action_config_json is invalid") from exc

            eligible: list[Mapping[str, Any]] = []
            for row in group_rows:
                object_uid = str(row["object_uid"])
                if self._reject_cooldown_active(str(row["strategy_id"]), object_uid, now):
                    skipped_cooldown += 1
                    continue
                if action_type == CREATE_RETARGET:
                    material_id = str(row["material_id"] or "")
                    if not material_id:
                        raise CandidateStateError("retarget HIT is missing material_id")
                    if self._has_active_tool_retarget(advertiser_id, ad_id, material_id):
                        skipped_guard += 1
                        continue
                eligible.append(row)

            if not eligible:
                continue

            groups: list[list[Mapping[str, Any]]]
            if action_type == CREATE_RETARGET and grouping_mode == MERGED:
                ordered = sorted(eligible, key=lambda row: (str(row["material_id"]), str(row["object_uid"])))
                groups = [ordered[index : index + MAX_RETARGET_MATERIALS] for index in range(0, len(ordered), MAX_RETARGET_MATERIALS)]
            else:
                groups = [[row] for row in eligible]

            for items in groups:
                object_uids = tuple(sorted(str(item["object_uid"]) for item in items))
                fingerprint = _hash(
                    "group_",
                    str(first["strategy_version_id"]),
                    action_type,
                    advertiser_id,
                    ad_id,
                    batch,
                    _json(action_config),
                    *object_uids,
                )
                candidate_id = _hash("candidate_", fingerprint)
                status = WAITING_CONFIRMATION if execution_mode == MANUAL else APPROVED
                approved_at = None if status == WAITING_CONFIRMATION else now
                expires_at = expires if status == WAITING_CONFIRMATION else None

                def work(conn):
                    cursor = conn.execute(
                        """INSERT OR IGNORE INTO candidate_batch(
                           candidate_id,strategy_id,strategy_version_id,action_type,advertiser_id,ad_id,
                           execution_mode,grouping_mode,execution_params_json,group_fingerprint,status,
                           created_at,expires_at,approved_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            candidate_id,
                            first["strategy_id"],
                            first["strategy_version_id"],
                            action_type,
                            advertiser_id,
                            ad_id,
                            execution_mode,
                            grouping_mode,
                            _json(action_config),
                            fingerprint,
                            status,
                            now,
                            expires_at,
                            approved_at,
                        ),
                    )
                    inserted = int(cursor.rowcount == 1)
                    if inserted:
                        for item in items:
                            item_id = _hash("candidate_item_", candidate_id, str(item["object_uid"]))
                            before_state = {
                                "source_batch_id": batch,
                                "source_collected_at": item["source_collected_at"],
                                "condition_snapshot": json.loads(str(item["condition_snapshot_json"])),
                            }
                            conn.execute(
                                """INSERT INTO candidate_item(
                                   candidate_item_id,candidate_id,hit_id,object_uid,material_id,control_task_id,
                                   metric_snapshot_json,before_state_json,created_at
                                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                                (
                                    item_id,
                                    candidate_id,
                                    item["hit_id"],
                                    item["object_uid"],
                                    item["material_id"],
                                    item["control_task_id"],
                                    item["metric_snapshot_json"],
                                    _json(before_state),
                                    now,
                                ),
                            )
                    return inserted

                inserted = int(self._writer.transaction(work).result(timeout=10))
                if inserted:
                    built += 1
                else:
                    existing += 1
                candidate_ids.append(candidate_id)

        return CandidateBuildSummary(
            target_uid=target,
            source_batch_id=batch,
            eligible_hits=len(rows),
            built_candidates=built,
            existing_candidates=existing,
            skipped_active_guard=skipped_guard,
            skipped_reject_cooldown=skipped_cooldown,
            candidate_ids=tuple(candidate_ids),
        )

    def approve(self, candidate_id: str) -> CandidateDecision:
        return self._decide(candidate_id, approve=True)

    def reject(self, candidate_id: str) -> CandidateDecision:
        return self._decide(candidate_id, approve=False)

    def expire_due(self) -> int:
        now = _iso(self._clock())
        return int(
            self._writer.execute(
                """UPDATE candidate_batch SET status='EXPIRED'
                   WHERE status='WAITING_CONFIRMATION' AND expires_at IS NOT NULL AND expires_at<=?""",
                (now,),
            ).result(timeout=5).rowcount
        )

    def cancel(self, candidate_id: str, reason: str) -> CandidateDecision:
        reason_text = str(reason or "").strip()
        if not reason_text:
            raise ValueError("cancel reason is required")
        now = _iso(self._clock())

        def work(conn):
            row = conn.execute("SELECT * FROM candidate_batch WHERE candidate_id=?", (candidate_id,)).fetchone()
            if row is None:
                raise CandidateStateError("candidate does not exist")
            status = str(row["status"])
            if status == CANCELLED:
                return CandidateDecision(candidate_id, CANCELLED, False, row["expires_at"], row["reject_cooldown_until"])
            if status not in {WAITING_CONFIRMATION, APPROVED}:
                raise CandidateStateError(f"candidate cannot be cancelled from {status}")
            conn.execute(
                "UPDATE candidate_batch SET status='CANCELLED',cancelled_at=?,cancel_reason=? WHERE candidate_id=?",
                (now, reason_text, candidate_id),
            )
            return CandidateDecision(candidate_id, CANCELLED, True, row["expires_at"], row["reject_cooldown_until"])

        return self._writer.transaction(work).result(timeout=5)

    def get(self, candidate_id: str) -> dict[str, Any] | None:
        with self._database.connect(readonly=True) as conn:
            row = conn.execute("SELECT * FROM candidate_batch WHERE candidate_id=?", (candidate_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["items"] = [
                dict(item)
                for item in conn.execute(
                    "SELECT * FROM candidate_item WHERE candidate_id=? ORDER BY object_uid",
                    (candidate_id,),
                ).fetchall()
            ]
            return result

    def _decide(self, candidate_id: str, *, approve: bool) -> CandidateDecision:
        now_dt = self._clock().astimezone(timezone.utc)
        now = _iso(now_dt)
        cooldown_until = _iso(now_dt + timedelta(minutes=self._reject_cooldown))

        def work(conn):
            row = conn.execute("SELECT * FROM candidate_batch WHERE candidate_id=?", (candidate_id,)).fetchone()
            if row is None:
                raise CandidateStateError("candidate does not exist")
            status = str(row["status"])
            expires_at = str(row["expires_at"]) if row["expires_at"] else None
            reject_until = str(row["reject_cooldown_until"]) if row["reject_cooldown_until"] else None

            if status == APPROVED and approve:
                return CandidateDecision(candidate_id, APPROVED, False, expires_at, reject_until)
            if status == REJECTED and not approve:
                return CandidateDecision(candidate_id, REJECTED, False, expires_at, reject_until)
            if status != WAITING_CONFIRMATION:
                raise CandidateStateError(f"candidate decision is not allowed from {status}")
            if expires_at is not None and expires_at <= now:
                conn.execute("UPDATE candidate_batch SET status='EXPIRED' WHERE candidate_id=?", (candidate_id,))
                return CandidateDecision(candidate_id, EXPIRED, True, expires_at, reject_until)

            if approve:
                conn.execute(
                    "UPDATE candidate_batch SET status='APPROVED',approved_at=? WHERE candidate_id=?",
                    (now, candidate_id),
                )
                return CandidateDecision(candidate_id, APPROVED, True, expires_at, reject_until)

            conn.execute(
                """UPDATE candidate_batch SET status='REJECTED',rejected_at=?,reject_cooldown_until=?
                   WHERE candidate_id=?""",
                (now, cooldown_until, candidate_id),
            )
            return CandidateDecision(candidate_id, REJECTED, True, expires_at, cooldown_until)

        return self._writer.transaction(work).result(timeout=5)

    def _has_active_tool_retarget(self, advertiser_id: str, ad_id: str, material_id: str) -> bool:
        with self._database.connect(readonly=True) as conn:
            row = conn.execute(
                """SELECT 1
                   FROM control_task_registry r
                   JOIN control_task_latest l ON l.control_task_uid=r.control_task_uid
                   JOIN control_task_material m ON m.control_task_uid=r.control_task_uid
                   WHERE r.advertiser_id=? AND r.ad_id=? AND m.material_id=?
                     AND r.created_by_tool=1 AND l.official_task_status='PROCESSING'
                   LIMIT 1""",
                (advertiser_id, ad_id, material_id),
            ).fetchone()
        return row is not None

    def _reject_cooldown_active(self, strategy_id: str, object_uid: str, now: str) -> bool:
        with self._database.connect(readonly=True) as conn:
            row = conn.execute(
                """SELECT 1
                   FROM candidate_batch b
                   JOIN candidate_item i ON i.candidate_id=b.candidate_id
                   WHERE b.strategy_id=? AND i.object_uid=? AND b.status='REJECTED'
                     AND b.reject_cooldown_until IS NOT NULL AND b.reject_cooldown_until>?
                   LIMIT 1""",
                (strategy_id, object_uid, now),
            ).fetchone()
        return row is not None
