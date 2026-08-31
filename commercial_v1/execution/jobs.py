"""Phase 6 Execution Durable Jobs。

PREPARE 是纯本地确定性冻结；PREFLIGHT 只读千川 GET。两者都可安全重放。
真正业务 POST 不属于本模块。
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from typing import Any

from commercial_v1.runtime.jobs import ClaimedJob, PersistentJobStore

from .service import ExecutionPreflightService, ExecutionService

EXECUTION_PREPARE = "EXECUTION_PREPARE"
EXECUTION_PREFLIGHT = "EXECUTION_PREFLIGHT"
BusinessAllowed = Callable[[], bool]


def _always_allowed() -> bool:
    return True


class ExecutionGateBlocked(RuntimeError):
    pass


def execution_prepare_job_uid(candidate_id: str) -> str:
    raw = str(candidate_id or "").strip()
    if not raw:
        raise ValueError("candidate_id is required")
    return "execution-prepare-job-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def execution_preflight_job_uid(execution_id: str) -> str:
    raw = str(execution_id or "").strip()
    if not raw:
        raise ValueError("execution_id is required")
    return "execution-preflight-job-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ExecutionJobEnqueuer:
    def __init__(self, jobs: PersistentJobStore) -> None:
        self._jobs = jobs

    def prepare(self, candidate_id: str) -> str:
        uid = execution_prepare_job_uid(candidate_id)
        return self._enqueue_once(
            uid,
            EXECUTION_PREPARE,
            {"candidate_id": str(candidate_id)},
            priority=60,
        )

    def preflight(self, execution_id: str) -> str:
        uid = execution_preflight_job_uid(execution_id)
        return self._enqueue_once(
            uid,
            EXECUTION_PREFLIGHT,
            {"execution_id": str(execution_id)},
            priority=55,
        )

    def _enqueue_once(self, uid: str, job_type: str, payload: Mapping[str, Any], *, priority: int) -> str:
        try:
            return self._jobs.enqueue(job_type, payload, priority=priority, job_uid=uid)
        except Exception:
            if self._jobs.get(uid) is not None:
                return uid
            raise


class ExecutionPrepareHandler:
    def __init__(
        self,
        service: ExecutionService,
        *,
        business_allowed: BusinessAllowed = _always_allowed,
    ) -> None:
        self._service = service
        self._business_allowed = business_allowed

    def __call__(self, job: ClaimedJob) -> Mapping[str, Any]:
        if not self._business_allowed():
            raise ExecutionGateBlocked("LICENSE_BLOCKED")
        candidate_id = str(job.payload.get("candidate_id") or "").strip()
        if not candidate_id:
            raise ValueError("execution prepare payload is incomplete")
        result = self._service.prepare_from_candidate(candidate_id)
        return {
            "candidate_id": result.candidate_id,
            "execution_id": result.execution_id,
            "action_type": result.action_type,
            "status": result.status,
            "created": result.created,
            "object_count": result.object_count,
        }


class ExecutionPreflightHandler:
    def __init__(
        self,
        service: ExecutionPreflightService,
        *,
        business_allowed: BusinessAllowed = _always_allowed,
    ) -> None:
        self._service = service
        self._business_allowed = business_allowed

    def __call__(self, job: ClaimedJob) -> Mapping[str, Any]:
        if not self._business_allowed():
            raise ExecutionGateBlocked("LICENSE_BLOCKED")
        execution_id = str(job.payload.get("execution_id") or "").strip()
        if not execution_id:
            raise ValueError("execution preflight payload is incomplete")
        result = self._service.preflight(execution_id)
        return {
            "execution_id": result.execution_id,
            "status": result.status,
            "changed": result.changed,
            "reason": result.reason,
            "request_ids": list(result.request_ids),
        }
