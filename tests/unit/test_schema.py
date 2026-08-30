import sqlite3
from pathlib import Path

import pytest

from commercial_v1.storage.database import Database, DatabaseConfig
from commercial_v1.storage.schema import REQUIRED_TABLES, SCHEMA_VERSION, create_schema_v1, current_schema_version, table_names


def _new_db(tmp_path: Path):
    db = Database(DatabaseConfig(tmp_path / "runtime.db"))
    conn = db.connect()
    create_schema_v1(conn)
    return conn


def test_schema_v1_creates_all_required_tables(tmp_path: Path) -> None:
    conn = _new_db(tmp_path)
    try:
        assert current_schema_version(conn) == SCHEMA_VERSION
        names = table_names(conn)
        assert REQUIRED_TABLES <= names
        assert "product_identity" not in names
        assert "product_material_relation" not in names
    finally:
        conn.close()


def test_same_material_id_can_exist_in_two_plans(tmp_path: Path) -> None:
    conn = _new_db(tmp_path)
    try:
        now = "2026-08-30T00:00:00Z"
        values = ("adv", "material-1", now, now, now, now)
        conn.execute("INSERT INTO material_registry(material_uid,advertiser_id,ad_id,material_id,first_seen_at,last_seen_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", ("m1", values[0], "plan-a", values[1], *values[2:]))
        conn.execute("INSERT INTO material_registry(material_uid,advertiser_id,ad_id,material_id,first_seen_at,last_seen_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", ("m2", values[0], "plan-b", values[1], *values[2:]))
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM material_registry WHERE material_id='material-1'").fetchone()[0] == 2
    finally:
        conn.close()


def test_metric_columns_default_to_null_not_zero(tmp_path: Path) -> None:
    conn = _new_db(tmp_path)
    try:
        now = "2026-08-30T00:00:00Z"
        conn.execute("INSERT INTO qianchuan_account(account_uid,advertiser_id,enabled,auth_status,created_at,updated_at) VALUES(?,?,?,?,?,?)", ("acc","adv",1,"ACTIVE",now,now))
        conn.execute("INSERT INTO monitor_plan(target_uid,account_uid,advertiser_id,ad_id,plan_system,promotion_scene,monitor_enabled,lifecycle_state,collection_active,strategy_eligible,write_eligible,sync_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("target","acc","adv","plan","UNI","LIVE",1,"ACTIVE_COLLECTING",1,0,0,"FRESH",now,now))
        conn.execute("INSERT INTO collection_batch(batch_id,account_uid,target_uid,advertiser_id,ad_id,pipeline_type,started_at,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)", ("batch","acc","target","adv","plan","MATERIAL",now,"SUCCESS",now))
        conn.execute("INSERT INTO material_registry(material_uid,advertiser_id,ad_id,material_id,first_seen_at,last_seen_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", ("m","adv","plan","material",now,now,now,now))
        conn.execute("INSERT INTO material_latest(material_uid,advertiser_id,ad_id,material_id,stat_date,collected_at,batch_id,sync_state,strategy_eligible,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", ("m","adv","plan","material","2026-08-30",now,"batch","FRESH",0,now))
        row = conn.execute("SELECT overall_cost_cent,net_settle_roi,overall_order_count FROM material_latest WHERE material_uid='m'").fetchone()
        assert tuple(row) == (None, None, None)
    finally:
        conn.close()


def test_execution_attempt_is_unique_per_number(tmp_path: Path) -> None:
    conn = _new_db(tmp_path)
    try:
        now = "2026-08-30T00:00:00Z"
        conn.execute("INSERT INTO execution_task(execution_id,advertiser_id,ad_id,action_type,execution_mode,status,execution_params_json,created_at) VALUES(?,?,?,?,?,?,?,?)", ("e","adv","plan","CREATE_RETARGET","AUTO","SUBMITTING","{}",now))
        values = ("a1","e",1,"/write","hash","{}",now,"NOT_SENT","PENDING")
        conn.execute("INSERT INTO execution_attempt(attempt_id,execution_id,attempt_no,endpoint,request_hash,request_summary_json,started_at,transport_status,outcome) VALUES(?,?,?,?,?,?,?,?,?)", values)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO execution_attempt(attempt_id,execution_id,attempt_no,endpoint,request_hash,request_summary_json,started_at,transport_status,outcome) VALUES(?,?,?,?,?,?,?,?,?)", ("a2",)+values[1:])
    finally:
        conn.close()
