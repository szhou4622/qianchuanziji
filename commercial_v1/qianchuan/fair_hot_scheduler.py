"""Phase 3 100 计划公平调度与单账户并发上限。

目标：10 个账户 × 每账户 10 个计划时，不能让第一个账户的 20 条 Material/Control Job
占满读取池。Scheduler 按账户轮转分层 priority；Handler Gate 再硬限制每个 advertiser
最多 2 个并发官方读取。二者分别解决“公平”和“硬上限”。
"""
from __future__ import annotations

import json
import threading
import uuid
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from commercial_v1.runtime.jobs import ClaimedJob
from commercial_v1.storage.database import Database
from commercial_v1.storage.writer import StorageWriter

from .hot_collection import fixed_five_minute_slot, iso, utc_now
from .hot_scheduler import CONTROL_5M, MATERIAL_5M, HotCollectionScheduler, next_five_minute_boundary

JobHandler = Callable[[ClaimedJob], Mapping[str, Any] | None]


class AdvertiserConcurrencyGate:
    """跨 Worker 的 advertiser 级硬并发阀门。"""

    def __init__(self, database: Database, *, max_per_advertiser: int = 2) -> None:
        if max_per_advertiser < 1:
            raise ValueError("max_per_advertiser must be positive")
        self._database = database
        self._max = int(max_per_advertiser)
        self._lock = threading.Lock()
        self._semaphores: dict[str, threading.BoundedSemaphore] = {}
        self._active: dict[str, int] = defaultdict(int)
        self._peak: dict[str, int] = defaultdict(int)

    def wrap(self, handler: JobHandler) -> JobHandler:
        def guarded(job: ClaimedJob) -> Mapping[str, Any] | None:
            target_uid = str(job.payload.get("target_uid") or "").strip()
            if not target_uid:
                return handler(job)
            advertiser_id = self._advertiser_id(target_uid)
            semaphore = self._semaphore(advertiser_id)
            # JobWorker 会在等待期间持续 heartbeat，因此这里可以阻塞等待而不会丢 lease。
            semaphore.acquire()
            try:
                with self._lock:
                    self._active[advertiser_id] += 1
                    self._peak[advertiser_id] = max(
                        self._peak[advertiser_id], self._active[advertiser_id]
                    )
                return handler(job)
            finally:
                with self._lock:
                    self._active[advertiser_id] = max(0, self._active[advertiser_id] - 1)
                semaphore.release()
        return guarded

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "max_per_advertiser": self._max,
                "active": dict(self._active),
                "peak": dict(self._peak),
            }

    def _advertiser_id(self, target_uid: str) -> str:
        with self._database.connect(readonly=True) as conn:
            row = conn.execute(
                "SELECT advertiser_id FROM monitor_plan WHERE target_uid=?", (target_uid,)
            ).fetchone()
        if row is None:
            raise ValueError("hot job target plan is missing")
        return str(row["advertiser_id"])

    def _semaphore(self, advertiser_id: str) -> threading.BoundedSemaphore:
        with self._lock:
            semaphore = self._semaphores.get(advertiser_id)
            if semaphore is None:
                semaphore = threading.BoundedSemaphore(self._max)
                self._semaphores[advertiser_id] = semaphore
            return semaphore


class FairHotCollectionScheduler(HotCollectionScheduler):
    """按账户分层生成当前 5m Job，确保每账户第一计划先于任何账户第二计划。"""

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
                   ORDER BY advertiser_id,next_hot_collect_at,target_uid""",
                (current,),
            ).fetchall()
            ordered = self._round_robin(rows)
            account_order = {
                aid: index for index, aid in enumerate(sorted({str(row["advertiser_id"]) for row in rows}))
            }
            account_round: dict[str, int] = defaultdict(int)
            counters = {"enqueued": 0, "skipped_overlap": 0, "skipped_stale": 0}

            for row in ordered:
                target_uid = str(row["target_uid"])
                advertiser_id = str(row["advertiser_id"])
                due_text = str(row["next_hot_collect_at"])
                try:
                    due_dt = datetime.fromisoformat(due_text.replace("Z", "+00:00")).astimezone(timezone.utc)
                except ValueError:
                    due_dt = current_dt
                lateness = max(0.0, (current_dt - due_dt).total_seconds())

                if row["last_hot_collect_at"] and lateness > self._max_lateness:
                    conn.execute(
                        "UPDATE monitor_plan SET next_hot_collect_at=?,updated_at=? WHERE target_uid=?",
                        (iso(next_five_minute_boundary(current_dt)), current, target_uid),
                    )
                    counters["skipped_stale"] += 1
                    continue

                round_no = account_round[advertiser_id]
                account_round[advertiser_id] += 1
                # 10 账户上限下每轮预留 20 个 priority 槽；同一计划两条流水同优先级。
                priority = 40 + round_no * 20 + account_order[advertiser_id]
                slot = due_text if not row["last_hot_collect_at"] else fixed_five_minute_slot(due_dt)

                for pipeline in (MATERIAL_5M, CONTROL_5M):
                    duplicate = conn.execute(
                        """SELECT 1 FROM collection_batch
                           WHERE target_uid=? AND pipeline_type=? AND scheduled_at=? LIMIT 1""",
                        (target_uid, pipeline, slot),
                    ).fetchone()
                    if duplicate is not None:
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
                        conn.execute(
                            """INSERT INTO collection_batch(
                               batch_id,account_uid,target_uid,advertiser_id,ad_id,pipeline_type,scheduled_at,
                               started_at,finished_at,status,error_type,error_message,created_at
                               ) VALUES(?,?,?,?,?,?,?,?,?,'SKIPPED_OVERLAP','OVERLAP',?,?)""",
                            (
                                str(uuid.uuid4()), row["account_uid"], target_uid, advertiser_id,
                                row["ad_id"], pipeline, slot, current, current,
                                "previous hot job still queued/running", current,
                            ),
                        )
                        counters["skipped_overlap"] += 1
                        continue

                    payload_json = json.dumps(
                        {"target_uid": target_uid, "scheduled_at": slot},
                        ensure_ascii=False, separators=(",", ":"), sort_keys=True,
                    )
                    conn.execute(
                        """INSERT INTO background_job(
                           job_uid,job_type,priority,payload_json,status,due_at,created_at,updated_at
                           ) VALUES(?,?,?,?,'QUEUED',?,?,?)""",
                        (str(uuid.uuid4()), pipeline, priority, payload_json, current, current, current),
                    )
                    counters["enqueued"] += 1

                conn.execute(
                    "UPDATE monitor_plan SET next_hot_collect_at=?,updated_at=? WHERE target_uid=?",
                    (iso(next_five_minute_boundary(current_dt)), current, target_uid),
                )
            return counters

        result = self._writer.transaction(work).result(timeout=15)
        self._enqueued += int(result["enqueued"])
        self._skipped_overlap += int(result["skipped_overlap"])
        self._skipped_stale += int(result["skipped_stale"])
        self._last_error = None
        return dict(result)

    @staticmethod
    def _round_robin(rows) -> list[Any]:
        groups: dict[str, deque[Any]] = defaultdict(deque)
        for row in rows:
            groups[str(row["advertiser_id"])].append(row)
        account_ids = sorted(groups)
        ordered: list[Any] = []
        while True:
            progressed = False
            for advertiser_id in account_ids:
                queue = groups[advertiser_id]
                if queue:
                    ordered.append(queue.popleft())
                    progressed = True
            if not progressed:
                return ordered
