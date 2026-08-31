"""Phase 4 策略求值 Job。

可信热采集完成后只在该计划确有启用策略时排本地策略任务；策略 Job 可重跑，因为 HIT
使用确定性主键幂等落库。软件授权失效时不得继续产生新的策略结果。
Phase 5 可通过回调把存在未压制 HIT 的结果交给独立候选构建队列。
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from typing import Any

from commercial_v1.runtime.jobs import ClaimedJob, PersistentJobStore

from .engine import HIT, StrategyEvaluationService, StrategyStore

STRATEGY_MATERIAL_EVALUATE = "STRATEGY_MATERIAL_EVALUATE"
STRATEGY_CONTROL_EVALUATE = "STRATEGY_CONTROL_EVALUATE"
BusinessAllowed = Callable[[], bool]
EvaluatedBatchCallback = Callable[[str, str], Any]


def _always_allowed() -> bool:
    return True


def strategy_job_uid(pipeline_type: str, target_uid: str, batch_id: str) -> str:
    raw = f"{pipeline_type}|{target_uid}|{batch_id}".encode("utf-8")
    return "strategy-job-" + hashlib.sha256(raw).hexdigest()


class StrategyEvaluationEnqueuer:
    """把成功的 Phase 3 热采集批次转换为 Durable 策略 Job。

    没有启用策略时不生成空 Job。这样 100 个监控计划在完全没配置策略时，不会每 5 分钟
    额外写入最多 200 条无业务意义的策略队列记录。
    """

    def __init__(self, jobs: PersistentJobStore, store: StrategyStore) -> None:
        self._jobs = jobs
        self._store = store

    def __call__(self, target_uid: str, pipeline_type: str, batch_id: str) -> str | None:
        pipeline = str(pipeline_type or "").strip().upper()
        if pipeline == "MATERIAL_5M":
            job_type = STRATEGY_MATERIAL_EVALUATE
            object_type = "MATERIAL"
        elif pipeline == "CONTROL_5M":
            job_type = STRATEGY_CONTROL_EVALUATE
            object_type = "CONTROL_TASK"
        else:
            return None

        if not self._store.list_for_target(target_uid, object_type=object_type):
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
        on_evaluated_batch: EvaluatedBatchCallback | None = None,
    ) -> None:
        if job_type not in {STRATEGY_MATERIAL_EVALUATE, STRATEGY_CONTROL_EVALUATE}:
            raise ValueError("unsupported strategy evaluation job type")
        self._service = service
        self._job_type = job_type
        self._business_allowed = business_allowed
        self._on_evaluated_batch = on_evaluated_batch

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

        eligible_hits = sum(
            1
            for outcome in result.outcomes
            if outcome.result == HIT and not outcome.suppression_reason
        )
        candidate_job_uid = None
        if eligible_hits and self._on_evaluated_batch is not None:
            candidate_job_uid = self._on_evaluated_batch(target_uid, source_batch_id)

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
            "eligible_hits": eligible_hits,
            "candidate_job_uid": candidate_job_uid,
        }
