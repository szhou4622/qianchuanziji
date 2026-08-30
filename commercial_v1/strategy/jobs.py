"""Phase 4 策略求值 Job。

可信热采集完成后只排本地策略任务；策略 Job 可重跑，因为 HIT 使用确定性主键幂等落库。
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from commercial_v1.runtime.jobs import ClaimedJob, PersistentJobStore

from .engine import StrategyEvaluationService

STRATEGY_MATERIAL_EVALUATE = "STRATEGY_MATERIAL_EVALUATE"
STRATEGY_CONTROL_EVALUATE = "STRATEGY_CONTROL_EVALUATE"


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
            # 确定性 job_uid 使重复入队等价于“已经存在”。只有确实存在时才吞掉异常；
            # 其他数据库错误继续上抛，避免把真正的持久化故障伪装成成功。
            existing = self._jobs.get(uid)
            if existing is not None:
                return uid
            raise


class StrategyEvaluationHandler:
    def __init__(self, service: StrategyEvaluationService, job_type: str) -> None:
        if job_type not in {STRATEGY_MATERIAL_EVALUATE, STRATEGY_CONTROL_EVALUATE}:
            raise ValueError("unsupported strategy evaluation job type")
        self._service = service
        self._job_type = job_type

    def __call__(self, job: ClaimedJob) -> Mapping[str, Any]:
        target_uid = str(job.payload.get("target_uid") or "").strip()
        source_batch_id = str(job.payload.get("source_batch_id") or "").strip()
        if not target_uid or not source_batch_id:
            raise ValueError("strategy evaluation job payload is incomplete")
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
