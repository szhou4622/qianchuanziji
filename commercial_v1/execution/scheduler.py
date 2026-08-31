"""Phase 6 Execution 本地调度器。

扫描两类事实：
1. APPROVED Candidate 尚无 Execution -> 确保 EXECUTION_PREPARE；
2. PENDING Execution -> 确保 EXECUTION_PREFLIGHT。

相同对象使用确定性 Job UID。FAILED/SUCCESS 但目标事实仍未收敛时，经过最小退避后重排同一
Durable Job，不生成无穷 Job 记录。License 不允许业务时完全不排新任务。
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from commercial_v1.runtime.jobs import PersistentJobStore
from commercial_v1.storage.database import Database

from .jobs import (
    ExecutionJobEnqueuer,
    execution_preflight_job_uid,
    execution_prepare_job_uid,
)

BusinessAllowed = Callable[[], bool]
Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class ExecutionScheduler:
    def __init__(
        self,
        database: Database,
        jobs: PersistentJobStore,
        *,
        business_allowed: BusinessAllowed,
        clock: Clock = utc_now,
        interval_seconds: float = 1.0,
        retry_delay_seconds: float = 15.0,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if retry_delay_seconds < 1:
            raise ValueError("retry_delay_seconds must be at least 1")
        self._database = database
        self._jobs = jobs
        self._enqueuer = ExecutionJobEnqueuer(jobs)
        self._business_allowed = business_allowed
        self._clock = clock
        self._interval = float(interval_seconds)
        self._retry_delay = float(retry_delay_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_error: str | None = None
        self._last_result: dict[str, int] = {
            "prepare_enqueued": 0,
            "prepare_requeued": 0,
            "preflight_enqueued": 0,
            "preflight_requeued": 0,
        }

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="execution-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self._interval * 4))

    def restart(self) -> None:
        self.stop()
        self._thread = None
        self.start()

    def run_once(self) -> dict[str, int]:
        result = {
            "prepare_enqueued": 0,
            "prepare_requeued": 0,
            "preflight_enqueued": 0,
            "preflight_requeued": 0,
        }
        if not self._business_allowed():
            self._last_result = result
            self._last_error = None
            return dict(result)

        with self._database.connect(readonly=True) as conn:
            candidates = [
                str(row["candidate_id"])
                for row in conn.execute(
                    """SELECT c.candidate_id
                       FROM candidate_batch c
                       LEFT JOIN execution_task e ON e.candidate_id=c.candidate_id
                       WHERE c.status='APPROVED' AND e.execution_id IS NULL
                       ORDER BY c.approved_at,c.created_at,c.candidate_id"""
                ).fetchall()
            ]
            executions = [
                str(row["execution_id"])
                for row in conn.execute(
                    """SELECT execution_id FROM execution_task
                       WHERE status='PENDING' ORDER BY created_at,execution_id"""
                ).fetchall()
            ]

        for candidate_id in candidates:
            outcome = self._ensure_prepare(candidate_id)
            if outcome is not None:
                result[outcome] += 1
        for execution_id in executions:
            outcome = self._ensure_preflight(execution_id)
            if outcome is not None:
                result[outcome] += 1

        self._last_result = result
        self._last_error = None
        return dict(result)

    def health_snapshot(self) -> dict[str, Any]:
        return {
            "alive": bool(self._thread and self._thread.is_alive()),
            "last_result": dict(self._last_result),
            "last_error": self._last_error,
            "retry_delay_seconds": self._retry_delay,
        }

    def _ensure_prepare(self, candidate_id: str) -> str | None:
        uid = execution_prepare_job_uid(candidate_id)
        existing = self._jobs.get(uid)
        if existing is None:
            self._enqueuer.prepare(candidate_id)
            return "prepare_enqueued"
        if self._retryable_terminal(existing) and self._jobs.requeue(uid):
            return "prepare_requeued"
        return None

    def _ensure_preflight(self, execution_id: str) -> str | None:
        uid = execution_preflight_job_uid(execution_id)
        existing = self._jobs.get(uid)
        if existing is None:
            self._enqueuer.preflight(execution_id)
            return "preflight_enqueued"
        if self._retryable_terminal(existing) and self._jobs.requeue(uid):
            return "preflight_requeued"
        return None

    def _retryable_terminal(self, job: dict[str, Any]) -> bool:
        if str(job.get("status") or "").upper() not in {"FAILED", "SUCCESS", "BLOCKED"}:
            return False
        updated = _parse(job.get("updated_at"))
        if updated is None:
            return True
        return updated <= self._clock().astimezone(timezone.utc) - timedelta(seconds=self._retry_delay)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except BaseException as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"[:1000]
            self._stop.wait(self._interval)
