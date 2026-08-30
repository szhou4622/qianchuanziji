"""Phase 4 策略求值 Job。

可信热采集完成后只排本地策略任务；策略 Job 可重跑，因为 HIT 使用确定性主键幂等落库。
软件授权失效时不得继续产生新的策略结果。
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from typing import Any

from commercial_v1.runtime.jobs import ClaimedJob, PersistentJobStore

from .engine import StrategyEvaluationService

STRATEGY_MATERIAL_EVALUATE = "STRATEGY_MATERIAL_EVALUATE"
STRATEGY_CONTROL_EVALUATE = "STRATEGY_CONTROL_EVALUATE"
BusinessAllowed = Callable[[], bool]


def _always_allowed() -> bool:
    return True


def strategy_job_uid(pipeline_type: str, target_uid: str, batch_id: str) -> str:
    raw = f"{pipeline_type}|{target_uid}|{batch_id}".encode("utf-8")
    return "strategy-job-" + hashlib.sha256(raw).hexdigest()


class StrategyEvaluationEnqueuer:
    """把成功的 Phase 3 热采集批次转换为 Durable 策略 Job。"""

    def __init__(self, jobs: PersistentJobStore) -> None:
        self._jobs = jobs

    def __call__(self, target_uid: str, pipeline_type: str, batch_id: str) -> str | None:
        pipeline = str(pipeline_type or "").strip().upper()
        if pipeline == "MATERIAL_5M":
            job_type = STRATEGY_MATERIAL_EVALUATE
        elif pipeline == "CONTROL_5M":
            job_type = STRATEGY_CONTROL_EVALUATE
        else:
            return None
        uid = strategy_job_uid(pipeline, target_uid, batch_id)
        try:
            return self._jobs.enqueue(
                job_type,
                {"target_uid": target_uid, "source_batch_id": batch_id},
                priority=60,
                job_uid=uid,
            )
        except Exception:
            existing = self._jobs.get(uid)
            if existing is not None:
                return uid
            raise


class StrategyEvaluationHandler:
    def __init__(
        self,
        service: StrategyEvaluationService,
        job_type: str,
        *,
        business_allowed: BusinessAllowed = _always_allowed,
    ) -> None:
        if job_type not in {STRATEGY_MATERIAL_EVALUATE, STRATEGY_CONTROL_EVALUATE}:
            raise ValueError("unsupported strategy evaluation job type")
        self._service = service
        self._job_type = job_type
        self._business_allowed = business_allowed

    def __call__(self, job: ClaimedJob) -> Mapping[str, Any]:
        target_uid = str(job.payload.get("target_uid") or "").strip()
        source_batch_id = str(job.payload.get("source_batch_id") or "").strip()
        if not target_uid or not source_batch_id:
            raise ValueError("strategy evaluation job payload is incomplete")
        if not self._business_allowed():
            return {
                "target_uid": target_uid,
                "source_batch_id": source_batch_id,
                "skipped": "LICENSE_BLOCKED",
            }
        if self._job_type == STRATEGY_MATERIAL_EVALUATE:
            result = self._service.evaluate_material_batch(target_uid, source_batch_id)
        else:
            result = self._service.evaluate_control_batch(target_uid, source_batch_id)
        return {
            "target_uid": result.target_uid,
            "pipeline_type": result.pipeline_type,
            "source_batch_id": result.source_batch_id,
            "evaluated": result.evaluated,
            "hit": result.hit,
            "not_hit": result.not_hit,
            "not_evaluable": result.not_evaluable,
            "persisted_hits": result.persisted_hits,
            "suppressed_hits": result.suppressed_hits,
        }
