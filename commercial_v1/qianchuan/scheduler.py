"""10 分钟“监控计划活跃状态检查”调度器。

它只服务 WATCHING 计划；ACTIVE_COLLECTING 计划不继续跑 10 分钟检查。
Scheduler 每次只为一个目标保留一个 QUEUED/RUNNING Job。进程重启后，历史 RUNNING
状态检查可以被恢复层 ABORT，Scheduler 只重新生成“当前检查”，不补历史周期。
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from commercial_v1.runtime.jobs import ClaimedJob
from commercial_v1.security.redaction import sanitize_text
from commercial_v1.storage.database import Database
from commercial_v1.storage.writer import StorageWriter

from .plans import MonitorPlanStore, PlanMonitorService

PLAN_STATUS_CHECK = "PLAN_STATUS_CHECK"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


class PlanStateCheckHandler:
    def __init__(
        self,
        database: Database,
        writer: StorageWriter,
        store: MonitorPlanStore,
        monitor: PlanMonitorService,
        *,
        retry_minutes: int = 2,
    ) -> None:
        self._database = database
        self._writer = writer
        self._store = store
        self._monitor = monitor
        self._retry_minutes = max(1, int(retry_minutes))

    def __call__(self, job: ClaimedJob) -> Mapping[str, Any]:
        target_uid = str(job.payload.get("target_uid") or "").strip()
        if not target_uid:
            raise ValueError("PLAN_STATUS_CHECK payload missing target_uid")
        target = self._store.get_target(target_uid)
        if not int(target["monitor_enabled"]):
            return {"target_uid": target_uid, "skipped": "MONITOR_DISABLED"}
        if str(target["lifecycle_state"]) == "TERMINAL":
            return {"target_uid": target_uid, "skipped": "TERMINAL"}
        if str(target["lifecycle_state"]) != "WATCHING":
            return {
                "target_uid": target_uid,
                "skipped": f"STATE_{target['lifecycle_state']}",
            }

        try:
            plan = self._monitor.check_active_state(target_uid)
        except BaseException as exc:
            self._defer_after_error(target_uid, exc)
            raise

        refreshed = self._store.get_target(target_uid)
        return {
            "target_uid": target_uid,
            "official_status": plan.official_status,
            "lifecycle_state": refreshed["lifecycle_state"],
            "collection_active": bool(refreshed["collection_active"]),
            "next_status_check_at": refreshed["next_status_check_at"],
            "next_hot_collect_at": refreshed["next_hot_collect_at"],
        }

    def _defer_after_error(self, target_uid: str, exc: BaseException) -> None:
        now_dt = _utc_now()
        now = _iso(now_dt)
        next_check = _iso(now_dt + timedelta(minutes=self._retry_minutes))
        message = sanitize_text(f"{type(exc).__name__}: {exc}")[:1000]
        self._writer.execute(
            """UPDATE monitor_plan SET lifecycle_state='WATCHING',collection_active=0,
               strategy_eligible=0,write_eligible=0,sync_state='STATUS_CHECK_ERROR',
               last_status_check_at=?,next_status_check_at=?,next_hot_collect_at=NULL,
               updated_at=? WHERE target_uid=? AND monitor_enabled=1""",
            (now, next_check, now, target_uid),
        ).result(timeout=5)

        # 错误详情写通用 error event，官方状态列仍保持服务器最后一次可信事实。
        try:
            blocked = json.dumps(
                [
                    "EVALUATE_RETARGET",
                    "EVALUATE_STOP",
                    "CREATE_RETARGET",
                    "PAUSE_CONTROL",
                    "UPDATE_BUDGET",
                    "UPDATE_DURATION",
                ],
                separators=(",", ":"),
            )
            self._writer.execute(
                """INSERT INTO api_error_event(
                   error_id,module,error_scope,advertiser_id,ad_id,endpoint,http_status,api_code,
                   request_id,error_type,message,retryable,blocked_capabilities_json,occurred_at,resolved_at
                   )
                   SELECT ?,'qianchuan.plan_state',?,advertiser_id,ad_id,'PLAN_STATUS_CHECK',NULL,?,
                          NULL,?,?,?, ?,?,NULL
                   FROM monitor_plan WHERE target_uid=?""",
                (
                    str(uuid.uuid4()),
                    f"PLAN:{target_uid}",
                    str(getattr(exc, "code", "") or ""),
                    type(exc).__name__,
                    message,
                    1 if bool(getattr(exc, "retryable", False)) else 0,
                    blocked,
                    now,
                    target_uid,
                ),
            ).result(timeout=5)
        except Exception:
            # 诊断写失败不能改变“停止该计划自动能力”的主结果。
            pass


class PlanStateScheduler:
    def __init__(
        self,
        database: Database,
        writer: StorageWriter,
        *,
        interval_seconds: float = 15.0,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._database = database
        self._writer = writer
        self._interval = float(interval_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_error: str | None = None
        self._enqueued = 0
        self._last_scan_at: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="plan-state-scheduler",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(2.0, self._interval * 2))

    def restart(self) -> None:
        self.stop()
        self._thread = None
        self.start()

    def run_once(self) -> int:
        now = _iso(_utc_now())

        def work(conn):
            due = conn.execute(
                """SELECT target_uid FROM monitor_plan
                   WHERE monitor_enabled=1 AND lifecycle_state='WATCHING'
                     AND next_status_check_at IS NOT NULL AND next_status_check_at<=?
                   ORDER BY next_status_check_at ASC,target_uid ASC""",
                (now,),
            ).fetchall()
            inserted = 0
            for row in due:
                target_uid = str(row["target_uid"])
                payload_json = json.dumps(
                    {"target_uid": target_uid},
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                active = conn.execute(
                    """SELECT 1 FROM background_job
                       WHERE job_type=? AND payload_json=? AND status IN('QUEUED','RUNNING')
                       LIMIT 1""",
                    (PLAN_STATUS_CHECK, payload_json),
                ).fetchone()
                if active is not None:
                    continue
                uid = str(uuid.uuid4())
                conn.execute(
                    """INSERT INTO background_job(
                       job_uid,job_type,priority,payload_json,status,due_at,created_at,updated_at
                       ) VALUES(?,?,50,?,'QUEUED',?,?,?)""",
                    (uid, PLAN_STATUS_CHECK, payload_json, now, now, now),
                )
                inserted += 1
            return inserted

        inserted = int(self._writer.transaction(work).result(timeout=5))
        self._enqueued += inserted
        self._last_scan_at = now
        self._last_error = None
        return inserted

    def health_snapshot(self) -> dict[str, Any]:
        return {
            "alive": bool(self._thread and self._thread.is_alive()),
            "enqueued": self._enqueued,
            "last_scan_at": self._last_scan_at,
            "last_error": self._last_error,
        }

    def _run(self) -> None:
        # 启动立即扫一次，恢复 WATCHING 计划，不等待一个 interval。
        while not self._stop.is_set():
            try:
                self.run_once()
            except BaseException as exc:
                self._last_error = sanitize_text(f"{type(exc).__name__}: {exc}")[:1000]
            if self._stop.wait(self._interval):
                return
