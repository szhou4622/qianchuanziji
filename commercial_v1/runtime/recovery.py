"""商业版启动恢复。

恢复原则：
- 旧实时采集不补跑；
- Durable Job 才允许重新排队；
- 未知 Job 默认 BLOCKED；
- 已发送/可能已发送的 Execution 只暴露给后续 Reconciliation，绝不在启动时重发。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from commercial_v1.storage.database import Database
from commercial_v1.storage.writer import StorageWriter

from .jobs import PersistentJobStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


DEFAULT_RECOVERY_POLICY: dict[str, str] = {
    # 实时/状态轮询类：过期后不补历史周期，后续 Scheduler 只生成当前周期。
    "MATERIAL_5M": "abort",
    "CONTROL_5M": "abort",
    "PLAN_STATUS_CHECK": "abort",
    "PLAN_CATALOG_REFRESH": "abort",
    # 异常对象证据确认不是历史实时点；重启后必须继续完成，否则 Latest 会永久停在不可信状态。
    "MATERIAL_CONFIRM": "requeue",
    "CONTROL_CONFIRM": "requeue",
    # 策略求值只消费已经落库的可信批次，使用确定性 HIT ID，可安全重放且不能丢。
    "STRATEGY_MATERIAL_EVALUATE": "requeue",
    "STRATEGY_CONTROL_EVALUATE": "requeue",
    # 候选构建只消费已经落库的 HIT，候选 ID/Item ID 确定性生成，可安全重放。
    "CANDIDATE_BUILD": "requeue",
    # 可持续恢复的持久任务。
    "RECONCILE_EXECUTION": "requeue",
    "FEISHU_OUTBOX": "requeue",
    "DAILY_SETTLEMENT": "requeue",
    "MONTHLY_SETTLEMENT": "requeue",
}


@dataclass(frozen=True)
class StartupRecoveryReport:
    aborted_collection_batches: int
    job_recovery: dict[str, int]
    unresolved_execution_count: int
    unresolved_execution_ids: tuple[str, ...]


class StartupRecoveryService:
    def __init__(
        self,
        database: Database,
        writer: StorageWriter,
        jobs: PersistentJobStore,
        *,
        recovery_policy: Mapping[str, str] | None = None,
    ) -> None:
        self._database = database
        self._writer = writer
        self._jobs = jobs
        self._policy = dict(recovery_policy or DEFAULT_RECOVERY_POLICY)

    def run(self) -> StartupRecoveryReport:
        now = _now()

        def abort_stale_collections(conn):
            result = conn.execute(
                """UPDATE collection_batch
                   SET status='ABORTED_BY_RESTART',
                       finished_at=COALESCE(finished_at, ?),
                       error_type=COALESCE(error_type, 'RUNTIME_RESTART'),
                       error_message=COALESCE(error_message, 'previous process exited before batch completion')
                   WHERE status='RUNNING'""",
                (now,),
            )
            return int(result.rowcount)

        aborted = self._writer.transaction(abort_stale_collections).result(timeout=5)
        job_recovery = self._jobs.recover_expired(self._policy)

        with self._database.connect(readonly=True) as conn:
            rows = conn.execute(
                """SELECT execution_id
                   FROM execution_task
                   WHERE status IN ('SUBMITTING','SUBMITTED','VERIFYING','UNKNOWN_REQUIRES_REVIEW')
                   ORDER BY created_at ASC, execution_id ASC"""
            ).fetchall()
        ids = tuple(str(row["execution_id"]) for row in rows)
        return StartupRecoveryReport(
            aborted_collection_batches=aborted,
            job_recovery=job_recovery,
            unresolved_execution_count=len(ids),
            unresolved_execution_ids=ids,
        )
