from __future__ import annotations

import json
from pathlib import Path

import pytest

from commercial_v1.strategy import (
    HIT,
    NOT_EVALUABLE,
    NOT_HIT,
    StrategyEvaluationService,
    StrategyEvaluator,
    StrategyStore,
    StrategyVersion,
)
from commercial_v1.storage.database import Database, DatabaseConfig
from commercial_v1.storage.schema import create_schema_v1
from commercial_v1.storage.writer import StorageWriter


NOW = "2026-08-30T08:00:00+00:00"


def _env(tmp_path: Path):
    db = Database(DatabaseConfig(tmp_path / "runtime.db"))
    with db.connect() as conn:
        create_schema_v1(conn)
    writer = StorageWriter(db)
    writer.start()
    writer.execute(
        """INSERT INTO qianchuan_account(
           account_uid,advertiser_id,enabled,auth_status,created_at,updated_at
           ) VALUES('acc','111111',1,'ACTIVE',?,?)""",
        (NOW, NOW),
    ).result(timeout=5)
    writer.execute(
        """INSERT INTO monitor_plan(
           target_uid,account_uid,advertiser_id,ad_id,plan_name,plan_system,promotion_scene,
           official_status,monitor_enabled,lifecycle_state,collection_active,strategy_eligible,
           write_eligible,sync_state,created_at,updated_at
           ) VALUES('target','acc','111111','222222','plan','UNI_PROJECT','VIDEO_PROM_GOODS',
                    'DELIVERY_OK',1,'ACTIVE_COLLECTING',1,1,1,'TRUSTED',?,?)""",
        (NOW, NOW),
    ).result(timeout=5)
    store = StrategyStore(db, writer)
    service = StrategyEvaluationService(db, writer, store)
    return db, writer, store, service


def _version(conditions):
    return StrategyVersion(
        strategy_id="s",
        strategy_name="s",
        strategy_type="MATERIAL_RETARGET",
        object_type="MATERIAL",
        target_scope="PLAN:target",
        action_type="CREATE_RETARGET",
        execution_mode="MANUAL",
        enabled=True,
        strategy_version_id="v",
        version_no=1,
        conditions={"logic": "AND", "conditions": conditions},
        action_config={},
        grouping_mode="SEPARATE",
        priority=10,
        content_hash="h",
    )


def _condition(field: str, op: str, value: str):
    return {"field": field, "op": op, "value": value}


def _create_material_batch(db: Database, writer: StorageWriter, *, batch_id="material-batch", cost="120", roi="1.5"):
    writer.execute(
        """INSERT INTO collection_batch(
           batch_id,account_uid,target_uid,advertiser_id,ad_id,pipeline_type,scheduled_at,
           started_at,finished_at,status,raw_row_count,unique_row_count,created_at
           ) VALUES(?,?,?,?,?,'MATERIAL_5M',?,?,?,'SUCCESS',1,1,?)""",
        (batch_id, "acc", "target", "111111", "222222", NOW, NOW, NOW, NOW),
    ).result(timeout=5)
    writer.execute(
        """INSERT INTO material_registry(
           material_uid,advertiser_id,ad_id,material_id,first_seen_at,last_seen_at,first_active_at,
           last_active_at,last_official_status,created_at,updated_at
           ) VALUES('material:111111:222222:900001','111111','222222','900001',?,?,?,?,
                    'DELIVERY_OK',?,?)""",
        (NOW, NOW, NOW, NOW, NOW, NOW),
    ).result(timeout=5)
    writer.execute(
        """INSERT INTO material_latest(
           material_uid,advertiser_id,ad_id,material_id,official_material_status,official_audit_status,
           overall_cost_decimal,net_settle_amount_decimal,net_settle_roi_decimal,net_settle_order_count,
           overall_order_count,overall_gmv_decimal,overall_pay_roi_decimal,stat_date,collected_at,batch_id,
           request_id,sync_state,strategy_eligible,updated_at
           ) VALUES('material:111111:222222:900001','111111','222222','900001','DELIVERY_OK','PASS',
                    ?, '150', ?,2,3,'200','1.66','2026-08-30',?,?, 'rid','TRUSTED',1,?)""",
        (cost, roi, NOW, batch_id, NOW),
    ).result(timeout=5)


def _create_control_batch(db: Database, writer: StorageWriter, *, batch_id="control-batch", cost="120"):
    writer.execute(
        """INSERT INTO collection_batch(
           batch_id,account_uid,target_uid,advertiser_id,ad_id,pipeline_type,scheduled_at,
           started_at,finished_at,status,raw_row_count,unique_row_count,created_at
           ) VALUES(?,?,?,?,?,'CONTROL_5M',?,?,?,'SUCCESS',1,1,?)""",
        (batch_id, "acc", "target", "111111", "222222", NOW, NOW, NOW, NOW),
    ).result(timeout=5)
    writer.execute(
        """INSERT INTO control_task_registry(
           control_task_uid,advertiser_id,ad_id,control_task_id,scene,task_name,first_seen_at,last_seen_at,
           first_processing_at,last_official_status,material_count,created_at,updated_at
           ) VALUES('control:111111:222222:700001','111111','222222','700001','MATERIAL_ADD_BUDGET',
                    'task',?,?,?,'PROCESSING',1,?,?)""",
        (NOW, NOW, NOW, NOW, NOW),
    ).result(timeout=5)
    writer.execute(
        """INSERT INTO control_task_latest(
           control_task_uid,advertiser_id,ad_id,control_task_id,official_task_status,budget_decimal,duration_decimal,
           assist_cost_decimal,assist_order_count,assist_gmv_decimal,assist_pay_roi_decimal,
           assist_net_amount_decimal,assist_net_roi_decimal,assist_net_order_count,stat_start_time,stat_end_time,
           stat_date,collected_at,batch_id,request_id,sync_state,strategy_eligible,write_eligible,updated_at
           ) VALUES('control:111111:222222:700001','111111','222222','700001','PROCESSING','500','2',
                    ?,2,'300','2.5','250','2.08',1,?,?, '2026-08-30',?,?,'rid','TRUSTED',1,1,?)""",
        (cost, "2026-08-30 00:00:00", "2026-08-30 16:00:00", NOW, batch_id, NOW),
    ).result(timeout=5)


def test_and_tristate_null_never_becomes_zero() -> None:
    evaluator = StrategyEvaluator()
    result, outcomes = evaluator.evaluate(
        _version(
            [
                _condition("overall_cost_decimal", "GTE", "100"),
                _condition("net_settle_roi_decimal", "LT", "2"),
            ]
        ),
        {"overall_cost_decimal": "120", "net_settle_roi_decimal": None},
    )
    assert result == NOT_EVALUABLE
    assert outcomes[1].actual is None
    assert outcomes[1].result == NOT_EVALUABLE

    # AND 中只要已有确定 false，整体就是 NOT_HIT；未知值不能把 false 改成 HIT。
    result, _ = evaluator.evaluate(
        _version(
            [
                _condition("overall_cost_decimal", "GT", "500"),
                _condition("net_settle_roi_decimal", "LT", "2"),
            ]
        ),
        {"overall_cost_decimal": "120", "net_settle_roi_decimal": None},
    )
    assert result == NOT_HIT


def test_strategy_store_rejects_or_and_untrusted_fields(tmp_path: Path) -> None:
    _, writer, store, _ = _env(tmp_path)
    try:
        with pytest.raises(ValueError, match="AND"):
            store.create_strategy(
                strategy_name="bad-or",
                strategy_type="MATERIAL_RETARGET",
                target_uid="target",
                execution_mode="MANUAL",
                priority=1,
                conditions={"logic": "OR", "conditions": [_condition("overall_cost_decimal", "GT", "1")]},
            )
        with pytest.raises(ValueError, match="trusted V1 metrics"):
            store.create_strategy(
                strategy_name="bad-field",
                strategy_type="MATERIAL_RETARGET",
                target_uid="target",
                execution_mode="MANUAL",
                priority=1,
                conditions={"logic": "AND", "conditions": [_condition("basic_cost_decimal", "GT", "1")]},
            )
        with pytest.raises(ValueError, match="unsupported V1"):
            store.create_strategy(
                strategy_name="product-level",
                strategy_type="PRODUCT_RETARGET",
                target_uid="target",
                execution_mode="MANUAL",
                priority=1,
                conditions={"logic": "AND", "conditions": [_condition("overall_cost_decimal", "GT", "1")]},
            )
    finally:
        writer.close()


def test_strategy_version_is_immutable(tmp_path: Path) -> None:
    db, writer, store, _ = _env(tmp_path)
    try:
        v1 = store.create_strategy(
            strategy_name="retarget",
            strategy_type="MATERIAL_RETARGET",
            target_uid="target",
            execution_mode="MANUAL",
            priority=10,
            conditions={"logic": "AND", "conditions": [_condition("overall_cost_decimal", "GTE", "100")]},
            action_config={"budget": "500"},
        )
        v2 = store.create_new_version(
            v1.strategy_id,
            priority=20,
            conditions={"logic": "AND", "conditions": [_condition("overall_cost_decimal", "GTE", "200")]},
        )
        assert v2.version_no == 2
        assert v2.strategy_version_id != v1.strategy_version_id
        with db.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT version_no,condition_json,priority FROM strategy_version WHERE strategy_id=? ORDER BY version_no",
                (v1.strategy_id,),
            ).fetchall()
            assert len(rows) == 2
            assert json.loads(rows[0]["condition_json"])["conditions"][0]["value"] == "100"
            assert rows[0]["priority"] == 10
            assert json.loads(rows[1]["condition_json"])["conditions"][0]["value"] == "200"
            assert rows[1]["priority"] == 20
    finally:
        writer.close()


def test_same_action_priority_arbitration_persists_winner_and_suppressed_hit(tmp_path: Path) -> None:
    db, writer, store, service = _env(tmp_path)
    try:
        _create_material_batch(db, writer)
        high = store.create_strategy(
            strategy_name="high",
            strategy_type="MATERIAL_RETARGET",
            target_uid="target",
            execution_mode="MANUAL",
            priority=100,
            conditions={"logic": "AND", "conditions": [_condition("overall_cost_decimal", "GTE", "100")]},
        )
        low = store.create_strategy(
            strategy_name="low",
            strategy_type="MATERIAL_RETARGET",
            target_uid="target",
            execution_mode="AUTO",
            priority=10,
            conditions={"logic": "AND", "conditions": [_condition("net_settle_roi_decimal", "LT", "2")]},
        )
        result = service.evaluate_material_batch("target", "material-batch")
        assert result.hit == 2
        assert result.persisted_hits == 2
        assert result.suppressed_hits == 1
        winners = [item for item in result.outcomes if item.result == HIT and not item.suppression_reason]
        assert [item.strategy_id for item in winners] == [high.strategy_id]
        losers = [item for item in result.outcomes if item.suppression_reason]
        assert losers[0].strategy_id == low.strategy_id
        assert losers[0].winner_strategy_id == high.strategy_id

        # 同一 batch 重复求值幂等，不重复落 HIT。
        second = service.evaluate_material_batch("target", "material-batch")
        assert second.persisted_hits == 0
        with db.connect(readonly=True) as conn:
            rows = conn.execute("SELECT * FROM strategy_hit ORDER BY strategy_id").fetchall()
            assert len(rows) == 2
            assert {row["source_batch_id"] for row in rows} == {"material-batch"}
            assert {row["source_collected_at"] for row in rows} == {NOW}
            assert conn.execute("SELECT COUNT(*) FROM candidate_batch").fetchone()[0] == 0
    finally:
        writer.close()


def test_equal_priority_is_deterministic_by_strategy_id(tmp_path: Path) -> None:
    _, writer, store, service = _env(tmp_path)
    try:
        _create_material_batch(service._database, writer)  # type: ignore[attr-defined]
        first = store.create_strategy(
            strategy_name="a",
            strategy_type="MATERIAL_RETARGET",
            target_uid="target",
            execution_mode="MANUAL",
            priority=50,
            conditions={"logic": "AND", "conditions": [_condition("overall_cost_decimal", "GT", "1")]},
        )
        second = store.create_strategy(
            strategy_name="b",
            strategy_type="MATERIAL_RETARGET",
            target_uid="target",
            execution_mode="MANUAL",
            priority=50,
            conditions={"logic": "AND", "conditions": [_condition("overall_cost_decimal", "GT", "1")]},
        )
        summary = service.evaluate_material_batch("target", "material-batch")
        winner = next(item for item in summary.outcomes if item.result == HIT and not item.suppression_reason)
        assert winner.strategy_id == min(first.strategy_id, second.strategy_id)
    finally:
        writer.close()


def test_different_control_actions_do_not_suppress_each_other(tmp_path: Path) -> None:
    db, writer, store, service = _env(tmp_path)
    try:
        _create_control_batch(db, writer)
        stop = store.create_strategy(
            strategy_name="stop",
            strategy_type="CONTROL_STOP",
            target_uid="target",
            execution_mode="MANUAL",
            priority=100,
            conditions={"logic": "AND", "conditions": [_condition("assist_cost_decimal", "GTE", "100")]},
        )
        budget = store.create_strategy(
            strategy_name="budget",
            strategy_type="CONTROL_BUDGET_INCREASE",
            target_uid="target",
            execution_mode="AUTO",
            priority=1,
            conditions={"logic": "AND", "conditions": [_condition("assist_pay_roi_decimal", "GTE", "2")]},
        )
        summary = service.evaluate_control_batch("target", "control-batch")
        assert summary.hit == 2
        assert summary.suppressed_hits == 0
        by_id = {item.strategy_id: item for item in summary.outcomes}
        assert by_id[stop.strategy_id].winner_strategy_id == stop.strategy_id
        assert by_id[budget.strategy_id].winner_strategy_id == budget.strategy_id
    finally:
        writer.close()
