"""Phase 3 异常空集/对象缺失的证据确认。

热采集只看活动对象，因此“本轮没返回”不能等价为“对象已结束”。本模块用一次短延迟、
不带活动状态过滤的官方完整读取核验。只有明确读到官方非活动状态，才更新为已确认终态；
仍然找不到的对象继续保持不可用于策略，绝不猜状态。
"""
from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from commercial_v1.runtime.jobs import ClaimedJob
from commercial_v1.security.redaction import sanitize_text
from commercial_v1.storage.database import Database
from commercial_v1.storage.writer import StorageWriter

from .client import OpenApiClient
from .contracts import CONTROL_TASK_LIST, MATERIAL_GET
from .hot_collection import CHINA_TZ, iso, utc_now
from .hot_models import (
    CONTROL_METRIC_FIELDS,
    MATERIAL_METRIC_FIELDS,
    ControlTaskHotRecord,
    MaterialHotRecord,
    control_task_identity,
    material_identity,
    normalize_control_task_hot,
    normalize_material_hot,
)
from .pagination import get_all_pages
from .token_provider import OAuthTokenProvider

MATERIAL_CONFIRM = "MATERIAL_CONFIRM"
CONTROL_CONFIRM = "CONTROL_CONFIRM"
BusinessAllowed = Callable[[], bool]


def _always_allowed() -> bool:
    return True


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


def _business_window(value: datetime | None = None) -> tuple[str, str, str]:
    current = (value or utc_now()).astimezone(CHINA_TZ)
    date_text = current.strftime("%Y-%m-%d")
    return date_text, f"{date_text} 00:00:00", current.strftime("%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True)
class ConfirmationResult:
    batch_id: str | None
    pipeline_type: str
    status: str
    pending_count: int
    confirmed_active: int
    confirmed_inactive: int
    unresolved: int
    request_ids: tuple[str, ...]


class HotConfirmationService:
    def __init__(
        self,
        database: Database,
        writer: StorageWriter,
        client: OpenApiClient,
        token_provider: OAuthTokenProvider,
    ) -> None:
        self._database = database
        self._writer = writer
        self._client = client
        self._tokens = token_provider

    def confirm_materials(self, target_uid: str, *, now: datetime | None = None) -> ConfirmationResult:
        target = self._load_target(target_uid)
        pending = self._pending_material_ids(target)
        if not pending:
            return ConfirmationResult(None, MATERIAL_CONFIRM, "NO_PENDING", 0, 0, 0, 0, ())
        batch_id = self._start_batch(target, MATERIAL_CONFIRM)
        business_date, _, _ = _business_window(now)
        aid = str(target["advertiser_id"])
        ad_id = str(target["ad_id"])
        auth_profile_id = str(target["auth_profile_id"])
        try:
            rows, request_ids = get_all_pages(
                self._client,
                MATERIAL_GET,
                query={
                    "advertiser_id": aid,
                    "ad_id": ad_id,
                    "filtering": {
                        "material_type": "VIDEO",
                        "start_date": business_date,
                        "end_date": business_date,
                        "material_select_type": "ALL",
                    },
                    "fields": list(MATERIAL_METRIC_FIELDS),
                },
                access_token=self._tokens.get_access_token(auth_profile_id),
                advertiser_id=aid,
                page_size=100,
                identity_getter=material_identity,
                refresh_access_token=lambda: self._tokens.get_access_token(
                    auth_profile_id, force_refresh=True
                ),
            )
            records = {
                record.material_id: record
                for record in (
                    normalize_material_hot(row, advertiser_id=aid, ad_id=ad_id) for row in rows
                )
            }
            return self._persist_material_confirmation(
                target,
                batch_id,
                business_date,
                pending,
                records,
                tuple(request_ids),
            )
        except BaseException as exc:
            self._fail_batch(batch_id, exc)
            raise

    def confirm_controls(self, target_uid: str, *, now: datetime | None = None) -> ConfirmationResult:
        target = self._load_target(target_uid)
        pending = self._pending_control_ids(target)
        if not pending:
            return ConfirmationResult(None, CONTROL_CONFIRM, "NO_PENDING", 0, 0, 0, 0, ())
        batch_id = self._start_batch(target, CONTROL_CONFIRM)
        business_date, start_time, end_time = _business_window(now)
        aid = str(target["advertiser_id"])
        ad_id = str(target["ad_id"])
        auth_profile_id = str(target["auth_profile_id"])
        try:
            rows, request_ids = get_all_pages(
                self._client,
                CONTROL_TASK_LIST,
                query={
                    "advertiser_id": aid,
                    "ad_id": ad_id,
                    "marketing_goal": str(target["promotion_scene"]),
                    "start_time": start_time,
                    "end_time": end_time,
                    "scene": "MATERIAL_ADD_BUDGET",
                    "fields": list(CONTROL_METRIC_FIELDS),
                },
                access_token=self._tokens.get_access_token(auth_profile_id),
                advertiser_id=aid,
                page_size=100,
                identity_getter=control_task_identity,
                refresh_access_token=lambda: self._tokens.get_access_token(
                    auth_profile_id, force_refresh=True
                ),
            )
            records = {
                record.control_task_id: record
                for record in (
                    normalize_control_task_hot(row, advertiser_id=aid, ad_id=ad_id) for row in rows
                )
            }
            return self._persist_control_confirmation(
                target,
                batch_id,
                business_date,
                start_time,
                end_time,
                pending,
                records,
                tuple(request_ids),
            )
        except BaseException as exc:
            self._fail_batch(batch_id, exc)
            raise

    def _load_target(self, target_uid: str) -> dict[str, Any]:
        with self._database.connect(readonly=True) as conn:
            row = conn.execute(
                """SELECT p.*,a.enabled AS account_enabled,a.auth_status,
                   (SELECT aa.auth_profile_id FROM qianchuan_account_auth aa
                    WHERE aa.account_uid=p.account_uid AND aa.is_primary=1 LIMIT 1) AS auth_profile_id
                   FROM monitor_plan p JOIN qianchuan_account a ON a.account_uid=p.account_uid
                   WHERE p.target_uid=?""",
                (target_uid,),
            ).fetchone()
        if row is None:
            raise ValueError("监控计划不存在")
        target = dict(row)
        if not int(target["monitor_enabled"]):
            raise ValueError("计划监控已关闭")
        if not int(target["account_enabled"]):
            raise ValueError("千川账户已停用")
        if str(target["auth_status"]) != "ACTIVE":
            raise ValueError("千川账户授权不可用")
        if not str(target.get("auth_profile_id") or ""):
            raise ValueError("计划缺少主授权")
        return target

    def _pending_material_ids(self, target: Mapping[str, Any]) -> tuple[str, ...]:
        with self._database.connect(readonly=True) as conn:
            rows = conn.execute(
                """SELECT material_id FROM material_latest
                   WHERE advertiser_id=? AND ad_id=?
                     AND sync_state IN('SUSPICIOUS_EMPTY','MISSING_REQUIRES_CONFIRMATION')
                   ORDER BY material_id""",
                (target["advertiser_id"], target["ad_id"]),
            ).fetchall()
        return tuple(str(row["material_id"]) for row in rows)

    def _pending_control_ids(self, target: Mapping[str, Any]) -> tuple[str, ...]:
        with self._database.connect(readonly=True) as conn:
            rows = conn.execute(
                """SELECT control_task_id FROM control_task_latest
                   WHERE advertiser_id=? AND ad_id=?
                     AND sync_state IN('SUSPICIOUS_EMPTY','MISSING_REQUIRES_CONFIRMATION')
                   ORDER BY control_task_id""",
                (target["advertiser_id"], target["ad_id"]),
            ).fetchall()
        return tuple(str(row["control_task_id"]) for row in rows)

    def _start_batch(self, target: Mapping[str, Any], pipeline_type: str) -> str:
        batch_id = str(uuid.uuid4())
        now = iso(utc_now())
        self._writer.execute(
            """INSERT INTO collection_batch(
               batch_id,account_uid,target_uid,advertiser_id,ad_id,pipeline_type,
               scheduled_at,started_at,status,created_at
               ) VALUES(?,?,?,?,?,?,?,?,'RUNNING',?)""",
            (
                batch_id,target["account_uid"],target["target_uid"],target["advertiser_id"],
                target["ad_id"],pipeline_type,now,now,now,
            ),
        ).result(timeout=5)
        return batch_id

    def _fail_batch(self, batch_id: str, exc: BaseException) -> None:
        now = iso(utc_now())
        self._writer.execute(
            """UPDATE collection_batch SET status='FAILED',finished_at=?,error_type=?,error_code=?,
               error_message=? WHERE batch_id=? AND status='RUNNING'""",
            (
                now,type(exc).__name__,str(getattr(exc,"code","") or ""),
                sanitize_text(f"{type(exc).__name__}: {exc}")[:2000],batch_id,
            ),
        ).result(timeout=5)

    def _persist_material_confirmation(
        self,
        target: Mapping[str, Any],
        batch_id: str,
        business_date: str,
        pending: tuple[str, ...],
        records: Mapping[str, MaterialHotRecord],
        request_ids: tuple[str, ...],
    ) -> ConfirmationResult:
        now = iso(utc_now())
        aid, ad_id = str(target["advertiser_id"]), str(target["ad_id"])

        def work(conn):
            active = inactive = unresolved = 0
            request_id = request_ids[-1] if request_ids else None
            for material_id in pending:
                current = conn.execute(
                    """SELECT sync_state FROM material_latest WHERE advertiser_id=? AND ad_id=? AND material_id=?""",
                    (aid,ad_id,material_id),
                ).fetchone()
                if current is None or str(current["sync_state"]) not in {
                    "SUSPICIOUS_EMPTY","MISSING_REQUIRES_CONFIRMATION"
                }:
                    continue
                record = records.get(material_id)
                if record is None:
                    # 找不到仍不是终态证据。只把“可疑空集”收敛为“对象缺失待确认”，
                    # 不改官方状态、不改最后可信指标，也不刷新 updated_at，防止无穷确认循环。
                    conn.execute(
                        """UPDATE material_latest SET sync_state='MISSING_REQUIRES_CONFIRMATION',
                           strategy_eligible=0 WHERE advertiser_id=? AND ad_id=? AND material_id=?""",
                        (aid,ad_id,material_id),
                    )
                    unresolved += 1
                    continue
                is_active = record.official_material_status == "DELIVERY_OK"
                sync_state = "TRUSTED" if is_active else "TRUSTED_FINAL_STATE"
                eligible = 1 if is_active else 0
                conn.execute(
                    """UPDATE material_latest SET official_material_status=?,official_audit_status=?,
                       overall_cost_decimal=?,net_settle_amount_decimal=?,net_settle_roi_decimal=?,
                       net_settle_order_count=?,overall_order_count=?,overall_gmv_decimal=?,overall_pay_roi_decimal=?,
                       stat_date=?,collected_at=?,batch_id=?,request_id=?,sync_state=?,strategy_eligible=?,updated_at=?
                       WHERE advertiser_id=? AND ad_id=? AND material_id=?""",
                    (
                        record.official_material_status,record.official_audit_status,record.overall_cost,
                        record.net_settle_amount,record.net_settle_roi,record.net_settle_order_count,
                        record.overall_order_count,record.overall_gmv,record.overall_pay_roi,business_date,
                        now,batch_id,request_id,sync_state,eligible,now,aid,ad_id,material_id,
                    ),
                )
                conn.execute(
                    """UPDATE material_registry SET last_seen_at=?,last_official_status=?,
                       last_active_at=CASE WHEN ?=1 THEN ? ELSE last_active_at END,
                       ended_at=CASE WHEN ?=0 THEN ? ELSE NULL END,updated_at=?
                       WHERE advertiser_id=? AND ad_id=? AND material_id=?""",
                    (now,record.official_material_status,eligible,now,eligible,now,now,aid,ad_id,material_id),
                )
                if is_active:
                    active += 1
                else:
                    inactive += 1
            status = "CONFIRMED" if unresolved == 0 else "INCONCLUSIVE"
            conn.execute(
                """UPDATE collection_batch SET status=?,finished_at=?,successful_pages=?,raw_row_count=?,
                   unique_row_count=?,request_ids_json=? WHERE batch_id=?""",
                (status,now,max(1,len(request_ids)),len(records),len(records),_json(request_ids),batch_id),
            )
            return ConfirmationResult(batch_id,MATERIAL_CONFIRM,status,len(pending),active,inactive,unresolved,request_ids)

        return self._writer.transaction(work).result(timeout=15)

    def _persist_control_confirmation(
        self,
        target: Mapping[str, Any],
        batch_id: str,
        business_date: str,
        stat_start_time: str,
        stat_end_time: str,
        pending: tuple[str, ...],
        records: Mapping[str, ControlTaskHotRecord],
        request_ids: tuple[str, ...],
    ) -> ConfirmationResult:
        now = iso(utc_now())
        aid, ad_id = str(target["advertiser_id"]), str(target["ad_id"])

        def work(conn):
            active = inactive = unresolved = 0
            request_id = request_ids[-1] if request_ids else None
            for task_id in pending:
                current = conn.execute(
                    """SELECT control_task_uid,sync_state FROM control_task_latest
                       WHERE advertiser_id=? AND ad_id=? AND control_task_id=?""",
                    (aid,ad_id,task_id),
                ).fetchone()
                if current is None or str(current["sync_state"]) not in {
                    "SUSPICIOUS_EMPTY","MISSING_REQUIRES_CONFIRMATION"
                }:
                    continue
                record = records.get(task_id)
                if record is None:
                    conn.execute(
                        """UPDATE control_task_latest SET sync_state='MISSING_REQUIRES_CONFIRMATION',
                           strategy_eligible=0,write_eligible=0
                           WHERE advertiser_id=? AND ad_id=? AND control_task_id=?""",
                        (aid,ad_id,task_id),
                    )
                    unresolved += 1
                    continue
                is_active = record.official_task_status == "PROCESSING"
                sync_state = "TRUSTED" if is_active else "TRUSTED_FINAL_STATE"
                eligible = 1 if is_active else 0
                conn.execute(
                    """UPDATE control_task_latest SET official_task_status=?,budget_decimal=?,duration_decimal=?,
                       bid_decimal=?,roi_goal_decimal=?,assist_cost_decimal=?,assist_order_count=?,assist_gmv_decimal=?,
                       assist_pay_roi_decimal=?,assist_net_amount_decimal=?,assist_net_roi_decimal=?,
                       assist_net_order_count=?,stat_start_time=?,stat_end_time=?,stat_date=?,collected_at=?,batch_id=?,
                       request_id=?,sync_state=?,strategy_eligible=?,write_eligible=?,updated_at=?
                       WHERE advertiser_id=? AND ad_id=? AND control_task_id=?""",
                    (
                        record.official_task_status,record.budget,record.duration_decimal,record.bid,record.roi_goal,
                        record.assist_cost,record.assist_order_count,record.assist_gmv,record.assist_pay_roi,
                        record.assist_net_amount,record.assist_net_roi,record.assist_net_order_count,
                        stat_start_time,stat_end_time,business_date,now,batch_id,request_id,sync_state,
                        eligible,eligible,now,aid,ad_id,task_id,
                    ),
                )
                task_uid = str(current["control_task_uid"])
                conn.execute(
                    """UPDATE control_task_registry SET last_seen_at=?,last_official_status=?,
                       first_processing_at=CASE WHEN ?=1 THEN COALESCE(first_processing_at,?) ELSE first_processing_at END,
                       ended_at=CASE WHEN ?=0 THEN ? ELSE NULL END,material_count=?,updated_at=?
                       WHERE control_task_uid=?""",
                    (now,record.official_task_status,eligible,now,eligible,now,len(record.material_ids),now,task_uid),
                )
                conn.execute("DELETE FROM control_task_material WHERE control_task_uid=?",(task_uid,))
                for material_id in record.material_ids:
                    material_uid = f"material:{aid}:{ad_id}:{material_id}"
                    exists = conn.execute("SELECT 1 FROM material_registry WHERE material_uid=?",(material_uid,)).fetchone()
                    conn.execute(
                        "INSERT INTO control_task_material(control_task_uid,material_uid,material_id,observed_at) VALUES(?,?,?,?)",
                        (task_uid,material_uid if exists else None,material_id,now),
                    )
                if is_active:
                    active += 1
                else:
                    inactive += 1
            status = "CONFIRMED" if unresolved == 0 else "INCONCLUSIVE"
            conn.execute(
                """UPDATE collection_batch SET status=?,finished_at=?,successful_pages=?,raw_row_count=?,
                   unique_row_count=?,request_ids_json=? WHERE batch_id=?""",
                (status,now,max(1,len(request_ids)),len(records),len(records),_json(request_ids),batch_id),
            )
            return ConfirmationResult(batch_id,CONTROL_CONFIRM,status,len(pending),active,inactive,unresolved,request_ids)

        return self._writer.transaction(work).result(timeout=15)


class HotConfirmationHandler:
    def __init__(
        self,
        service: HotConfirmationService,
        pipeline_type: str,
        *,
        business_allowed: BusinessAllowed = _always_allowed,
    ) -> None:
        if pipeline_type not in {MATERIAL_CONFIRM, CONTROL_CONFIRM}:
            raise ValueError("unsupported confirmation pipeline")
        self._service = service
        self._pipeline = pipeline_type
        self._business_allowed = business_allowed

    def __call__(self, job: ClaimedJob) -> Mapping[str, Any]:
        target_uid = str(job.payload.get("target_uid") or "").strip()
        if not target_uid:
            raise ValueError("confirmation payload missing target_uid")
        if not self._business_allowed():
            return {"target_uid":target_uid,"pipeline_type":self._pipeline,"skipped":"LICENSE_BLOCKED"}
        result = (
            self._service.confirm_materials(target_uid)
            if self._pipeline == MATERIAL_CONFIRM
            else self._service.confirm_controls(target_uid)
        )
        return {
            "target_uid":target_uid,"pipeline_type":self._pipeline,"batch_id":result.batch_id,
            "status":result.status,"pending_count":result.pending_count,
            "confirmed_active":result.confirmed_active,"confirmed_inactive":result.confirmed_inactive,
            "unresolved":result.unresolved,
        }


class HotConfirmationScheduler:
    """为最新一次异常对象生成一次短延迟确认；确认无证据时不无限循环。"""

    def __init__(
        self,
        database: Database,
        writer: StorageWriter,
        *,
        delay_seconds: int = 60,
        interval_seconds: float = 10.0,
        business_allowed: BusinessAllowed = _always_allowed,
    ) -> None:
        self._database = database
        self._writer = writer
        self._delay = max(1,int(delay_seconds))
        self._interval = max(1.0,float(interval_seconds))
        self._business_allowed = business_allowed
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_error: str | None = None
        self._last_scan_at: str | None = None
        self._enqueued = 0
        self._license_blocked = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run,name="hot-confirmation-scheduler",daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(2.0,self._interval*2))

    def restart(self) -> None:
        self.stop(); self._thread=None; self.start()

    def run_once(self, *, now: datetime | None = None) -> int:
        current_dt = (now or utc_now()).astimezone(timezone.utc)
        current = iso(current_dt)
        self._last_scan_at = current
        if not self._business_allowed():
            self._license_blocked=True; self._last_error=None; return 0
        self._license_blocked=False

        def work(conn):
            candidates: list[tuple[str,str,str]] = []
            for table,pipeline in (("material_latest",MATERIAL_CONFIRM),("control_task_latest",CONTROL_CONFIRM)):
                rows = conn.execute(
                    f"""SELECT p.target_uid,MAX(x.updated_at) AS pending_at
                       FROM {table} x JOIN monitor_plan p
                         ON p.advertiser_id=x.advertiser_id AND p.ad_id=x.ad_id
                       WHERE p.monitor_enabled=1
                         AND x.sync_state IN('SUSPICIOUS_EMPTY','MISSING_REQUIRES_CONFIRMATION')
                       GROUP BY p.target_uid"""
                ).fetchall()
                candidates.extend((str(row["target_uid"]),pipeline,str(row["pending_at"])) for row in rows)
            inserted=0
            for target_uid,pipeline,pending_at in candidates:
                latest_confirm = conn.execute(
                    """SELECT MAX(finished_at) FROM collection_batch
                       WHERE target_uid=? AND pipeline_type=? AND status IN('CONFIRMED','INCONCLUSIVE')""",
                    (target_uid,pipeline),
                ).fetchone()[0]
                if latest_confirm and str(latest_confirm) >= pending_at:
                    continue
                payload_json=_json({"target_uid":target_uid})
                existing=conn.execute(
                    """SELECT 1 FROM background_job WHERE job_type=? AND payload_json=?
                       AND status IN('QUEUED','RUNNING') LIMIT 1""",
                    (pipeline,payload_json),
                ).fetchone()
                if existing is not None:
                    continue
                try:
                    pending_dt=datetime.fromisoformat(pending_at.replace("Z","+00:00")).astimezone(timezone.utc)
                except ValueError:
                    pending_dt=current_dt
                due=max(current_dt,pending_dt+timedelta(seconds=self._delay))
                conn.execute(
                    """INSERT INTO background_job(job_uid,job_type,priority,payload_json,status,due_at,created_at,updated_at)
                       VALUES(?,?,30,?,'QUEUED',?,?,?)""",
                    (str(uuid.uuid4()),pipeline,payload_json,iso(due),current,current),
                )
                inserted+=1
            return inserted

        inserted=int(self._writer.transaction(work).result(timeout=10))
        self._enqueued+=inserted; self._last_error=None
        return inserted

    def health_snapshot(self) -> dict[str,Any]:
        return {
            "alive":bool(self._thread and self._thread.is_alive()),"enqueued":self._enqueued,
            "last_scan_at":self._last_scan_at,"license_blocked":self._license_blocked,
            "last_error":self._last_error,
        }

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except BaseException as exc:
                self._last_error=sanitize_text(f"{type(exc).__name__}: {exc}")[:1000]
            if self._stop.wait(self._interval):
                return
