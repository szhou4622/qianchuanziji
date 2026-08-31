"""Phase 5 候选构建 Durable Job。

策略求值完成后只排本地候选构建任务；候选构建不会访问千川网络，也不会执行平台 POST。
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from typing import Any

from commercial_v1.runtime.jobs import ClaimedJob, PersistentJobStore

from .service import CandidateService

CANDIDATE_BUILD = "CANDIDATE_BUILD"
BusinessAllowed = Callable[[], bool]


def _always_allowed() -> bool:
    return True


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
    ) -> None:
        self._service = service
        self._business_allowed = business_allowed

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
        return {
            "target_uid": result.target_uid,
            "source_batch_id": result.source_batch_id,
            "eligible_hits": result.eligible_hits,
            "built_candidates": result.built_candidates,
            "existing_candidates": result.existing_candidates,
            "skipped_active_guard": result.skipped_active_guard,
            "skipped_reject_cooldown": result.skipped_reject_cooldown,
            "candidate_ids": list(result.candidate_ids),
        }
