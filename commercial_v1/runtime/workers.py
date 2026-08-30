"""通用 Job Worker 与过期任务恢复 Worker。

Job handler 可能包含分页网络读取，执行时间可能超过初始 lease。Worker 在 handler 运行期间
主动 heartbeat，保证“任务仍在执行”和“租约仍有效”是一致事实；停止 heartbeat 后才允许
complete/fail，避免长请求被 Recovery 误判为僵尸任务。
"""
from __future__ import annotations

import threading
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
        if lease_seconds < 3:
            raise ValueError("lease_seconds must be at least 3")
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
        self._heartbeat_failures = 0

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
            "heartbeat_failures": self._heartbeat_failures,
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
        heartbeat_stop = threading.Event()
        heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(job, heartbeat_stop),
            name=f"{self._instance_id}-heartbeat",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            result = handler(job)
        except StaleJobFencingToken:
            self._stop_heartbeat(heartbeat_stop, heartbeat_thread)
            self._failed += 1
            self._last_error = "STALE_JOB_FENCING_TOKEN"
            return True
        except BaseException as exc:
            self._stop_heartbeat(heartbeat_stop, heartbeat_thread)
            self._failed += 1
            self._last_error = sanitize_text(f"{type(exc).__name__}: {exc}")[:1000]
            try:
                self._jobs.fail(job, self._last_error)
            except StaleJobFencingToken:
                self._last_error = "STALE_JOB_FENCING_TOKEN"
            return True
        self._stop_heartbeat(heartbeat_stop, heartbeat_thread)
        try:
            self._jobs.complete(job, result or {})
        except StaleJobFencingToken:
            self._failed += 1
            self._last_error = "STALE_JOB_FENCING_TOKEN"
            return True
        self._handled += 1
        self._last_error = None
        return True

    def _heartbeat_loop(self, job: ClaimedJob, stop: threading.Event) -> None:
        # 至少每 15 秒、且不晚于 lease 的 1/3 续租一次。
        interval = max(1.0, min(15.0, self._lease_seconds / 3.0))
        while not stop.wait(interval):
            try:
                self._jobs.heartbeat(job, lease_seconds=self._lease_seconds)
            except StaleJobFencingToken:
                # Recovery 已经取得新 fencing token，旧 worker 绝不能继续续租。
                return
            except BaseException as exc:
                self._heartbeat_failures += 1
                self._last_error = sanitize_text(f"HEARTBEAT_{type(exc).__name__}: {exc}")[:1000]
                # 不在 heartbeat 线程里擅自重排业务 Job；主线程最终 complete/fail 会通过
                # fencing 再做一次硬校验。

    @staticmethod
    def _stop_heartbeat(stop: threading.Event, thread: threading.Thread) -> None:
        stop.set()
        thread.join(timeout=2.0)

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
