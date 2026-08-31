"""Phase 5/6 候选构建 Durable Job。

策略求值完成后排本地候选构建任务；候选构建不会访问千川网络，也不会执行平台 POST。
候选持久化成功后可触发一个可选的本地通知桥接（例如写入 Feishu Outbox）。通知桥接本身
必须幂等；若它抛错，Durable Job 失败后可安全重跑候选构建与通知写入。
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from typing import Any

from commercial_v1.runtime.jobs import ClaimedJob, PersistentJobStore

from .service import CandidateService

CANDIDATE_BUILD = "CANDIDATE_BUILD"
BusinessAllowed = Callable[[], bool]
CandidateReady = Callable[[Sequence[str]], Any]


def _always_allowed() -> bool:
    return True


def _result_payload(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool, list, tuple, dict)):
        return value
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Mapping):
        return dict(value)
    return str(value)


def candidate_job_uid(target_uid: str, source_batch_id: str) -> str:
    raw = f"{target_uid}|{source_batch_id}".encode("utf-8")
    return "candidate-job-" + hashlib.sha256(raw).hexdigest()


class CandidateBuildEnqueuer:
    def __init__(self, jobs: PersistentJobStore) -> None:
        self._jobs = jobs

    def __call__(self, target_uid: str, source_batch_id: str) -> str:
        uid = candidate_job_uid(target_uid, source_batch_id)
        try:
            return self._jobs.enqueue(
                CANDIDATE_BUILD,
                {"target_uid": target_uid, "source_batch_id": source_batch_id},
                priority=70,
                job_uid=uid,
            )
        except Exception:
            existing = self._jobs.get(uid)
            if existing is not None:
                return uid
            raise


class CandidateBuildHandler:
    def __init__(
        self,
        service: CandidateService,
        *,
        business_allowed: BusinessAllowed = _always_allowed,
        on_candidates_ready: CandidateReady | None = None,
    ) -> None:
        self._service = service
        self._business_allowed = business_allowed
        self._on_candidates_ready = on_candidates_ready

    def __call__(self, job: ClaimedJob) -> Mapping[str, Any]:
        target_uid = str(job.payload.get("target_uid") or "").strip()
        source_batch_id = str(job.payload.get("source_batch_id") or "").strip()
        if not target_uid or not source_batch_id:
            raise ValueError("candidate build job payload is incomplete")
        if not self._business_allowed():
            return {
                "target_uid": target_uid,
                "source_batch_id": source_batch_id,
                "skipped": "LICENSE_BLOCKED",
            }
        result = self._service.build_from_source_batch(target_uid, source_batch_id)
        notification = None
        if self._on_candidates_ready is not None and result.candidate_ids:
            notification = _result_payload(self._on_candidates_ready(result.candidate_ids))
        return {
            "target_uid": result.target_uid,
            "source_batch_id": result.source_batch_id,
            "eligible_hits": result.eligible_hits,
            "built_candidates": result.built_candidates,
            "existing_candidates": result.existing_candidates,
            "skipped_active_guard": result.skipped_active_guard,
            "skipped_reject_cooldown": result.skipped_reject_cooldown,
            "skipped_missing_control_baseline": result.skipped_missing_control_baseline,
            "candidate_ids": list(result.candidate_ids),
            "notification": notification,
        }
