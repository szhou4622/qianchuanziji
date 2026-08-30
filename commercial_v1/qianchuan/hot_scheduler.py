"""Phase 3 固定墙钟 5 分钟热采集调度器。

实时周期只生成当前周期，不补历史周期；Material / Control 独立排队，单目标同流水不重叠。
可信 SUCCESS 批次可通过回调交给 Phase 4 持久策略队列，异常批次绝不触发策略。
"""
from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from commercial_v1.runtime.jobs import ClaimedJob
from commercial_v1.security.redaction import sanitize_text
from commercial_v1.storage.database import Database
from commercial_v1.storage.writer import StorageWriter

from .hot_collection import HotCollectionService, fixed_five_minute_slot, iso, utc_now

MATERIAL_5M = "MATERIAL_5M"
CONTROL_5M = "CONTROL_5M"
BusinessAllowed = Callable[[], bool]
TrustedBatchCallback = Callable[[str, str, str], Any]


def _always_allowed() -> bool:
    return True


def next_five_minute_boundary(value: datetime | None = None) -> datetime:
    current = (value or utc_now()).astimezone(timezone.utc)
    base_minute = current.minute - current.minute % 5
    base = current.replace(minute=base_minute, second=0, microsecond=0)
    if base <= current:
        base += timedelta(minutes=5)
    return base


class HotCollectionHandler:
    def __init__(
        self,
        service: HotCollectionService,
        writer: StorageWriter,
        pipeline_type: str,
        *,
        business_allowed: BusinessAllowed = _always_allowed,
        on_trusted_batch: TrustedBatchCallback | None = None,
    ) -> None:
        if pipeline_type not in {MATERIAL_5M, CONTROL_5M}:
            raise ValueError("unsupported hot pipeline")
        self._service = service
        self._writer = writer
        self._pipeline = pipeline_type
        self._business_allowed = business_allowed
        self._on_trusted_batch = on_trusted_batch

    def __call__(self, job: ClaimedJob) -> Mapping[str, Any]:
        target_uid = str(job.payload.get("target_uid") or "").strip()
        scheduled_at = str(job.payload.get("scheduled_at") or "").strip()
        if not target_uid or not scheduled_at:
            raise ValueError("hot collection job payload is incomplete")
        if not self._business_allowed():
            return {"target_uid": target_uid, "pipeline_type": self._pipeline, "skipped": "LICENSE_BLOCKED"}

        try:
            if self._pipeline == MATERIAL_5M:
                result = self._service.collect_materials(target_uid, scheduled_at=scheduled_at)
            else:
                result = self._service.collect_controls(target_uid, scheduled_at=scheduled_at)

            # 只有完整、可信 SUCCESS 才能进入策略。SUSPICIOUS_EMPTY / FAILED /
            # MISSING confirmation 本身都不会从这里直接产生策略任务。
            strategy_job_uid = None
            if result.status == "SUCCESS" and self._on_trusted_batch is not None:
                strategy_job_uid = self._on_trusted_batch(
                    target_uid,
                    self._pipeline,
                    result.batch_id,
                )
            return {
                "target_uid": target_uid,
                "pipeline_type": self._pipeline,
                "batch_id": result.batch_id,
                "status": result.status,
                "row_count": result.row_count,
                "suspicious_empty": result.suspicious_empty,
                "missing_count": result.missing_count,
                "strategy_job_uid": strategy_job_uid,
            }
        finally:
            self._advance_plan_clock(target_uid)

    def _advance_plan_clock(self, target_uid: str) -> None:
        now_dt = utc_now()
        now = iso(now_dt)
        next_due = iso(next_five_minute_boundary(now_dt))
        self._writer.execute(
            """UPDATE monitor_plan SET last_hot_collect_at=?,
               next_hot_collect_at=CASE
                 WHEN monitor_enabled=1 AND lifecycle_state='ACTIVE_COLLECTING' AND collection_active=1
                 THEN ? ELSE NULL END,
               updated_at=? WHERE target_uid=?""",
            (now, next_due, now, target_uid),
        ).result(timeout=5)


class HotCollectionScheduler:
    """为 ACTIVE_COLLECTING 计划生成当前 Material/Control 5m Job。"""

    def __init__(
        self,
        database: Database,
        writer: StorageWriter,
        *,
        interval_seconds: float = 5.0,
        max_lateness_seconds: int = 90,
        business_allowed: BusinessAllowed = _always_allowed,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._database = database
        self._writer = writer
        self._interval = float(interval_seconds)
        self._max_lateness = max(0, int(max_lateness_seconds))
        self._business_allowed = business_allowed
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_error: str | None = None
        self._last_scan_at: str | None = None
        self._enqueued = 0
        self._skipped_overlap = 0
        self._skipped_stale = 0
        self._license_blocked = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="hot-collection-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(2.0, self._interval * 2))

    def restart(self) -> None:
        self.stop()
        self._thread = None
        self.start()

    def run_once(self, *, now: datetime | None = None) -> dict[str, int]:
        current_dt = (now or utc_now()).astimezone(timezone.utc)
        current = iso(current_dt)
        self._last_scan_at = current
        if not self._business_allowed():
            self._license_blocked = True
            self._last_error = None
            return {"enqueued": 0, "skipped_overlap": 0, "skipped_stale": 0}
        self._license_blocked = False

        def work(conn):
            rows = conn.execute(
                """SELECT target_uid,account_uid,advertiser_id,ad_id,next_hot_collect_at,last_hot_collect_at
                   FROM monitor_plan
                   WHERE monitor_enabled=1 AND lifecycle_state='ACTIVE_COLLECTING'
                     AND collection_active=1 AND official_status='DELIVERY_OK'
                     AND next_hot_collect_at IS NOT NULL AND next_hot_collect_at<=?
                   ORDER BY next_hot_collect_at ASC,target_uid ASC""",
                (current,),
            ).fetchall()
            counters = {"enqueued": 0, "skipped_overlap": 0, "skipped_stale": 0}
            for row in rows:
                target_uid = str(row["target_uid"])
                due_text = str(row["next_hot_collect_at"])
                try:
                    due_dt = datetime.fromisoformat(due_text.replace("Z", "+00:00")).astimezone(timezone.utc)
                except ValueError:
                    due_dt = current_dt
                lateness = max(0.0, (current_dt - due_dt).total_seconds())

                if row["last_hot_collect_at"] and lateness > self._max_lateness:
                    next_due = iso(next_five_minute_boundary(current_dt))
                    conn.execute(
                        "UPDATE monitor_plan SET next_hot_collect_at=?,updated_at=? WHERE target_uid=?",
                        (next_due, current, target_uid),
                    )
                    counters["skipped_stale"] += 1
                    continue

                slot = due_text if not row["last_hot_collect_at"] else fixed_five_minute_slot(due_dt)
                for pipeline in (MATERIAL_5M, CONTROL_5M):
                    existing_batch = conn.execute(
                        """SELECT 1 FROM collection_batch
                           WHERE target_uid=? AND pipeline_type=? AND scheduled_at=? LIMIT 1""",
                        (target_uid, pipeline, slot),
                    ).fetchone()
                    if existing_batch is not None:
                        continue

                    active_jobs = conn.execute(
                        """SELECT payload_json FROM background_job
                           WHERE job_type=? AND status IN('QUEUED','RUNNING')""",
                        (pipeline,),
                    ).fetchall()
                    overlapping = False
                    for active in active_jobs:
                        try:
                            payload = json.loads(str(active["payload_json"]))
                        except Exception:
                            continue
                        if str(payload.get("target_uid") or "") == target_uid:
                            overlapping = True
                            break
                    if overlapping:
                        batch_id = str(uuid.uuid4())
                        conn.execute(
                            """INSERT INTO collection_batch(
                               batch_id,account_uid,target_uid,advertiser_id,ad_id,pipeline_type,scheduled_at,
                               started_at,finished_at,status,error_type,error_message,created_at
                               ) VALUES(?,?,?,?,?,?,?,?,?,'SKIPPED_OVERLAP','OVERLAP',?,?)""",
                            (
                                batch_id,
                                row["account_uid"],
                                target_uid,
                                row["advertiser_id"],
                                row["ad_id"],
                                pipeline,
                                slot,
                                current,
                                current,
                                "previous hot job still queued/running",
                                current,
                            ),
                        )
                        counters["skipped_overlap"] += 1
                        continue

                    payload_json = json.dumps(
                        {"target_uid": target_uid, "scheduled_at": slot},
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    conn.execute(
                        """INSERT INTO background_job(
                           job_uid,job_type,priority,payload_json,status,due_at,created_at,updated_at
                           ) VALUES(?,?,40,?,'QUEUED',?,?,?)""",
                        (str(uuid.uuid4()), pipeline, payload_json, current, current, current),
                    )
                    counters["enqueued"] += 1

                conn.execute(
                    "UPDATE monitor_plan SET next_hot_collect_at=?,updated_at=? WHERE target_uid=?",
                    (iso(next_five_minute_boundary(current_dt)), current, target_uid),
                )
            return counters

        result = self._writer.transaction(work).result(timeout=10)
        self._enqueued += int(result["enqueued"])
        self._skipped_overlap += int(result["skipped_overlap"])
        self._skipped_stale += int(result["skipped_stale"])
        self._last_error = None
        return dict(result)

    def health_snapshot(self) -> dict[str, Any]:
        return {
            "alive": bool(self._thread and self._thread.is_alive()),
            "enqueued": self._enqueued,
            "skipped_overlap": self._skipped_overlap,
            "skipped_stale": self._skipped_stale,
            "last_scan_at": self._last_scan_at,
            "license_blocked": self._license_blocked,
            "last_error": self._last_error,
        }

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except BaseException as exc:
                self._last_error = sanitize_text(f"{type(exc).__name__}: {exc}")[:1000]
            if self._stop.wait(self._interval):
                return
