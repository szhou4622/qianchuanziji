"""Phase 3 可信 5 分钟素材/调控任务采集。

网络请求与 SQLite 写事务严格分离。任何分页/规范化异常都不得覆盖上一轮可信 Latest。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from commercial_v1.security.redaction import sanitize_text
from commercial_v1.storage.database import Database
from commercial_v1.storage.writer import StorageWriter

from .client import OpenApiClient
from .contracts import CONTROL_TASK_LIST, MATERIAL_GET
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

CHINA_TZ = timezone(timedelta(hours=8))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def fixed_five_minute_slot(value: datetime | None = None) -> str:
    current = (value or utc_now()).astimezone(timezone.utc)
    minute = current.minute - current.minute % 5
    return iso(current.replace(minute=minute, second=0, microsecond=0))


def _local_business_window(value: datetime | None = None) -> tuple[str, str, str]:
    current = (value or utc_now()).astimezone(CHINA_TZ)
    date_text = current.strftime("%Y-%m-%d")
    return date_text, f"{date_text} 00:00:00", current.strftime("%Y-%m-%d %H:%M:%S")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


def _fingerprint(records: Sequence[Any]) -> str:
    payload = [asdict(record) for record in records]
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CollectionResult:
    batch_id: str
    pipeline_type: str
    status: str
    row_count: int
    suspicious_empty: bool
    missing_count: int
    request_ids: tuple[str, ...]


class HotCollectionService:
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

    def collect_materials(
        self,
        target_uid: str,
        *,
        scheduled_at: str | None = None,
        now: datetime | None = None,
    ) -> CollectionResult:
        target = self._load_active_target(target_uid)
        batch_id = self._start_batch(target, "MATERIAL_5M", scheduled_at or fixed_five_minute_slot(now))
        business_date, _, _ = _local_business_window(now)
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
                        "material_status": "DELIVERY_OK",
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
            normalized = tuple(
                record
                for record in (
                    normalize_material_hot(row, advertiser_id=aid, ad_id=ad_id)
                    for row in rows
                )
                if record.official_material_status == "DELIVERY_OK"
            )
            return self._persist_material_batch(
                target,
                batch_id,
                scheduled_at or fixed_five_minute_slot(now),
                business_date,
                normalized,
                tuple(request_ids),
            )
        except BaseException as exc:
            self._fail_batch(batch_id, exc)
            raise

    def collect_controls(
        self,
        target_uid: str,
        *,
        scheduled_at: str | None = None,
        now: datetime | None = None,
    ) -> CollectionResult:
        target = self._load_active_target(target_uid)
        batch_id = self._start_batch(target, "CONTROL_5M", scheduled_at or fixed_five_minute_slot(now))
        business_date, start_time, end_time = _local_business_window(now)
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
                    "filtering": {"task_status": "PROCESSING"},
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
            normalized = tuple(
                record
                for record in (
                    normalize_control_task_hot(row, advertiser_id=aid, ad_id=ad_id)
                    for row in rows
                )
                if record.official_task_status == "PROCESSING"
            )
            return self._persist_control_batch(
                target,
                batch_id,
                scheduled_at or fixed_five_minute_slot(now),
                business_date,
                start_time,
                end_time,
                normalized,
                tuple(request_ids),
            )
        except BaseException as exc:
            self._fail_batch(batch_id, exc)
            raise

    def _load_active_target(self, target_uid: str) -> dict[str, Any]:
        with self._database.connect(readonly=True) as conn:
            row = conn.execute(
                """SELECT p.*,
                   a.enabled AS account_enabled,a.auth_status,
                   (SELECT aa.auth_profile_id FROM qianchuan_account_auth aa
                    WHERE aa.account_uid=p.account_uid AND aa.is_primary=1 LIMIT 1) AS auth_profile_id
                   FROM monitor_plan p
                   JOIN qianchuan_account a ON a.account_uid=p.account_uid
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
        if str(target["official_status"]) != "DELIVERY_OK":
            raise ValueError("计划当前不是 DELIVERY_OK")
        if str(target["lifecycle_state"]) != "ACTIVE_COLLECTING" or not int(target["collection_active"]):
            raise ValueError("计划当前不具备 5 分钟热采集资格")
        if not str(target.get("auth_profile_id") or ""):
            raise ValueError("计划缺少主授权")
        return target

    def _start_batch(self, target: Mapping[str, Any], pipeline_type: str, scheduled_at: str) -> str:
        batch_id = str(uuid.uuid4())
        now = iso(utc_now())
        self._writer.execute(
            """INSERT INTO collection_batch(
               batch_id,account_uid,target_uid,advertiser_id,ad_id,pipeline_type,
               scheduled_at,started_at,status,created_at
               ) VALUES(?,?,?,?,?,?,?,?,'RUNNING',?)""",
            (
                batch_id,
                target["account_uid"],
                target["target_uid"],
                target["advertiser_id"],
                target["ad_id"],
                pipeline_type,
                scheduled_at,
                now,
                now,
            ),
        ).result(timeout=5)
        return batch_id

    def _fail_batch(self, batch_id: str, exc: BaseException) -> None:
        now = iso(utc_now())
        code = str(getattr(exc, "code", "") or "")
        message = sanitize_text(f"{type(exc).__name__}: {exc}")[:2000]
        self._writer.execute(
            """UPDATE collection_batch SET status='FAILED',finished_at=?,error_type=?,
               error_code=?,error_message=? WHERE batch_id=? AND status='RUNNING'""",
            (now, type(exc).__name__, code, message, batch_id),
        ).result(timeout=5)

    @staticmethod
    def _material_uid(aid: str, ad_id: str, material_id: str) -> str:
        return f"material:{aid}:{ad_id}:{material_id}"

    @staticmethod
    def _control_uid(aid: str, ad_id: str, task_id: str) -> str:
        return f"control:{aid}:{ad_id}:{task_id}"

    def _persist_material_batch(
        self,
        target: Mapping[str, Any],
        batch_id: str,
        scheduled_at: str,
        business_date: str,
        records: tuple[MaterialHotRecord, ...],
        request_ids: tuple[str, ...],
    ) -> CollectionResult:
        now = iso(utc_now())
        aid = str(target["advertiser_id"])
        ad_id = str(target["ad_id"])
        current_ids = {record.material_id for record in records}
        fingerprint = _fingerprint(records)

        def work(conn):
            previous_rows = conn.execute(
                """SELECT * FROM material_latest
                   WHERE advertiser_id=? AND ad_id=? AND official_material_status='DELIVERY_OK'""",
                (aid, ad_id),
            ).fetchall()
            previous = {str(row["material_id"]): dict(row) for row in previous_rows}
            trusted_previous_count = sum(
                1 for row in previous.values() if str(row.get("sync_state")) == "TRUSTED"
            )

            if trusted_previous_count > 0 and not records:
                conn.execute(
                    """UPDATE material_latest SET sync_state='SUSPICIOUS_EMPTY',strategy_eligible=0,
                       updated_at=? WHERE advertiser_id=? AND ad_id=?
                       AND official_material_status='DELIVERY_OK'""",
                    (now, aid, ad_id),
                )
                conn.execute(
                    """UPDATE collection_batch SET status='SUSPICIOUS_EMPTY',finished_at=?,
                       successful_pages=?,raw_row_count=0,unique_row_count=0,request_ids_json=?,
                       response_fingerprint=? WHERE batch_id=?""",
                    (now, max(1, len(request_ids)), _json(request_ids), fingerprint, batch_id),
                )
                return CollectionResult(batch_id, "MATERIAL_5M", "SUSPICIOUS_EMPTY", 0, True, 0, request_ids)

            missing_ids = set(previous) - current_ids
            if records and missing_ids:
                placeholders = ",".join("?" for _ in missing_ids)
                conn.execute(
                    f"""UPDATE material_latest SET sync_state='MISSING_REQUIRES_CONFIRMATION',
                       strategy_eligible=0,updated_at=? WHERE advertiser_id=? AND ad_id=?
                       AND material_id IN ({placeholders})""",
                    (now, aid, ad_id, *sorted(missing_ids)),
                )

            for record in records:
                material_uid = self._material_uid(aid, ad_id, record.material_id)
                old = previous.get(record.material_id)
                recovered = bool(old and str(old.get("sync_state")) != "TRUSTED")
                changed = old is None or any(
                    old.get(column) != value
                    for column, value in (
                        ("official_material_status", record.official_material_status),
                        ("official_audit_status", record.official_audit_status),
                        ("overall_cost_decimal", record.overall_cost),
                        ("net_settle_amount_decimal", record.net_settle_amount),
                        ("net_settle_roi_decimal", record.net_settle_roi),
                        ("net_settle_order_count", record.net_settle_order_count),
                        ("overall_order_count", record.overall_order_count),
                        ("overall_gmv_decimal", record.overall_gmv),
                        ("overall_pay_roi_decimal", record.overall_pay_roi),
                    )
                )
                conn.execute(
                    """INSERT INTO material_registry(
                       material_uid,advertiser_id,ad_id,material_id,video_id,title,first_seen_at,last_seen_at,
                       first_active_at,last_active_at,last_official_status,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(material_uid) DO UPDATE SET
                         video_id=excluded.video_id,title=excluded.title,last_seen_at=excluded.last_seen_at,
                         last_active_at=excluded.last_active_at,last_official_status=excluded.last_official_status,
                         updated_at=excluded.updated_at""",
                    (
                        material_uid, aid, ad_id, record.material_id, record.video_id, record.title,
                        now, now, now, now, record.official_material_status, now, now,
                    ),
                )
                conn.execute(
                    """INSERT INTO material_latest(
                       material_uid,advertiser_id,ad_id,material_id,official_material_status,official_audit_status,
                       overall_cost_decimal,net_settle_amount_decimal,net_settle_roi_decimal,net_settle_order_count,
                       overall_order_count,overall_gmv_decimal,overall_pay_roi_decimal,stat_date,collected_at,batch_id,
                       request_id,sync_state,strategy_eligible,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'TRUSTED',1,?)
                       ON CONFLICT(material_uid) DO UPDATE SET
                         official_material_status=excluded.official_material_status,
                         official_audit_status=excluded.official_audit_status,
                         overall_cost_decimal=excluded.overall_cost_decimal,
                         net_settle_amount_decimal=excluded.net_settle_amount_decimal,
                         net_settle_roi_decimal=excluded.net_settle_roi_decimal,
                         net_settle_order_count=excluded.net_settle_order_count,
                         overall_order_count=excluded.overall_order_count,
                         overall_gmv_decimal=excluded.overall_gmv_decimal,
                         overall_pay_roi_decimal=excluded.overall_pay_roi_decimal,
                         stat_date=excluded.stat_date,collected_at=excluded.collected_at,batch_id=excluded.batch_id,
                         request_id=excluded.request_id,sync_state='TRUSTED',strategy_eligible=1,updated_at=excluded.updated_at""",
                    (
                        material_uid, aid, ad_id, record.material_id, record.official_material_status,
                        record.official_audit_status, record.overall_cost, record.net_settle_amount,
                        record.net_settle_roi, record.net_settle_order_count, record.overall_order_count,
                        record.overall_gmv, record.overall_pay_roi, business_date, now, batch_id,
                        request_ids[-1] if request_ids else None, now,
                    ),
                )
                reason = "FIRST_SEEN" if old is None else ("RECOVERED_FROM_UNTRUSTED" if recovered else "METRIC_OR_STATUS_CHANGED")
                if changed or recovered:
                    conn.execute(
                        """INSERT OR IGNORE INTO material_5m(
                           snapshot_id,material_uid,advertiser_id,ad_id,material_id,scheduled_at,collected_at,stat_date,
                           overall_cost_decimal,net_settle_amount_decimal,net_settle_roi_decimal,net_settle_order_count,
                           overall_order_count,overall_gmv_decimal,overall_pay_roi_decimal,official_material_status,
                           official_audit_status,batch_id,snapshot_reason
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            str(uuid.uuid4()), material_uid, aid, ad_id, record.material_id, scheduled_at, now,
                            business_date, record.overall_cost, record.net_settle_amount, record.net_settle_roi,
                            record.net_settle_order_count, record.overall_order_count, record.overall_gmv,
                            record.overall_pay_roi, record.official_material_status, record.official_audit_status,
                            batch_id, reason,
                        ),
                    )

            conn.execute(
                """UPDATE collection_batch SET status='SUCCESS',finished_at=?,successful_pages=?,
                   raw_row_count=?,unique_row_count=?,request_ids_json=?,response_fingerprint=? WHERE batch_id=?""",
                (now, max(1, len(request_ids)), len(records), len(records), _json(request_ids), fingerprint, batch_id),
            )
            return CollectionResult(batch_id, "MATERIAL_5M", "SUCCESS", len(records), False, len(missing_ids), request_ids)

        return self._writer.transaction(work).result(timeout=15)

    def _persist_control_batch(
        self,
        target: Mapping[str, Any],
        batch_id: str,
        scheduled_at: str,
        business_date: str,
        stat_start_time: str,
        stat_end_time: str,
        records: tuple[ControlTaskHotRecord, ...],
        request_ids: tuple[str, ...],
    ) -> CollectionResult:
        now = iso(utc_now())
        aid = str(target["advertiser_id"])
        ad_id = str(target["ad_id"])
        current_ids = {record.control_task_id for record in records}
        fingerprint = _fingerprint(records)

        def work(conn):
            previous_rows = conn.execute(
                """SELECT * FROM control_task_latest
                   WHERE advertiser_id=? AND ad_id=? AND official_task_status='PROCESSING'""",
                (aid, ad_id),
            ).fetchall()
            previous = {str(row["control_task_id"]): dict(row) for row in previous_rows}
            trusted_previous_count = sum(
                1 for row in previous.values() if str(row.get("sync_state")) == "TRUSTED"
            )

            if trusted_previous_count > 0 and not records:
                conn.execute(
                    """UPDATE control_task_latest SET sync_state='SUSPICIOUS_EMPTY',strategy_eligible=0,
                       write_eligible=0,updated_at=? WHERE advertiser_id=? AND ad_id=?
                       AND official_task_status='PROCESSING'""",
                    (now, aid, ad_id),
                )
                conn.execute(
                    """UPDATE collection_batch SET status='SUSPICIOUS_EMPTY',finished_at=?,
                       successful_pages=?,raw_row_count=0,unique_row_count=0,request_ids_json=?,
                       response_fingerprint=? WHERE batch_id=?""",
                    (now, max(1, len(request_ids)), _json(request_ids), fingerprint, batch_id),
                )
                return CollectionResult(batch_id, "CONTROL_5M", "SUSPICIOUS_EMPTY", 0, True, 0, request_ids)

            missing_ids = set(previous) - current_ids
            if records and missing_ids:
                placeholders = ",".join("?" for _ in missing_ids)
                conn.execute(
                    f"""UPDATE control_task_latest SET sync_state='MISSING_REQUIRES_CONFIRMATION',
                       strategy_eligible=0,write_eligible=0,updated_at=? WHERE advertiser_id=? AND ad_id=?
                       AND control_task_id IN ({placeholders})""",
                    (now, aid, ad_id, *sorted(missing_ids)),
                )

            for record in records:
                task_uid = self._control_uid(aid, ad_id, record.control_task_id)
                old = previous.get(record.control_task_id)
                recovered = bool(old and str(old.get("sync_state")) != "TRUSTED")
                changed = old is None or any(
                    old.get(column) != value
                    for column, value in (
                        ("official_task_status", record.official_task_status),
                        ("assist_cost_decimal", record.assist_cost),
                        ("assist_order_count", record.assist_order_count),
                        ("assist_gmv_decimal", record.assist_gmv),
                        ("assist_pay_roi_decimal", record.assist_pay_roi),
                        ("assist_net_amount_decimal", record.assist_net_amount),
                        ("assist_net_roi_decimal", record.assist_net_roi),
                        ("assist_net_order_count", record.assist_net_order_count),
                    )
                )
                conn.execute(
                    """INSERT INTO control_task_registry(
                       control_task_uid,advertiser_id,ad_id,control_task_id,scene,task_name,create_time,
                       first_seen_at,last_seen_at,first_processing_at,last_official_status,material_count,
                       created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(control_task_uid) DO UPDATE SET
                         scene=excluded.scene,task_name=excluded.task_name,last_seen_at=excluded.last_seen_at,
                         last_official_status=excluded.last_official_status,material_count=excluded.material_count,
                         updated_at=excluded.updated_at""",
                    (
                        task_uid, aid, ad_id, record.control_task_id, record.scene, record.task_name,
                        record.create_time or None, now, now, now, record.official_task_status,
                        len(record.material_ids), now, now,
                    ),
                )
                conn.execute("DELETE FROM control_task_material WHERE control_task_uid=?", (task_uid,))
                for material_id in record.material_ids:
                    material_uid = self._material_uid(aid, ad_id, material_id)
                    exists = conn.execute(
                        "SELECT 1 FROM material_registry WHERE material_uid=?", (material_uid,)
                    ).fetchone()
                    conn.execute(
                        """INSERT INTO control_task_material(control_task_uid,material_uid,material_id,observed_at)
                           VALUES(?,?,?,?)""",
                        (task_uid, material_uid if exists else None, material_id, now),
                    )
                conn.execute(
                    """INSERT INTO control_task_latest(
                       control_task_uid,advertiser_id,ad_id,control_task_id,official_task_status,budget_decimal,
                       duration_decimal,bid_decimal,roi_goal_decimal,assist_cost_decimal,assist_order_count,
                       assist_gmv_decimal,assist_pay_roi_decimal,assist_net_amount_decimal,assist_net_roi_decimal,
                       assist_net_order_count,stat_start_time,stat_end_time,stat_date,collected_at,batch_id,request_id,
                       sync_state,strategy_eligible,write_eligible,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'TRUSTED',1,1,?)
                       ON CONFLICT(control_task_uid) DO UPDATE SET
                         official_task_status=excluded.official_task_status,budget_decimal=excluded.budget_decimal,
                         duration_decimal=excluded.duration_decimal,bid_decimal=excluded.bid_decimal,
                         roi_goal_decimal=excluded.roi_goal_decimal,assist_cost_decimal=excluded.assist_cost_decimal,
                         assist_order_count=excluded.assist_order_count,assist_gmv_decimal=excluded.assist_gmv_decimal,
                         assist_pay_roi_decimal=excluded.assist_pay_roi_decimal,
                         assist_net_amount_decimal=excluded.assist_net_amount_decimal,
                         assist_net_roi_decimal=excluded.assist_net_roi_decimal,
                         assist_net_order_count=excluded.assist_net_order_count,
                         stat_start_time=excluded.stat_start_time,stat_end_time=excluded.stat_end_time,
                         stat_date=excluded.stat_date,collected_at=excluded.collected_at,batch_id=excluded.batch_id,
                         request_id=excluded.request_id,sync_state='TRUSTED',strategy_eligible=1,write_eligible=1,
                         updated_at=excluded.updated_at""",
                    (
                        task_uid, aid, ad_id, record.control_task_id, record.official_task_status,
                        record.budget, record.duration_decimal, record.bid, record.roi_goal,
                        record.assist_cost, record.assist_order_count, record.assist_gmv, record.assist_pay_roi,
                        record.assist_net_amount, record.assist_net_roi, record.assist_net_order_count,
                        stat_start_time, stat_end_time, business_date, now, batch_id,
                        request_ids[-1] if request_ids else None, now,
                    ),
                )
                reason = "FIRST_SEEN" if old is None else ("RECOVERED_FROM_UNTRUSTED" if recovered else "METRIC_OR_STATUS_CHANGED")
                if changed or recovered:
                    conn.execute(
                        """INSERT OR IGNORE INTO control_task_5m(
                           snapshot_id,control_task_uid,advertiser_id,ad_id,control_task_id,scheduled_at,
                           stat_start_time,stat_end_time,stat_date,assist_cost_decimal,assist_order_count,
                           assist_gmv_decimal,assist_pay_roi_decimal,assist_net_amount_decimal,assist_net_roi_decimal,
                           assist_net_order_count,official_task_status,batch_id,snapshot_reason
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            str(uuid.uuid4()), task_uid, aid, ad_id, record.control_task_id, scheduled_at,
                            stat_start_time, stat_end_time, business_date, record.assist_cost,
                            record.assist_order_count, record.assist_gmv, record.assist_pay_roi,
                            record.assist_net_amount, record.assist_net_roi, record.assist_net_order_count,
                            record.official_task_status, batch_id, reason,
                        ),
                    )

            conn.execute(
                """UPDATE collection_batch SET status='SUCCESS',finished_at=?,successful_pages=?,
                   raw_row_count=?,unique_row_count=?,request_ids_json=?,response_fingerprint=? WHERE batch_id=?""",
                (now, max(1, len(request_ids)), len(records), len(records), _json(request_ids), fingerprint, batch_id),
            )
            return CollectionResult(batch_id, "CONTROL_5M", "SUCCESS", len(records), False, len(missing_ids), request_ids)

        return self._writer.transaction(work).result(timeout=15)
