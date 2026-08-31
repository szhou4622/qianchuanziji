"""Phase 6 Execution 准备与只读 Preflight。

本模块刻意不包含任何千川业务 POST：
- APPROVED Candidate 先确定性冻结为 Execution Task；
- Preflight 只通过官方 GET 重新确认计划/素材/调控任务的服务器事实；
- 确定性业务事实已过期时只取消当前 Execution；
- 网络、Token、权限、响应契约等不确定错误不改成“业务失败”，Execution 保持 PENDING；
- 本阶段绝不创建 execution_attempt。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from commercial_v1.candidate import (
    APPROVED as CANDIDATE_APPROVED,
    CREATE_RETARGET,
    PAUSE_CONTROL,
    UPDATE_BUDGET,
    UPDATE_DURATION,
)
from commercial_v1.qianchuan.contracts import CONTROL_TASK_LIST, MATERIAL_GET
from commercial_v1.qianchuan.hot_models import (
    CONTROL_METRIC_FIELDS,
    MATERIAL_METRIC_FIELDS,
    control_task_identity,
    material_identity,
    normalize_control_task_hot,
    normalize_material_hot,
)
from commercial_v1.qianchuan.pagination import get_all_pages
from commercial_v1.storage.database import Database
from commercial_v1.storage.writer import StorageWriter

Clock = Callable[[], datetime]
CHINA_TZ = timezone(timedelta(hours=8))

PENDING = "PENDING"
EXECUTION_APPROVED = "APPROVED"
CANCELLED = "CANCELLED"

SUPPORTED_ACTIONS = frozenset(
    {CREATE_RETARGET, PAUSE_CONTROL, UPDATE_BUDGET, UPDATE_DURATION}
)
CONTROL_ACTIONS = frozenset({PAUSE_CONTROL, UPDATE_BUDGET, UPDATE_DURATION})


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


def _load_json(value: Any, *, label: str) -> Any:
    try:
        return json.loads(str(value or "{}"))
    except Exception as exc:
        raise ExecutionStateError(f"stored {label} is invalid JSON") from exc


def execution_id_for_candidate(candidate_id: str) -> str:
    raw = str(candidate_id or "").strip()
    if not raw:
        raise ValueError("candidate_id is required")
    return "execution_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ExecutionStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExecutionPreparation:
    execution_id: str
    candidate_id: str
    action_type: str
    status: str
    created: bool
    object_count: int


@dataclass(frozen=True)
class PreflightResult:
    execution_id: str
    status: str
    changed: bool
    reason: str | None
    request_ids: tuple[str, ...]


class ExecutionService:
    """把已经批准的 Candidate 幂等冻结为本地 Execution Task。"""

    def __init__(self, database: Database, writer: StorageWriter, *, clock: Clock = utc_now) -> None:
        self._database = database
        self._writer = writer
        self._clock = clock

    def prepare_from_candidate(self, candidate_id: str) -> ExecutionPreparation:
        cid = str(candidate_id or "").strip()
        if not cid:
            raise ValueError("candidate_id is required")

        with self._database.connect(readonly=True) as conn:
            candidate = conn.execute(
                "SELECT * FROM candidate_batch WHERE candidate_id=?", (cid,)
            ).fetchone()
            if candidate is None:
                raise ExecutionStateError("candidate does not exist")
            items = conn.execute(
                "SELECT * FROM candidate_item WHERE candidate_id=? ORDER BY object_uid,candidate_item_id",
                (cid,),
            ).fetchall()

        if str(candidate["status"]) != CANDIDATE_APPROVED:
            raise ExecutionStateError("only APPROVED candidate can create execution")
        action_type = str(candidate["action_type"] or "").upper()
        if action_type not in SUPPORTED_ACTIONS:
            raise ExecutionStateError(f"unsupported execution action: {action_type}")
        if not items:
            raise ExecutionStateError("approved candidate has no frozen items")

        frozen_items: list[dict[str, Any]] = []
        control_task_id: str | None = None
        if action_type == CREATE_RETARGET:
            for item in items:
                material_id = str(item["material_id"] or "").strip()
                if not material_id:
                    raise ExecutionStateError("CREATE_RETARGET candidate item is missing material_id")
                frozen_items.append(self._frozen_item(item))
        else:
            if len(items) != 1:
                raise ExecutionStateError("control action candidate must freeze exactly one control task")
            control_task_id = str(items[0]["control_task_id"] or "").strip()
            if not control_task_id:
                raise ExecutionStateError("control action candidate is missing control_task_id")
            frozen_items.append(self._frozen_item(items[0]))

        execution_params = _load_json(candidate["execution_params_json"], label="execution_params_json")
        if not isinstance(execution_params, Mapping):
            raise ExecutionStateError("execution_params_json must be an object")
        now = _iso(self._clock())
        execution_id = execution_id_for_candidate(cid)

        frozen_before: dict[str, Any] = {
            "candidate": {
                "candidate_id": cid,
                "strategy_id": candidate["strategy_id"],
                "strategy_version_id": candidate["strategy_version_id"],
                "group_fingerprint": candidate["group_fingerprint"],
                "action_type": action_type,
                "advertiser_id": str(candidate["advertiser_id"]),
                "ad_id": str(candidate["ad_id"]),
                "execution_mode": str(candidate["execution_mode"]),
                "approved_at": candidate["approved_at"],
                "created_at": candidate["created_at"],
            },
            "items": frozen_items,
            # 控制任务预算/时长的“候选冻结时服务器基线”在 Phase 5 还未单独固化。
            # 在补齐前，即使只读 Preflight 通过，也绝不能据此开放 UPDATE_* POST。
            "external_change_baseline_complete": action_type not in {UPDATE_BUDGET, UPDATE_DURATION},
        }
        expected_after = {
            "action_type": action_type,
            "material_ids": [item["material_id"] for item in frozen_items if item.get("material_id")],
            "control_task_id": control_task_id,
            "execution_params": dict(execution_params),
        }

        def work(conn):
            existing = conn.execute(
                "SELECT execution_id,status,action_type FROM execution_task WHERE execution_id=? OR candidate_id=? LIMIT 1",
                (execution_id, cid),
            ).fetchone()
            if existing is not None:
                return ExecutionPreparation(
                    execution_id=str(existing["execution_id"]),
                    candidate_id=cid,
                    action_type=str(existing["action_type"]),
                    status=str(existing["status"]),
                    created=False,
                    object_count=len(items),
                )

            conn.execute(
                """INSERT INTO execution_task(
                   execution_id,candidate_id,strategy_id,strategy_version_id,advertiser_id,ad_id,
                   action_type,execution_mode,status,expected_before_json,expected_after_json,
                   execution_params_json,control_task_id,created_at,approved_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    execution_id,
                    cid,
                    candidate["strategy_id"],
                    candidate["strategy_version_id"],
                    str(candidate["advertiser_id"]),
                    str(candidate["ad_id"]),
                    action_type,
                    str(candidate["execution_mode"]),
                    PENDING,
                    _json(frozen_before),
                    _json(expected_after),
                    _json(dict(execution_params)),
                    control_task_id,
                    now,
                    candidate["approved_at"] or now,
                ),
            )
            if action_type == CREATE_RETARGET:
                for item in items:
                    material_id = str(item["material_id"])
                    registry = conn.execute(
                        """SELECT material_uid FROM material_registry
                           WHERE advertiser_id=? AND ad_id=? AND material_id=?""",
                        (str(candidate["advertiser_id"]), str(candidate["ad_id"]), material_id),
                    ).fetchone()
                    conn.execute(
                        """INSERT INTO execution_task_material(
                           execution_id,material_uid,material_id,hit_id,candidate_item_id
                           ) VALUES(?,?,?,?,?)""",
                        (
                            execution_id,
                            registry["material_uid"] if registry is not None else None,
                            material_id,
                            item["hit_id"],
                            item["candidate_item_id"],
                        ),
                    )
            return ExecutionPreparation(
                execution_id=execution_id,
                candidate_id=cid,
                action_type=action_type,
                status=PENDING,
                created=True,
                object_count=len(items),
            )

        return self._writer.transaction(work).result(timeout=10)

    def get(self, execution_id: str) -> dict[str, Any] | None:
        with self._database.connect(readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM execution_task WHERE execution_id=?", (execution_id,)
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["materials"] = [
                dict(item)
                for item in conn.execute(
                    "SELECT * FROM execution_task_material WHERE execution_id=? ORDER BY material_id",
                    (execution_id,),
                ).fetchall()
            ]
            return result

    @staticmethod
    def _frozen_item(item: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "candidate_item_id": str(item["candidate_item_id"]),
            "hit_id": item["hit_id"],
            "object_uid": str(item["object_uid"]),
            "material_id": item["material_id"],
            "control_task_id": item["control_task_id"],
            "metric_snapshot": _load_json(item["metric_snapshot_json"], label="metric_snapshot_json"),
            "before_state": _load_json(item["before_state_json"], label="before_state_json"),
        }


class ExecutionPreflightService:
    """只读服务器 Preflight；通过后仅把 Execution 本地状态推进到 APPROVED。"""

    def __init__(
        self,
        database: Database,
        writer: StorageWriter,
        client: Any,
        token_provider: Any,
        plan_catalog: Any,
        *,
        clock: Clock = utc_now,
    ) -> None:
        self._database = database
        self._writer = writer
        self._client = client
        self._tokens = token_provider
        self._plans = plan_catalog
        self._clock = clock

    def preflight(self, execution_id: str) -> PreflightResult:
        context = self._load_context(execution_id)
        status = str(context["status"])
        if status == EXECUTION_APPROVED:
            return PreflightResult(execution_id, EXECUTION_APPROVED, False, None, ())
        if status == CANCELLED:
            return PreflightResult(execution_id, CANCELLED, False, str(context["cancel_reason"] or ""), ())
        if status != PENDING:
            raise ExecutionStateError(f"preflight is not allowed from execution status {status}")

        local_reason = self._local_cancel_reason(context)
        if local_reason is not None:
            return self._cancel(execution_id, local_reason)

        # 下面开始访问官方服务器。任何网络/Token/权限/契约异常直接向上抛，
        # Execution 仍保持 PENDING；不能把“取不到证据”误判成业务已结束。
        plan, plan_request_id = self._plans.get_detail(
            str(context["auth_profile_id"]),
            str(context["advertiser_id"]),
            str(context["ad_id"]),
            expected_marketing_goal=str(context["promotion_scene"]),
            expected_adlab_scene=str(context["plan_system"]),
        )
        request_ids: list[str] = [plan_request_id] if plan_request_id else []
        if str(plan.advertiser_id) != str(context["advertiser_id"]) or str(plan.ad_id) != str(context["ad_id"]):
            return self._cancel(execution_id, "PLAN_CONTEXT_MISMATCH", tuple(request_ids))
        if str(plan.classification_status) != "VERIFIED":
            return self._cancel(execution_id, "PLAN_CLASSIFICATION_NOT_VERIFIED", tuple(request_ids))
        if str(plan.official_status) != "DELIVERY_OK":
            return self._cancel(execution_id, "PLAN_NOT_DELIVERY_OK", tuple(request_ids))

        evidence: dict[str, Any] = {
            "checked_at": _iso(self._clock()),
            "plan": {
                "advertiser_id": str(plan.advertiser_id),
                "ad_id": str(plan.ad_id),
                "official_status": str(plan.official_status),
                "classification_status": str(plan.classification_status),
                "modify_time": getattr(plan, "modify_time", None),
                "budget_decimal": getattr(plan, "budget_decimal", None),
            },
        }

        action = str(context["action_type"])
        if action == CREATE_RETARGET:
            result = self._preflight_retarget(context, evidence)
        elif action in CONTROL_ACTIONS:
            result = self._preflight_control(context, evidence)
        else:
            raise ExecutionStateError(f"unsupported execution action: {action}")
        request_ids.extend(result["request_ids"])
        if result["cancel_reason"] is not None:
            return self._cancel(execution_id, str(result["cancel_reason"]), tuple(request_ids))

        evidence.update(result["evidence"])
        if action in {UPDATE_BUDGET, UPDATE_DURATION}:
            evidence["external_change_baseline_complete"] = False
            evidence["post_blocker"] = "CANDIDATE_FREEZE_MISSING_CONTROL_BUDGET_DURATION_BASELINE"

        now = _iso(self._clock())

        def work(conn):
            row = conn.execute(
                "SELECT status,expected_before_json FROM execution_task WHERE execution_id=?",
                (execution_id,),
            ).fetchone()
            if row is None:
                raise ExecutionStateError("execution disappeared during preflight")
            current = str(row["status"])
            if current == EXECUTION_APPROVED:
                return False
            if current != PENDING:
                raise ExecutionStateError(f"execution changed during preflight: {current}")
            before = _load_json(row["expected_before_json"], label="expected_before_json")
            if not isinstance(before, dict):
                raise ExecutionStateError("expected_before_json must be an object")
            before["server_preflight"] = evidence
            conn.execute(
                """UPDATE execution_task SET status='APPROVED',expected_before_json=?,
                   last_error_code=NULL,last_error_message=NULL WHERE execution_id=? AND status='PENDING'""",
                (_json(before), execution_id),
            )
            return True

        changed = bool(self._writer.transaction(work).result(timeout=10))
        return PreflightResult(execution_id, EXECUTION_APPROVED, changed, None, tuple(request_ids))

    def _load_context(self, execution_id: str) -> dict[str, Any]:
        with self._database.connect(readonly=True) as conn:
            row = conn.execute(
                """SELECT e.*,c.status AS candidate_status,
                          p.target_uid,p.account_uid,p.monitor_enabled,p.plan_system,p.promotion_scene,
                          p.official_status AS local_plan_status,p.write_eligible,p.sync_state AS plan_sync_state,
                          a.enabled AS account_enabled,a.auth_status AS account_auth_status,
                          (SELECT aa.auth_profile_id FROM qianchuan_account_auth aa
                           WHERE aa.account_uid=p.account_uid AND aa.is_primary=1 LIMIT 1) AS auth_profile_id
                   FROM execution_task e
                   JOIN candidate_batch c ON c.candidate_id=e.candidate_id
                   LEFT JOIN monitor_plan p
                     ON p.advertiser_id=e.advertiser_id AND p.ad_id=e.ad_id
                   LEFT JOIN qianchuan_account a ON a.account_uid=p.account_uid
                   WHERE e.execution_id=?""",
                (execution_id,),
            ).fetchone()
            if row is None:
                raise ExecutionStateError("execution does not exist")
            result = dict(row)
            result["materials"] = [
                dict(item)
                for item in conn.execute(
                    "SELECT * FROM execution_task_material WHERE execution_id=? ORDER BY material_id",
                    (execution_id,),
                ).fetchall()
            ]
        return result

    @staticmethod
    def _local_cancel_reason(context: Mapping[str, Any]) -> str | None:
        if str(context.get("candidate_status") or "") != CANDIDATE_APPROVED:
            return "CANDIDATE_NOT_APPROVED"
        if not context.get("target_uid"):
            return "PLAN_CONTEXT_MISSING"
        if not int(context.get("account_enabled") or 0):
            return "ACCOUNT_DISABLED"
        if str(context.get("account_auth_status") or "") != "ACTIVE":
            return "ACCOUNT_AUTH_INACTIVE"
        if not int(context.get("monitor_enabled") or 0):
            return "PLAN_MONITOR_DISABLED"
        if not str(context.get("auth_profile_id") or ""):
            return "AUTH_PROFILE_MISSING"
        if str(context.get("plan_system") or "") not in {"OVERALL_PROJECT", "UNI_PROJECT"}:
            return "PLAN_CLASSIFICATION_LOCAL_INVALID"
        if str(context.get("promotion_scene") or "") not in {"LIVE_PROM_GOODS", "VIDEO_PROM_GOODS"}:
            return "PLAN_CLASSIFICATION_LOCAL_INVALID"
        return None

    def _preflight_retarget(self, context: Mapping[str, Any], plan_evidence: Mapping[str, Any]) -> dict[str, Any]:
        frozen = tuple(str(row["material_id"]) for row in context["materials"] if row.get("material_id"))
        if not frozen:
            raise ExecutionStateError("retarget execution has no frozen materials")
        business_date = self._clock().astimezone(CHINA_TZ).strftime("%Y-%m-%d")
        aid = str(context["advertiser_id"])
        ad_id = str(context["ad_id"])
        auth_profile_id = str(context["auth_profile_id"])
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
            refresh_access_token=lambda: self._tokens.get_access_token(auth_profile_id, force_refresh=True),
        )
        records = {
            record.material_id: record
            for record in (normalize_material_hot(row, advertiser_id=aid, ad_id=ad_id) for row in rows)
        }
        for material_id in frozen:
            record = records.get(material_id)
            if record is None:
                return {"cancel_reason": f"MATERIAL_NOT_FOUND:{material_id}", "request_ids": tuple(request_ids), "evidence": {}}
            if record.official_material_status != "DELIVERY_OK":
                return {"cancel_reason": f"MATERIAL_NOT_DELIVERY_OK:{material_id}", "request_ids": tuple(request_ids), "evidence": {}}
            if self._has_active_tool_retarget(aid, ad_id, material_id):
                return {"cancel_reason": f"RETARGET_ACTIVE_TASK_EXISTS:{material_id}", "request_ids": tuple(request_ids), "evidence": {}}
        material_evidence = [
            {
                "material_id": material_id,
                "official_material_status": records[material_id].official_material_status,
                "official_audit_status": records[material_id].official_audit_status,
            }
            for material_id in frozen
        ]
        return {
            "cancel_reason": None,
            "request_ids": tuple(request_ids),
            "evidence": {"materials": material_evidence},
        }

    def _preflight_control(self, context: Mapping[str, Any], plan_evidence: Mapping[str, Any]) -> dict[str, Any]:
        task_id = str(context.get("control_task_id") or "").strip()
        if not task_id:
            raise ExecutionStateError("control execution is missing control_task_id")
        current = self._clock().astimezone(CHINA_TZ)
        business_date = current.strftime("%Y-%m-%d")
        start_time = f"{business_date} 00:00:00"
        end_time = current.strftime("%Y-%m-%d %H:%M:%S")
        aid = str(context["advertiser_id"])
        ad_id = str(context["ad_id"])
        auth_profile_id = str(context["auth_profile_id"])
        rows, request_ids = get_all_pages(
            self._client,
            CONTROL_TASK_LIST,
            query={
                "advertiser_id": aid,
                "ad_id": ad_id,
                "marketing_goal": str(context["promotion_scene"]),
                "start_time": start_time,
                "end_time": end_time,
                "scene": "MATERIAL_ADD_BUDGET",
                "fields": list(CONTROL_METRIC_FIELDS),
            },
            access_token=self._tokens.get_access_token(auth_profile_id),
            advertiser_id=aid,
            page_size=100,
            identity_getter=control_task_identity,
            refresh_access_token=lambda: self._tokens.get_access_token(auth_profile_id, force_refresh=True),
        )
        records = {
            record.control_task_id: record
            for record in (normalize_control_task_hot(row, advertiser_id=aid, ad_id=ad_id) for row in rows)
        }
        record = records.get(task_id)
        if record is None:
            return {"cancel_reason": "CONTROL_TASK_NOT_FOUND", "request_ids": tuple(request_ids), "evidence": {}}
        if record.official_task_status != "PROCESSING":
            return {"cancel_reason": f"CONTROL_TASK_NOT_PROCESSING:{record.official_task_status}", "request_ids": tuple(request_ids), "evidence": {}}
        return {
            "cancel_reason": None,
            "request_ids": tuple(request_ids),
            "evidence": {
                "control_task": {
                    "control_task_id": task_id,
                    "official_task_status": record.official_task_status,
                    "budget_decimal": record.budget,
                    "duration_decimal": record.duration_decimal,
                    "bid_decimal": record.bid,
                    "roi_goal_decimal": record.roi_goal,
                    "material_ids": list(record.material_ids),
                }
            },
        }

    def _has_active_tool_retarget(self, advertiser_id: str, ad_id: str, material_id: str) -> bool:
        with self._database.connect(readonly=True) as conn:
            row = conn.execute(
                """SELECT 1 FROM control_task_registry r
                   JOIN control_task_latest l ON l.control_task_uid=r.control_task_uid
                   JOIN control_task_material m ON m.control_task_uid=r.control_task_uid
                   WHERE r.advertiser_id=? AND r.ad_id=? AND m.material_id=?
                     AND r.created_by_tool=1 AND l.official_task_status='PROCESSING'
                   LIMIT 1""",
                (advertiser_id, ad_id, material_id),
            ).fetchone()
        return row is not None

    def _cancel(self, execution_id: str, reason: str, request_ids: tuple[str, ...] = ()) -> PreflightResult:
        now = _iso(self._clock())

        def work(conn):
            row = conn.execute("SELECT status FROM execution_task WHERE execution_id=?", (execution_id,)).fetchone()
            if row is None:
                raise ExecutionStateError("execution does not exist")
            current = str(row["status"])
            if current == CANCELLED:
                return False
            if current != PENDING:
                raise ExecutionStateError(f"cannot cancel execution from {current}")
            conn.execute(
                """UPDATE execution_task SET status='CANCELLED',cancelled_at=?,cancel_reason=?,
                   last_error_code=?,last_error_message=? WHERE execution_id=?""",
                (now, reason, reason.split(":", 1)[0], reason, execution_id),
            )
            return True

        changed = bool(self._writer.transaction(work).result(timeout=5))
        return PreflightResult(execution_id, CANCELLED, changed, reason, request_ids)
