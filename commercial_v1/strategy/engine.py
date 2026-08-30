"""Phase 4 确定性策略配置、不可变版本与三态求值。

本模块只消费 Phase 3 已持久化的 TRUSTED Latest，不访问千川网络，不创建候选，也不
执行任何平台写操作。任何 NULL/缺字段都进入 NOT_EVALUABLE，绝不猜成 0。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from commercial_v1.storage.database import Database
from commercial_v1.storage.writer import StorageWriter


MATERIAL_FIELDS = frozenset(
    {
        "overall_cost_decimal",
        "net_settle_amount_decimal",
        "net_settle_roi_decimal",
        "net_settle_order_count",
        "overall_order_count",
        "overall_gmv_decimal",
        "overall_pay_roi_decimal",
    }
)

CONTROL_FIELDS = frozenset(
    {
        "assist_cost_decimal",
        "assist_order_count",
        "assist_gmv_decimal",
        "assist_pay_roi_decimal",
        "assist_net_amount_decimal",
        "assist_net_roi_decimal",
        "assist_net_order_count",
    }
)

STRATEGY_CONTRACTS: dict[str, tuple[str, str, frozenset[str]]] = {
    "MATERIAL_RETARGET": ("MATERIAL", "CREATE_RETARGET", MATERIAL_FIELDS),
    "CONTROL_STOP": ("CONTROL_TASK", "PAUSE_CONTROL", CONTROL_FIELDS),
    "CONTROL_BUDGET_INCREASE": ("CONTROL_TASK", "UPDATE_BUDGET", CONTROL_FIELDS),
    "CONTROL_DURATION_EXTEND": ("CONTROL_TASK", "UPDATE_DURATION", CONTROL_FIELDS),
}

OPERATORS = frozenset({"GT", "GTE", "LT", "LTE", "EQ", "NE"})
EXECUTION_MODES = frozenset({"MANUAL", "AUTO"})
GROUPING_MODES = frozenset({"SEPARATE", "MERGED"})

HIT = "HIT"
NOT_HIT = "NOT_HIT"
NOT_EVALUABLE = "NOT_EVALUABLE"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


def _decimal(value: Any, *, label: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{label} must be a valid decimal") from exc
    if not number.is_finite():
        raise ValueError(f"{label} must be finite")
    return number


def _decimal_text(value: Any, *, label: str) -> str:
    number = _decimal(value, label=label)
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _target_scope(target_uid: str) -> str:
    target = str(target_uid or "").strip()
    if not target:
        raise ValueError("target_uid is required")
    return f"PLAN:{target}"


def _normalize_conditions(
    strategy_type: str,
    condition_spec: Mapping[str, Any],
) -> dict[str, Any]:
    contract = STRATEGY_CONTRACTS.get(str(strategy_type or "").strip().upper())
    if contract is None:
        raise ValueError("unsupported V1 strategy_type")
    allowed_fields = contract[2]
    logic = str(condition_spec.get("logic") or "").strip().upper()
    if logic != "AND":
        raise ValueError("Phase 4 only supports AND conditions")
    raw_conditions = condition_spec.get("conditions")
    if not isinstance(raw_conditions, Sequence) or isinstance(raw_conditions, (str, bytes)):
        raise ValueError("conditions must be a non-empty list")
    if not raw_conditions:
        raise ValueError("strategy must contain at least one condition")

    normalized: list[dict[str, str]] = []
    for index, raw in enumerate(raw_conditions):
        if not isinstance(raw, Mapping):
            raise ValueError(f"condition[{index}] must be an object")
        field = str(raw.get("field") or "").strip()
        operator = str(raw.get("op") or "").strip().upper()
        if field not in allowed_fields:
            raise ValueError(f"condition field is outside trusted V1 metrics: {field}")
        if operator not in OPERATORS:
            raise ValueError(f"unsupported condition operator: {operator}")
        if "value" not in raw or raw.get("value") is None:
            raise ValueError(f"condition[{index}] value is required")
        normalized.append(
            {
                "field": field,
                "op": operator,
                "value": _decimal_text(raw.get("value"), label=f"condition[{index}].value"),
            }
        )
    return {"logic": "AND", "conditions": normalized}


def _normalize_action_config(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("action_config must be an object")
    # Phase 4 只冻结参数，不解释执行语义；Phase 5+ 必须再次按对应写接口契约验证。
    return json.loads(_json(dict(value)))


def _content_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _deterministic_hit_id(
    strategy_version_id: str,
    object_uid: str,
    source_batch_id: str,
) -> str:
    raw = f"{strategy_version_id}|{object_uid}|{source_batch_id}".encode("utf-8")
    return "hit_" + hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class StrategyVersion:
    strategy_id: str
    strategy_name: str
    strategy_type: str
    object_type: str
    target_scope: str
    action_type: str
    execution_mode: str
    enabled: bool
    strategy_version_id: str
    version_no: int
    conditions: Mapping[str, Any]
    action_config: Mapping[str, Any]
    grouping_mode: str
    priority: int
    content_hash: str


@dataclass(frozen=True)
class ConditionOutcome:
    field: str
    operator: str
    expected: str
    actual: str | None
    result: str


@dataclass(frozen=True)
class EvaluationOutcome:
    strategy_id: str
    strategy_version_id: str
    strategy_type: str
    action_type: str
    priority: int
    object_type: str
    object_uid: str
    material_id: str | None
    control_task_id: str | None
    advertiser_id: str
    ad_id: str
    source_batch_id: str
    source_collected_at: str
    result: str
    conditions: tuple[ConditionOutcome, ...]
    metric_snapshot: Mapping[str, Any]
    hit_id: str | None = None
    suppression_reason: str | None = None
    winner_strategy_id: str | None = None


@dataclass(frozen=True)
class EvaluationSummary:
    target_uid: str
    pipeline_type: str
    source_batch_id: str
    evaluated: int
    hit: int
    not_hit: int
    not_evaluable: int
    persisted_hits: int
    suppressed_hits: int
    outcomes: tuple[EvaluationOutcome, ...]


class StrategyStore:
    def __init__(self, database: Database, writer: StorageWriter) -> None:
        self._database = database
        self._writer = writer

    def create_strategy(
        self,
        *,
        strategy_name: str,
        strategy_type: str,
        target_uid: str,
        execution_mode: str,
        priority: int,
        conditions: Mapping[str, Any],
        action_config: Mapping[str, Any] | None = None,
        grouping_mode: str = "SEPARATE",
        created_by: str = "LOCAL_USER",
    ) -> StrategyVersion:
        kind = str(strategy_type or "").strip().upper()
        contract = STRATEGY_CONTRACTS.get(kind)
        if contract is None:
            raise ValueError("unsupported V1 strategy_type")
        object_type, action_type, _ = contract
        name = str(strategy_name or "").strip()
        if not name:
            raise ValueError("strategy_name is required")
        mode = str(execution_mode or "").strip().upper()
        if mode not in EXECUTION_MODES:
            raise ValueError("execution_mode must be MANUAL or AUTO")
        grouping = str(grouping_mode or "").strip().upper()
        if grouping not in GROUPING_MODES:
            raise ValueError("grouping_mode must be SEPARATE or MERGED")
        prio = int(priority)
        if prio < 0 or prio > 100000:
            raise ValueError("priority is outside supported range")
        normalized_conditions = _normalize_conditions(kind, conditions)
        normalized_action = _normalize_action_config(action_config)
        scope = _target_scope(target_uid)
        strategy_id = "strategy_" + uuid.uuid4().hex
        version_id = "strategy_version_" + uuid.uuid4().hex
        payload = {
            "strategy_type": kind,
            "target_scope": scope,
            "action_type": action_type,
            "execution_mode": mode,
            "condition_json": normalized_conditions,
            "action_config_json": normalized_action,
            "grouping_mode": grouping,
            "priority": prio,
        }
        content_hash = _content_hash(payload)

        def work(conn):
            plan = conn.execute(
                "SELECT target_uid FROM monitor_plan WHERE target_uid=?",
                (target_uid,),
            ).fetchone()
            if plan is None:
                raise ValueError("target plan does not exist")
            now = conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]
            conn.execute(
                """INSERT INTO strategy_config(
                   strategy_id,strategy_name,strategy_type,target_scope,action_type,execution_mode,
                   enabled,priority,current_version_id,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,1,?,?,?,?)""",
                (strategy_id, name, kind, scope, action_type, mode, prio, version_id, now, now),
            )
            conn.execute(
                """INSERT INTO strategy_version(
                   strategy_version_id,strategy_id,version_no,condition_json,action_config_json,
                   grouping_mode,priority,created_at,created_by,content_hash
                   ) VALUES(?,?,1,?,?,?,?,?,?,?)""",
                (
                    version_id,
                    strategy_id,
                    _json(normalized_conditions),
                    _json(normalized_action),
                    grouping,
                    prio,
                    now,
                    str(created_by or "LOCAL_USER"),
                    content_hash,
                ),
            )

        self._writer.transaction(work).result(timeout=5)
        return StrategyVersion(
            strategy_id=strategy_id,
            strategy_name=name,
            strategy_type=kind,
            object_type=object_type,
            target_scope=scope,
            action_type=action_type,
            execution_mode=mode,
            enabled=True,
            strategy_version_id=version_id,
            version_no=1,
            conditions=normalized_conditions,
            action_config=normalized_action,
            grouping_mode=grouping,
            priority=prio,
            content_hash=content_hash,
        )

    def create_new_version(
        self,
        strategy_id: str,
        *,
        conditions: Mapping[str, Any] | None = None,
        action_config: Mapping[str, Any] | None = None,
        grouping_mode: str | None = None,
        priority: int | None = None,
        execution_mode: str | None = None,
        created_by: str = "LOCAL_USER",
    ) -> StrategyVersion:
        current = self.get_current(strategy_id)
        normalized_conditions = (
            _normalize_conditions(current.strategy_type, conditions)
            if conditions is not None
            else dict(current.conditions)
        )
        normalized_action = (
            _normalize_action_config(action_config)
            if action_config is not None
            else dict(current.action_config)
        )
        grouping = (
            str(grouping_mode).strip().upper()
            if grouping_mode is not None
            else current.grouping_mode
        )
        if grouping not in GROUPING_MODES:
            raise ValueError("grouping_mode must be SEPARATE or MERGED")
        prio = current.priority if priority is None else int(priority)
        if prio < 0 or prio > 100000:
            raise ValueError("priority is outside supported range")
        mode = current.execution_mode if execution_mode is None else str(execution_mode).strip().upper()
        if mode not in EXECUTION_MODES:
            raise ValueError("execution_mode must be MANUAL or AUTO")
        version_id = "strategy_version_" + uuid.uuid4().hex
        version_no = current.version_no + 1
        payload = {
            "strategy_type": current.strategy_type,
            "target_scope": current.target_scope,
            "action_type": current.action_type,
            "execution_mode": mode,
            "condition_json": normalized_conditions,
            "action_config_json": normalized_action,
            "grouping_mode": grouping,
            "priority": prio,
        }
        content_hash = _content_hash(payload)

        def work(conn):
            latest = conn.execute(
                "SELECT current_version_id FROM strategy_config WHERE strategy_id=?",
                (strategy_id,),
            ).fetchone()
            if latest is None:
                raise ValueError("strategy does not exist")
            if str(latest["current_version_id"] or "") != current.strategy_version_id:
                raise RuntimeError("strategy changed concurrently; reload before saving")
            max_version = conn.execute(
                "SELECT COALESCE(MAX(version_no),0) FROM strategy_version WHERE strategy_id=?",
                (strategy_id,),
            ).fetchone()[0]
            if int(max_version) != current.version_no:
                raise RuntimeError("strategy version history changed concurrently")
            now = conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]
            conn.execute(
                """INSERT INTO strategy_version(
                   strategy_version_id,strategy_id,version_no,condition_json,action_config_json,
                   grouping_mode,priority,created_at,created_by,content_hash
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    version_id,
                    strategy_id,
                    version_no,
                    _json(normalized_conditions),
                    _json(normalized_action),
                    grouping,
                    prio,
                    now,
                    str(created_by or "LOCAL_USER"),
                    content_hash,
                ),
            )
            conn.execute(
                """UPDATE strategy_config SET execution_mode=?,priority=?,current_version_id=?,updated_at=?
                   WHERE strategy_id=?""",
                (mode, prio, version_id, now, strategy_id),
            )

        self._writer.transaction(work).result(timeout=5)
        return replace(
            current,
            execution_mode=mode,
            strategy_version_id=version_id,
            version_no=version_no,
            conditions=normalized_conditions,
            action_config=normalized_action,
            grouping_mode=grouping,
            priority=prio,
            content_hash=content_hash,
        )

    def set_enabled(self, strategy_id: str, enabled: bool) -> None:
        def work(conn):
            now = conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]
            result = conn.execute(
                "UPDATE strategy_config SET enabled=?,updated_at=? WHERE strategy_id=?",
                (1 if enabled else 0, now, strategy_id),
            )
            if result.rowcount != 1:
                raise ValueError("strategy does not exist")

        self._writer.transaction(work).result(timeout=5)

    def get_current(self, strategy_id: str) -> StrategyVersion:
        with self._database.connect(readonly=True) as conn:
            row = conn.execute(
                """SELECT c.*,v.strategy_version_id,v.version_no,v.condition_json,v.action_config_json,
                          v.grouping_mode,v.priority AS version_priority,v.content_hash
                   FROM strategy_config c
                   JOIN strategy_version v ON v.strategy_version_id=c.current_version_id
                   WHERE c.strategy_id=?""",
                (strategy_id,),
            ).fetchone()
        if row is None:
            raise ValueError("strategy does not exist")
        return self._row_to_version(row)

    def list_for_target(self, target_uid: str, *, object_type: str) -> tuple[StrategyVersion, ...]:
        scope = _target_scope(target_uid)
        wanted = str(object_type or "").strip().upper()
        with self._database.connect(readonly=True) as conn:
            rows = conn.execute(
                """SELECT c.*,v.strategy_version_id,v.version_no,v.condition_json,v.action_config_json,
                          v.grouping_mode,v.priority AS version_priority,v.content_hash
                   FROM strategy_config c
                   JOIN strategy_version v ON v.strategy_version_id=c.current_version_id
                   WHERE c.enabled=1 AND c.target_scope=?
                   ORDER BY c.priority DESC,c.strategy_id ASC""",
                (scope,),
            ).fetchall()
        versions = tuple(self._row_to_version(row) for row in rows)
        return tuple(version for version in versions if version.object_type == wanted)

    @staticmethod
    def _row_to_version(row: Mapping[str, Any]) -> StrategyVersion:
        kind = str(row["strategy_type"])
        contract = STRATEGY_CONTRACTS.get(kind)
        if contract is None:
            raise ValueError(f"stored strategy_type is outside V1 contract: {kind}")
        object_type, expected_action, _ = contract
        if str(row["action_type"]) != expected_action:
            raise ValueError("stored strategy action_type conflicts with V1 contract")
        return StrategyVersion(
            strategy_id=str(row["strategy_id"]),
            strategy_name=str(row["strategy_name"]),
            strategy_type=kind,
            object_type=object_type,
            target_scope=str(row["target_scope"]),
            action_type=str(row["action_type"]),
            execution_mode=str(row["execution_mode"]),
            enabled=bool(row["enabled"]),
            strategy_version_id=str(row["strategy_version_id"]),
            version_no=int(row["version_no"]),
            conditions=json.loads(str(row["condition_json"])),
            action_config=json.loads(str(row["action_config_json"])),
            grouping_mode=str(row["grouping_mode"]),
            priority=int(row["version_priority"]),
            content_hash=str(row["content_hash"]),
        )


class StrategyEvaluator:
    @staticmethod
    def evaluate(
        version: StrategyVersion,
        metrics: Mapping[str, Any],
    ) -> tuple[str, tuple[ConditionOutcome, ...]]:
        allowed_fields = STRATEGY_CONTRACTS[version.strategy_type][2]
        outcomes: list[ConditionOutcome] = []
        has_not_evaluable = False
        has_not_hit = False

        for condition in version.conditions["conditions"]:
            field = str(condition["field"])
            operator = str(condition["op"])
            expected_text = str(condition["value"])
            if field not in allowed_fields:
                raise ValueError("stored condition field is outside trusted V1 metrics")
            actual_raw = metrics.get(field)
            if actual_raw is None:
                result = NOT_EVALUABLE
                actual_text = None
                has_not_evaluable = True
            else:
                try:
                    actual = _decimal(actual_raw, label=field)
                    expected = _decimal(expected_text, label=f"{field}.expected")
                except ValueError:
                    result = NOT_EVALUABLE
                    actual_text = str(actual_raw)
                    has_not_evaluable = True
                else:
                    actual_text = _decimal_text(actual, label=field)
                    matched = {
                        "GT": actual > expected,
                        "GTE": actual >= expected,
                        "LT": actual < expected,
                        "LTE": actual <= expected,
                        "EQ": actual == expected,
                        "NE": actual != expected,
                    }[operator]
                    result = HIT if matched else NOT_HIT
                    if not matched:
                        has_not_hit = True
            outcomes.append(
                ConditionOutcome(
                    field=field,
                    operator=operator,
                    expected=expected_text,
                    actual=actual_text,
                    result=result,
                )
            )

        if has_not_hit:
            return NOT_HIT, tuple(outcomes)
        if has_not_evaluable:
            return NOT_EVALUABLE, tuple(outcomes)
        return HIT, tuple(outcomes)


class StrategyEvaluationService:
    def __init__(
        self,
        database: Database,
        writer: StorageWriter,
        store: StrategyStore,
    ) -> None:
        self._database = database
        self._writer = writer
        self._store = store
        self._evaluator = StrategyEvaluator()

    def evaluate_material_batch(self, target_uid: str, source_batch_id: str) -> EvaluationSummary:
        return self._evaluate_batch(target_uid, source_batch_id, object_type="MATERIAL")

    def evaluate_control_batch(self, target_uid: str, source_batch_id: str) -> EvaluationSummary:
        return self._evaluate_batch(target_uid, source_batch_id, object_type="CONTROL_TASK")

    def _evaluate_batch(
        self,
        target_uid: str,
        source_batch_id: str,
        *,
        object_type: str,
    ) -> EvaluationSummary:
        target = self._load_target(target_uid)
        batch = self._load_trusted_source_batch(target_uid, source_batch_id, object_type)
        strategies = self._store.list_for_target(target_uid, object_type=object_type)
        objects = self._load_objects(target, source_batch_id, object_type)
        outcomes: list[EvaluationOutcome] = []

        for row in objects:
            object_uid = str(row["material_uid"] if object_type == "MATERIAL" else row["control_task_uid"])
            metric_fields = MATERIAL_FIELDS if object_type == "MATERIAL" else CONTROL_FIELDS
            metrics = {field: row[field] for field in metric_fields}
            for version in strategies:
                result, condition_outcomes = self._evaluator.evaluate(version, metrics)
                outcomes.append(
                    EvaluationOutcome(
                        strategy_id=version.strategy_id,
                        strategy_version_id=version.strategy_version_id,
                        strategy_type=version.strategy_type,
                        action_type=version.action_type,
                        priority=version.priority,
                        object_type=object_type,
                        object_uid=object_uid,
                        material_id=(str(row["material_id"]) if object_type == "MATERIAL" else None),
                        control_task_id=(str(row["control_task_id"]) if object_type == "CONTROL_TASK" else None),
                        advertiser_id=str(row["advertiser_id"]),
                        ad_id=str(row["ad_id"]),
                        source_batch_id=source_batch_id,
                        source_collected_at=str(row["collected_at"]),
                        result=result,
                        conditions=condition_outcomes,
                        metric_snapshot=metrics,
                    )
                )

        arbitrated = self._arbitrate(outcomes)
        persisted = self._persist_hits(target_uid, arbitrated)
        return EvaluationSummary(
            target_uid=target_uid,
            pipeline_type=str(batch["pipeline_type"]),
            source_batch_id=source_batch_id,
            evaluated=len(arbitrated),
            hit=sum(1 for outcome in arbitrated if outcome.result == HIT),
            not_hit=sum(1 for outcome in arbitrated if outcome.result == NOT_HIT),
            not_evaluable=sum(1 for outcome in arbitrated if outcome.result == NOT_EVALUABLE),
            persisted_hits=persisted,
            suppressed_hits=sum(1 for outcome in arbitrated if outcome.suppression_reason),
            outcomes=tuple(arbitrated),
        )

    def _load_target(self, target_uid: str) -> Mapping[str, Any]:
        with self._database.connect(readonly=True) as conn:
            row = conn.execute(
                """SELECT * FROM monitor_plan WHERE target_uid=? AND monitor_enabled=1
                   AND lifecycle_state='ACTIVE_COLLECTING' AND collection_active=1
                   AND strategy_eligible=1 AND official_status='DELIVERY_OK'""",
                (target_uid,),
            ).fetchone()
        if row is None:
            raise ValueError("target plan is not eligible for strategy evaluation")
        return row

    def _load_trusted_source_batch(
        self,
        target_uid: str,
        source_batch_id: str,
        object_type: str,
    ) -> Mapping[str, Any]:
        expected = "MATERIAL_5M" if object_type == "MATERIAL" else "CONTROL_5M"
        with self._database.connect(readonly=True) as conn:
            row = conn.execute(
                """SELECT * FROM collection_batch WHERE batch_id=? AND target_uid=?
                   AND pipeline_type=? AND status='SUCCESS'""",
                (source_batch_id, target_uid, expected),
            ).fetchone()
        if row is None:
            raise ValueError("source batch is not a trusted successful hot batch")
        return row

    def _load_objects(
        self,
        target: Mapping[str, Any],
        source_batch_id: str,
        object_type: str,
    ) -> tuple[Mapping[str, Any], ...]:
        with self._database.connect(readonly=True) as conn:
            if object_type == "MATERIAL":
                rows = conn.execute(
                    """SELECT * FROM material_latest
                       WHERE advertiser_id=? AND ad_id=? AND batch_id=?
                         AND sync_state='TRUSTED' AND strategy_eligible=1
                         AND official_material_status='DELIVERY_OK'
                       ORDER BY material_uid""",
                    (target["advertiser_id"], target["ad_id"], source_batch_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM control_task_latest
                       WHERE advertiser_id=? AND ad_id=? AND batch_id=?
                         AND sync_state='TRUSTED' AND strategy_eligible=1
                         AND official_task_status='PROCESSING'
                       ORDER BY control_task_uid""",
                    (target["advertiser_id"], target["ad_id"], source_batch_id),
                ).fetchall()
        return tuple(rows)

    @staticmethod
    def _arbitrate(outcomes: Sequence[EvaluationOutcome]) -> list[EvaluationOutcome]:
        result = list(outcomes)
        grouped: dict[tuple[str, str], list[int]] = {}
        for index, outcome in enumerate(result):
            if outcome.result != HIT:
                continue
            grouped.setdefault((outcome.object_uid, outcome.action_type), []).append(index)

        for indices in grouped.values():
            if len(indices) <= 1:
                winner_index = indices[0]
                winner = result[winner_index]
                result[winner_index] = replace(
                    winner,
                    hit_id=_deterministic_hit_id(
                        winner.strategy_version_id,
                        winner.object_uid,
                        winner.source_batch_id,
                    ),
                    winner_strategy_id=winner.strategy_id,
                )
                continue
            ordered = sorted(
                indices,
                key=lambda idx: (-result[idx].priority, result[idx].strategy_id),
            )
            winner_index = ordered[0]
            winner = result[winner_index]
            result[winner_index] = replace(
                winner,
                hit_id=_deterministic_hit_id(
                    winner.strategy_version_id,
                    winner.object_uid,
                    winner.source_batch_id,
                ),
                winner_strategy_id=winner.strategy_id,
            )
            for index in ordered[1:]:
                loser = result[index]
                result[index] = replace(
                    loser,
                    hit_id=_deterministic_hit_id(
                        loser.strategy_version_id,
                        loser.object_uid,
                        loser.source_batch_id,
                    ),
                    suppression_reason="SUPPRESSED_BY_HIGHER_PRIORITY",
                    winner_strategy_id=winner.strategy_id,
                )
        return result

    def _persist_hits(self, target_uid: str, outcomes: Sequence[EvaluationOutcome]) -> int:
        hit_rows = [outcome for outcome in outcomes if outcome.result == HIT]
        if not hit_rows:
            return 0

        def work(conn):
            inserted = 0
            for outcome in hit_rows:
                hit_id = outcome.hit_id or _deterministic_hit_id(
                    outcome.strategy_version_id,
                    outcome.object_uid,
                    outcome.source_batch_id,
                )
                condition_snapshot = [
                    {
                        "field": item.field,
                        "op": item.operator,
                        "expected": item.expected,
                        "actual": item.actual,
                        "result": item.result,
                    }
                    for item in outcome.conditions
                ]
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO strategy_hit(
                       hit_id,strategy_id,strategy_version_id,target_uid,object_type,object_uid,
                       advertiser_id,ad_id,material_id,control_task_id,evaluated_at,source_collected_at,
                       source_batch_id,result,condition_snapshot_json,metric_snapshot_json,
                       suppression_reason,winner_strategy_id
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'HIT',?,?,?,?,?)""",
                    (
                        hit_id,
                        outcome.strategy_id,
                        outcome.strategy_version_id,
                        target_uid,
                        outcome.object_type,
                        outcome.object_uid,
                        outcome.advertiser_id,
                        outcome.ad_id,
                        outcome.material_id,
                        outcome.control_task_id,
                        outcome.source_collected_at,
                        outcome.source_collected_at,
                        outcome.source_batch_id,
                        _json(condition_snapshot),
                        _json(dict(outcome.metric_snapshot)),
                        outcome.suppression_reason,
                        outcome.winner_strategy_id,
                    ),
                )
                if cursor.rowcount == 1:
                    inserted += 1
            return inserted

        return int(self._writer.transaction(work).result(timeout=10))
