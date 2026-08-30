"""Phase 1 通用 Job Worker 与过期任务恢复 Worker。"""
from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from commercial_v1.security.redaction import sanitize_text

from .jobs import ClaimedJob, PersistentJobStore, StaleJobFencingToken

JobHandler = Callable[[ClaimedJob], Mapping[str, Any] | None]


class JobWorker:
    """只 Claim 已显式注册 handler 的 Job Type。"""

    def __init__(
        self,
        jobs: PersistentJobStore,
        handlers: Mapping[str, JobHandler],
        *,
        instance_id: str | None = None,
        poll_seconds: float = 0.5,
        lease_seconds: int = 45,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self._jobs = jobs
        self._handlers = dict(handlers)
        self._instance_id = instance_id or f"job-worker-{uuid.uuid4()}"
        self._poll_seconds = poll_seconds
        self._lease_seconds = lease_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_error: str | None = None
        self._handled = 0
        self._failed = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=self._instance_id, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(2.0, self._poll_seconds * 4))

    def restart(self) -> None:
        self.stop()
        self._thread = None
        self.start()

    def health_snapshot(self) -> dict[str, Any]:
        return {
            "alive": bool(self._thread and self._thread.is_alive()),
            "handled": self._handled,
            "failed": self._failed,
            "last_error": self._last_error,
            "job_types": sorted(self._handlers),
        }

    def run_once(self) -> bool:
        if not self._handlers:
            return False
        job = self._jobs.claim_next(
            self._instance_id,
            lease_seconds=self._lease_seconds,
            job_types=self._handlers.keys(),
        )
        if job is None:
            return False
        handler = self._handlers[job.job_type]
        try:
            result = handler(job)
            self._jobs.complete(job, result or {})
        except StaleJobFencingToken:
            self._failed += 1
            self._last_error = "STALE_JOB_FENCING_TOKEN"
            return True
        except BaseException as exc:
            self._failed += 1
            self._last_error = sanitize_text(f"{type(exc).__name__}: {exc}")[:1000]
            try:
                self._jobs.fail(job, self._last_error)
            except StaleJobFencingToken:
                self._last_error = "STALE_JOB_FENCING_TOKEN"
            return True
        self._handled += 1
        self._last_error = None
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                handled = self.run_once()
            except BaseException as exc:
                self._last_error = sanitize_text(f"{type(exc).__name__}: {exc}")[:1000]
                handled = False
            if not handled:
                self._stop.wait(self._poll_seconds)


class LeaseRecoveryWorker:
    """定期恢复过期 Job；未配置类型默认 BLOCKED。"""

    def __init__(self, jobs: PersistentJobStore, recovery_policy: Mapping[str, str], *, interval_seconds: float = 15.0) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._jobs = jobs
        self._policy = dict(recovery_policy)
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_result = {"requeue": 0, "abort": 0, "block": 0}
        self._last_error: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="lease-recovery", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(2.0, self._interval * 2))

    def restart(self) -> None:
        self.stop()
        self._thread = None
        self.start()

    def run_once(self) -> dict[str, int]:
        self._last_result = self._jobs.recover_expired(self._policy)
        self._last_error = None
        return dict(self._last_result)

    def health_snapshot(self) -> dict[str, Any]:
        return {
            "alive": bool(self._thread and self._thread.is_alive()),
            "last_result": dict(self._last_result),
            "last_error": self._last_error,
        }

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.run_once()
            except BaseException as exc:
                self._last_error = sanitize_text(f"{type(exc).__name__}: {exc}")[:1000]
