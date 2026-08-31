"""Phase 6 控制任务外部人工变更防护。

`service.py` 保留通用 PREPARE/PREFLIGHT 状态机；本层把 Phase 6 新增的 exact-batch
Candidate 控制任务基线接入 Execution：

- 新候选若冻结了可信 control_state_snapshot，Execution 将其提升为正式外部变更基线；
- UPDATE_BUDGET / UPDATE_DURATION 在官方 GET Preflight 时比较“候选冻结值 vs 当前服务器值”；
- 只要不同，就认定用户/平台在候选之后改变过服务器事实，取消当前 Execution；
- 老候选/人工测试若没有该基线，仍可完成只读 Preflight，但继续带 POST blocker，绝不能进入
  后续真实 Write Gate。

本模块仍然不包含任何千川业务 POST，也不会创建 execution_attempt。
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from commercial_v1.candidate import PAUSE_CONTROL, UPDATE_BUDGET, UPDATE_DURATION

from . import service as base

CONTROL_ACTIONS = frozenset({PAUSE_CONTROL, UPDATE_BUDGET, UPDATE_DURATION})


def _load_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except Exception as exc:
        raise base.ExecutionStateError("stored execution evidence is invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise base.ExecutionStateError("stored execution evidence must be an object")
    return parsed


def _decimal_equal(left: Any, right: Any) -> bool | None:
    if left in (None, "") or right in (None, ""):
        return None
    try:
        a = Decimal(str(left))
        b = Decimal(str(right))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not a.is_finite() or not b.is_finite():
        return None
    return a == b


def _candidate_control_baseline(frozen_before: Mapping[str, Any]) -> dict[str, Any] | None:
    items = frozen_before.get("items")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], Mapping):
        return None
    before_state = items[0].get("before_state")
    if not isinstance(before_state, Mapping):
        return None
    if before_state.get("control_state_baseline_complete") is not True:
        return None
    snapshot = before_state.get("control_state_snapshot")
    if not isinstance(snapshot, Mapping):
        return None
    if str(snapshot.get("sync_state") or "") != "TRUSTED":
        return None
    if not str(snapshot.get("control_task_id") or "").strip():
        return None
    if not str(snapshot.get("batch_id") or "").strip():
        return None
    return dict(snapshot)


class ExecutionService(base.ExecutionService):
    """在通用 Execution 冻结后提升 exact Candidate control baseline。"""

    def prepare_from_candidate(self, candidate_id: str) -> base.ExecutionPreparation:
        result = super().prepare_from_candidate(candidate_id)
        if result.action_type not in CONTROL_ACTIONS:
            return result

        with self._database.connect(readonly=True) as conn:
            row = conn.execute(
                "SELECT expected_before_json FROM execution_task WHERE execution_id=?",
                (result.execution_id,),
            ).fetchone()
        if row is None:
            raise base.ExecutionStateError("execution disappeared after preparation")
        frozen_before = _load_object(row["expected_before_json"])
        baseline = _candidate_control_baseline(frozen_before)
        if baseline is None:
            # 老候选没有 exact baseline：继续 fail-closed，保留 false blocker。
            return result

        # 至少身份/状态必须匹配，预算/时长字段可以为 NULL；具体 UPDATE 动作会在 Preflight
        # 判断字段是否可比较。NULL 不会被擅自当 0。
        control_task_id = str(frozen_before["items"][0].get("control_task_id") or "")
        if str(baseline.get("control_task_id") or "") != control_task_id:
            raise base.ExecutionStateError("candidate control baseline identity mismatch")

        frozen_before["control_candidate_baseline"] = baseline
        frozen_before["external_change_baseline_complete"] = True
        self._writer.execute(
            "UPDATE execution_task SET expected_before_json=? WHERE execution_id=?",
            (base._json(frozen_before), result.execution_id),
        ).result(timeout=5)
        return result


class ExecutionPreflightService(base.ExecutionPreflightService):
    """在通用只读 Preflight 上增加候选后人工修改检测。"""

    def _preflight_control(self, context: Mapping[str, Any], plan_evidence: Mapping[str, Any]) -> dict[str, Any]:
        result = super()._preflight_control(context, plan_evidence)
        if result.get("cancel_reason") is not None:
            return result

        action = str(context.get("action_type") or "").upper()
        if action not in CONTROL_ACTIONS:
            return result

        before = _load_object(context.get("expected_before_json"))
        baseline = before.get("control_candidate_baseline")
        if not isinstance(baseline, Mapping):
            # 没有候选时 exact baseline 的旧数据仍允许做只读核验，但明确维持 POST blocker。
            result["evidence"]["external_change_baseline_complete"] = False
            result["evidence"]["post_blocker"] = "CONTROL_CANDIDATE_BASELINE_MISSING"
            return result

        current = result.get("evidence", {}).get("control_task")
        if not isinstance(current, Mapping):
            raise base.ExecutionStateError("control preflight returned no current task evidence")
        if str(current.get("control_task_id") or "") != str(baseline.get("control_task_id") or ""):
            result["cancel_reason"] = "CONTROL_TASK_IDENTITY_CHANGED"
            return result

        if action == UPDATE_BUDGET:
            equal = _decimal_equal(baseline.get("budget_decimal"), current.get("budget_decimal"))
            if equal is None:
                result["cancel_reason"] = "CONTROL_BUDGET_BASELINE_NOT_COMPARABLE"
                return result
            if not equal:
                result["cancel_reason"] = "CANCELLED_EXTERNAL_CHANGE_BUDGET"
                return result
        elif action == UPDATE_DURATION:
            equal = _decimal_equal(baseline.get("duration_decimal"), current.get("duration_decimal"))
            if equal is None:
                result["cancel_reason"] = "CONTROL_DURATION_BASELINE_NOT_COMPARABLE"
                return result
            if not equal:
                result["cancel_reason"] = "CANCELLED_EXTERNAL_CHANGE_DURATION"
                return result

        result["evidence"]["candidate_control_baseline"] = dict(baseline)
        result["evidence"]["external_change_baseline_complete"] = True
        return result

    def preflight(self, execution_id: str) -> base.PreflightResult:
        result = super().preflight(execution_id)
        if result.status != base.EXECUTION_APPROVED:
            return result

        with self._database.connect(readonly=True) as conn:
            row = conn.execute(
                "SELECT action_type,expected_before_json FROM execution_task WHERE execution_id=?",
                (execution_id,),
            ).fetchone()
        if row is None:
            raise base.ExecutionStateError("execution disappeared after preflight")
        action = str(row["action_type"] or "").upper()
        if action not in {UPDATE_BUDGET, UPDATE_DURATION}:
            return result

        before = _load_object(row["expected_before_json"])
        baseline = before.get("control_candidate_baseline")
        if not isinstance(baseline, Mapping):
            # 旧候选：base service 已写入 false blocker，保持不动。
            return result

        server = before.get("server_preflight")
        if not isinstance(server, dict):
            raise base.ExecutionStateError("approved control execution lacks server preflight evidence")
        server["external_change_baseline_complete"] = True
        server.pop("post_blocker", None)
        before["external_change_baseline_complete"] = True
        self._writer.execute(
            "UPDATE execution_task SET expected_before_json=? WHERE execution_id=? AND status='APPROVED'",
            (base._json(before), execution_id),
        ).result(timeout=5)
        return result
