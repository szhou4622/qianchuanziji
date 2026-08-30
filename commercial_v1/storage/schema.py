"""商业版 Schema V1。

核心约束：新库独立；官方/本地状态分列；指标缺失保留 NULL；V1 不创建商品级策略表。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA_V1_SQL = r"""
CREATE TABLE IF NOT EXISTS schema_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS schema_migration_log(migration_id TEXT PRIMARY KEY,from_version INTEGER NOT NULL,to_version INTEGER NOT NULL,app_version TEXT NOT NULL,started_at TEXT NOT NULL,finished_at TEXT,status TEXT NOT NULL,backup_path_hash TEXT,error_message TEXT,validation_json TEXT);

CREATE TABLE IF NOT EXISTS qianchuan_auth_profile(auth_profile_id TEXT PRIMARY KEY,app_id TEXT NOT NULL,encrypted_app_secret TEXT,encrypted_access_token TEXT,encrypted_refresh_token TEXT,access_token_expires_at TEXT,refresh_token_expires_at TEXT,auth_status TEXT NOT NULL,last_refresh_at TEXT,last_error_code TEXT,last_error_message TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS license_runtime_state(singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),status TEXT NOT NULL,last_online_verified_at TEXT,first_network_failure_at TEXT,network_grace_until TEXT,last_error_code TEXT,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS qianchuan_account(account_uid TEXT PRIMARY KEY,advertiser_id TEXT NOT NULL UNIQUE,account_name TEXT,account_type TEXT,enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN(0,1)),auth_status TEXT NOT NULL,capability_json TEXT,last_auth_ok_at TEXT,last_seen_at TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS qianchuan_account_auth(account_uid TEXT NOT NULL REFERENCES qianchuan_account(account_uid),auth_profile_id TEXT NOT NULL REFERENCES qianchuan_auth_profile(auth_profile_id),is_primary INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN(0,1)),bound_at TEXT NOT NULL,last_verified_at TEXT,created_at TEXT NOT NULL,PRIMARY KEY(account_uid,auth_profile_id));
CREATE UNIQUE INDEX IF NOT EXISTS ux_qianchuan_account_auth_primary ON qianchuan_account_auth(account_uid) WHERE is_primary=1;

CREATE TABLE IF NOT EXISTS monitor_plan(target_uid TEXT PRIMARY KEY,account_uid TEXT NOT NULL REFERENCES qianchuan_account(account_uid),advertiser_id TEXT NOT NULL,ad_id TEXT NOT NULL,plan_name TEXT,plan_system TEXT NOT NULL,promotion_scene TEXT NOT NULL,official_status TEXT,monitor_enabled INTEGER NOT NULL DEFAULT 1 CHECK(monitor_enabled IN(0,1)),lifecycle_state TEXT NOT NULL,collection_active INTEGER NOT NULL DEFAULT 0 CHECK(collection_active IN(0,1)),strategy_eligible INTEGER NOT NULL DEFAULT 0 CHECK(strategy_eligible IN(0,1)),write_eligible INTEGER NOT NULL DEFAULT 0 CHECK(write_eligible IN(0,1)),sync_state TEXT NOT NULL,budget_cent INTEGER,official_modify_time TEXT,last_status_check_at TEXT,next_status_check_at TEXT,last_hot_collect_at TEXT,next_hot_collect_at TEXT,last_catalog_seen_at TEXT,last_active_at TEXT,terminal_at TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(advertiser_id,ad_id));
CREATE INDEX IF NOT EXISTS idx_monitor_plan_account_state ON monitor_plan(advertiser_id,monitor_enabled,lifecycle_state);

CREATE TABLE IF NOT EXISTS collection_batch(batch_id TEXT PRIMARY KEY,account_uid TEXT REFERENCES qianchuan_account(account_uid),target_uid TEXT REFERENCES monitor_plan(target_uid),advertiser_id TEXT,ad_id TEXT,pipeline_type TEXT NOT NULL,scheduled_at TEXT,started_at TEXT NOT NULL,finished_at TEXT,status TEXT NOT NULL,expected_pages INTEGER,successful_pages INTEGER NOT NULL DEFAULT 0,raw_row_count INTEGER NOT NULL DEFAULT 0,unique_row_count INTEGER NOT NULL DEFAULT 0,request_ids_json TEXT,response_fingerprint TEXT,error_type TEXT,error_code TEXT,error_message TEXT,created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_collection_target_pipeline_time ON collection_batch(target_uid,pipeline_type,started_at DESC);
CREATE INDEX IF NOT EXISTS idx_collection_status_time ON collection_batch(status,started_at DESC);

CREATE TABLE IF NOT EXISTS material_registry(material_uid TEXT PRIMARY KEY,advertiser_id TEXT NOT NULL,ad_id TEXT NOT NULL,material_id TEXT NOT NULL,video_id TEXT,title TEXT,first_seen_at TEXT NOT NULL,last_seen_at TEXT NOT NULL,first_active_at TEXT,last_active_at TEXT,last_official_status TEXT,ended_at TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(advertiser_id,ad_id,material_id));
CREATE TABLE IF NOT EXISTS material_latest(material_uid TEXT PRIMARY KEY REFERENCES material_registry(material_uid),advertiser_id TEXT NOT NULL,ad_id TEXT NOT NULL,material_id TEXT NOT NULL,official_material_status TEXT,official_audit_status TEXT,overall_cost_cent INTEGER,net_settle_amount_cent INTEGER,net_settle_roi TEXT,net_settle_order_count INTEGER,overall_order_count INTEGER,overall_gmv_cent INTEGER,overall_pay_roi TEXT,stat_date TEXT NOT NULL,collected_at TEXT NOT NULL,batch_id TEXT NOT NULL REFERENCES collection_batch(batch_id),request_id TEXT,sync_state TEXT NOT NULL,strategy_eligible INTEGER NOT NULL DEFAULT 0 CHECK(strategy_eligible IN(0,1)),updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS material_5m(snapshot_id TEXT PRIMARY KEY,material_uid TEXT NOT NULL REFERENCES material_registry(material_uid),advertiser_id TEXT NOT NULL,ad_id TEXT NOT NULL,material_id TEXT NOT NULL,scheduled_at TEXT NOT NULL,collected_at TEXT NOT NULL,stat_date TEXT NOT NULL,overall_cost_cent INTEGER,net_settle_amount_cent INTEGER,net_settle_roi TEXT,net_settle_order_count INTEGER,overall_order_count INTEGER,overall_gmv_cent INTEGER,overall_pay_roi TEXT,official_material_status TEXT,official_audit_status TEXT,batch_id TEXT NOT NULL REFERENCES collection_batch(batch_id),snapshot_reason TEXT NOT NULL,UNIQUE(material_uid,scheduled_at,snapshot_reason));
CREATE INDEX IF NOT EXISTS idx_material_5m_object_time ON material_5m(material_uid,scheduled_at DESC);
CREATE TABLE IF NOT EXISTS material_daily(advertiser_id TEXT NOT NULL,ad_id TEXT NOT NULL,material_id TEXT NOT NULL,business_date TEXT NOT NULL,overall_cost_cent INTEGER,net_settle_amount_cent INTEGER,net_settle_roi TEXT,net_settle_order_count INTEGER,overall_order_count INTEGER,overall_gmv_cent INTEGER,overall_pay_roi TEXT,data_status TEXT NOT NULL,revision_no INTEGER NOT NULL DEFAULT 1,finalized_at TEXT,batch_id TEXT REFERENCES collection_batch(batch_id),request_id TEXT,updated_at TEXT NOT NULL,PRIMARY KEY(advertiser_id,ad_id,material_id,business_date));
CREATE INDEX IF NOT EXISTS idx_material_daily_plan_date ON material_daily(advertiser_id,ad_id,business_date DESC);
CREATE TABLE IF NOT EXISTS material_monthly(advertiser_id TEXT NOT NULL,ad_id TEXT NOT NULL,material_id TEXT NOT NULL,business_month TEXT NOT NULL,overall_cost_cent INTEGER,net_settle_amount_cent INTEGER,net_settle_roi TEXT,net_settle_order_count INTEGER,overall_order_count INTEGER,overall_gmv_cent INTEGER,overall_pay_roi TEXT,data_status TEXT NOT NULL,revision_no INTEGER NOT NULL DEFAULT 1,finalized_at TEXT,batch_id TEXT REFERENCES collection_batch(batch_id),request_id TEXT,updated_at TEXT NOT NULL,PRIMARY KEY(advertiser_id,ad_id,material_id,business_month));

CREATE TABLE IF NOT EXISTS material_topic_metric_latest(topic_metric_uid TEXT PRIMARY KEY,advertiser_id TEXT NOT NULL,plan_system TEXT NOT NULL,promotion_scene TEXT NOT NULL,material_id TEXT NOT NULL,stat_date TEXT NOT NULL,topic_name TEXT NOT NULL,basic_cost_cent INTEGER,comprehensive_marketing_roi TEXT,collected_at TEXT NOT NULL,request_id TEXT,batch_id TEXT REFERENCES collection_batch(batch_id),UNIQUE(advertiser_id,plan_system,promotion_scene,material_id,stat_date,topic_name));
CREATE TABLE IF NOT EXISTS material_topic_metric_daily(advertiser_id TEXT NOT NULL,plan_system TEXT NOT NULL,promotion_scene TEXT NOT NULL,material_id TEXT NOT NULL,business_date TEXT NOT NULL,topic_name TEXT NOT NULL,basic_cost_cent INTEGER,comprehensive_marketing_roi TEXT,data_status TEXT NOT NULL,revision_no INTEGER NOT NULL DEFAULT 1,finalized_at TEXT,batch_id TEXT REFERENCES collection_batch(batch_id),request_id TEXT,updated_at TEXT NOT NULL,PRIMARY KEY(advertiser_id,plan_system,promotion_scene,material_id,business_date,topic_name));
CREATE TABLE IF NOT EXISTS material_topic_metric_monthly(advertiser_id TEXT NOT NULL,plan_system TEXT NOT NULL,promotion_scene TEXT NOT NULL,material_id TEXT NOT NULL,business_month TEXT NOT NULL,topic_name TEXT NOT NULL,basic_cost_cent INTEGER,comprehensive_marketing_roi TEXT,data_status TEXT NOT NULL,revision_no INTEGER NOT NULL DEFAULT 1,finalized_at TEXT,batch_id TEXT REFERENCES collection_batch(batch_id),request_id TEXT,updated_at TEXT NOT NULL,PRIMARY KEY(advertiser_id,plan_system,promotion_scene,material_id,business_month,topic_name));

CREATE TABLE IF NOT EXISTS control_task_registry(control_task_uid TEXT PRIMARY KEY,advertiser_id TEXT NOT NULL,ad_id TEXT NOT NULL,control_task_id TEXT NOT NULL,scene TEXT,task_name TEXT,create_time TEXT,first_seen_at TEXT NOT NULL,last_seen_at TEXT NOT NULL,first_processing_at TEXT,ended_at TEXT,last_official_status TEXT,material_count INTEGER NOT NULL DEFAULT 0,created_by_tool INTEGER NOT NULL DEFAULT 0 CHECK(created_by_tool IN(0,1)),external_reactivated INTEGER NOT NULL DEFAULT 0 CHECK(external_reactivated IN(0,1)),auto_manage INTEGER NOT NULL DEFAULT 0 CHECK(auto_manage IN(0,1)),created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(advertiser_id,ad_id,control_task_id));
CREATE TABLE IF NOT EXISTS control_task_material(control_task_uid TEXT NOT NULL REFERENCES control_task_registry(control_task_uid),material_uid TEXT REFERENCES material_registry(material_uid),material_id TEXT NOT NULL,observed_at TEXT NOT NULL,PRIMARY KEY(control_task_uid,material_id));
CREATE TABLE IF NOT EXISTS control_task_latest(control_task_uid TEXT PRIMARY KEY REFERENCES control_task_registry(control_task_uid),advertiser_id TEXT NOT NULL,ad_id TEXT NOT NULL,control_task_id TEXT NOT NULL,official_task_status TEXT,budget_cent INTEGER,duration_seconds INTEGER,bid_decimal TEXT,roi_goal_decimal TEXT,assist_cost_cent INTEGER,assist_order_count INTEGER,assist_gmv_cent INTEGER,assist_pay_roi TEXT,assist_net_amount_cent INTEGER,assist_net_roi TEXT,assist_net_order_count INTEGER,stat_start_time TEXT,stat_end_time TEXT,stat_date TEXT NOT NULL,collected_at TEXT NOT NULL,batch_id TEXT NOT NULL REFERENCES collection_batch(batch_id),request_id TEXT,sync_state TEXT NOT NULL,strategy_eligible INTEGER NOT NULL DEFAULT 0 CHECK(strategy_eligible IN(0,1)),write_eligible INTEGER NOT NULL DEFAULT 0 CHECK(write_eligible IN(0,1)),updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS control_task_5m(snapshot_id TEXT PRIMARY KEY,control_task_uid TEXT NOT NULL REFERENCES control_task_registry(control_task_uid),advertiser_id TEXT NOT NULL,ad_id TEXT NOT NULL,control_task_id TEXT NOT NULL,scheduled_at TEXT NOT NULL,stat_start_time TEXT,stat_end_time TEXT,stat_date TEXT NOT NULL,assist_cost_cent INTEGER,assist_order_count INTEGER,assist_gmv_cent INTEGER,assist_pay_roi TEXT,assist_net_amount_cent INTEGER,assist_net_roi TEXT,assist_net_order_count INTEGER,official_task_status TEXT,batch_id TEXT NOT NULL REFERENCES collection_batch(batch_id),snapshot_reason TEXT NOT NULL,UNIQUE(control_task_uid,scheduled_at,snapshot_reason));
CREATE INDEX IF NOT EXISTS idx_control_5m_task_time ON control_task_5m(control_task_uid,scheduled_at DESC);
CREATE TABLE IF NOT EXISTS control_task_daily(advertiser_id TEXT NOT NULL,ad_id TEXT NOT NULL,control_task_id TEXT NOT NULL,business_date TEXT NOT NULL,assist_cost_cent INTEGER,assist_order_count INTEGER,assist_gmv_cent INTEGER,assist_pay_roi TEXT,assist_net_amount_cent INTEGER,assist_net_roi TEXT,assist_net_order_count INTEGER,data_status TEXT NOT NULL,revision_no INTEGER NOT NULL DEFAULT 1,finalized_at TEXT,batch_id TEXT REFERENCES collection_batch(batch_id),request_id TEXT,updated_at TEXT NOT NULL,PRIMARY KEY(advertiser_id,ad_id,control_task_id,business_date));
CREATE TABLE IF NOT EXISTS control_task_monthly(advertiser_id TEXT NOT NULL,ad_id TEXT NOT NULL,control_task_id TEXT NOT NULL,business_month TEXT NOT NULL,assist_cost_cent INTEGER,assist_order_count INTEGER,assist_gmv_cent INTEGER,assist_pay_roi TEXT,assist_net_amount_cent INTEGER,assist_net_roi TEXT,assist_net_order_count INTEGER,data_status TEXT NOT NULL,revision_no INTEGER NOT NULL DEFAULT 1,finalized_at TEXT,batch_id TEXT REFERENCES collection_batch(batch_id),request_id TEXT,updated_at TEXT NOT NULL,PRIMARY KEY(advertiser_id,ad_id,control_task_id,business_month));

CREATE TABLE IF NOT EXISTS strategy_config(strategy_id TEXT PRIMARY KEY,strategy_name TEXT NOT NULL,strategy_type TEXT NOT NULL,target_scope TEXT NOT NULL,action_type TEXT NOT NULL,execution_mode TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN(0,1)),priority INTEGER NOT NULL,current_version_id TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS strategy_version(strategy_version_id TEXT PRIMARY KEY,strategy_id TEXT NOT NULL REFERENCES strategy_config(strategy_id),version_no INTEGER NOT NULL,condition_json TEXT NOT NULL,action_config_json TEXT NOT NULL,grouping_mode TEXT NOT NULL,priority INTEGER NOT NULL,created_at TEXT NOT NULL,created_by TEXT,content_hash TEXT NOT NULL,UNIQUE(strategy_id,version_no));
CREATE TABLE IF NOT EXISTS strategy_hit(hit_id TEXT PRIMARY KEY,strategy_id TEXT NOT NULL REFERENCES strategy_config(strategy_id),strategy_version_id TEXT NOT NULL REFERENCES strategy_version(strategy_version_id),target_uid TEXT REFERENCES monitor_plan(target_uid),object_type TEXT NOT NULL,object_uid TEXT NOT NULL,advertiser_id TEXT NOT NULL,ad_id TEXT NOT NULL,material_id TEXT,control_task_id TEXT,evaluated_at TEXT NOT NULL,source_collected_at TEXT NOT NULL,source_batch_id TEXT REFERENCES collection_batch(batch_id),result TEXT NOT NULL,condition_snapshot_json TEXT NOT NULL,metric_snapshot_json TEXT NOT NULL,suppression_reason TEXT,winner_strategy_id TEXT);

CREATE TABLE IF NOT EXISTS candidate_batch(candidate_id TEXT PRIMARY KEY,strategy_id TEXT REFERENCES strategy_config(strategy_id),strategy_version_id TEXT REFERENCES strategy_version(strategy_version_id),action_type TEXT NOT NULL,advertiser_id TEXT NOT NULL,ad_id TEXT NOT NULL,execution_mode TEXT NOT NULL,grouping_mode TEXT NOT NULL,execution_params_json TEXT NOT NULL,group_fingerprint TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,expires_at TEXT,approved_at TEXT,rejected_at TEXT,reject_cooldown_until TEXT,cancelled_at TEXT,cancel_reason TEXT);
CREATE TABLE IF NOT EXISTS candidate_item(candidate_item_id TEXT PRIMARY KEY,candidate_id TEXT NOT NULL REFERENCES candidate_batch(candidate_id),hit_id TEXT REFERENCES strategy_hit(hit_id),object_uid TEXT NOT NULL,material_id TEXT,control_task_id TEXT,metric_snapshot_json TEXT NOT NULL,before_state_json TEXT,created_at TEXT NOT NULL,UNIQUE(candidate_id,object_uid));

CREATE TABLE IF NOT EXISTS execution_task(execution_id TEXT PRIMARY KEY,candidate_id TEXT REFERENCES candidate_batch(candidate_id),strategy_id TEXT REFERENCES strategy_config(strategy_id),strategy_version_id TEXT REFERENCES strategy_version(strategy_version_id),advertiser_id TEXT NOT NULL,ad_id TEXT NOT NULL,action_type TEXT NOT NULL,execution_mode TEXT NOT NULL,status TEXT NOT NULL,expected_before_json TEXT,expected_after_json TEXT,execution_params_json TEXT NOT NULL,control_task_id TEXT,external_object_id TEXT,created_at TEXT NOT NULL,approved_at TEXT,submitted_at TEXT,confirmed_at TEXT,cancelled_at TEXT,cancel_reason TEXT,last_error_code TEXT,last_error_message TEXT);
CREATE INDEX IF NOT EXISTS idx_execution_status_created ON execution_task(status,created_at);
CREATE INDEX IF NOT EXISTS idx_execution_object ON execution_task(advertiser_id,ad_id,action_type,status);
CREATE TABLE IF NOT EXISTS execution_task_material(execution_id TEXT NOT NULL REFERENCES execution_task(execution_id),material_uid TEXT REFERENCES material_registry(material_uid),material_id TEXT NOT NULL,hit_id TEXT REFERENCES strategy_hit(hit_id),candidate_item_id TEXT REFERENCES candidate_item(candidate_item_id),PRIMARY KEY(execution_id,material_id));
CREATE TABLE IF NOT EXISTS execution_attempt(attempt_id TEXT PRIMARY KEY,execution_id TEXT NOT NULL REFERENCES execution_task(execution_id),attempt_no INTEGER NOT NULL CHECK(attempt_no>=1),endpoint TEXT NOT NULL,request_hash TEXT NOT NULL,request_summary_json TEXT NOT NULL,started_at TEXT NOT NULL,request_sent_at TEXT,response_received_at TEXT,transport_status TEXT NOT NULL,http_status INTEGER,api_code TEXT,request_id TEXT,response_summary_json TEXT,outcome TEXT NOT NULL,error_message TEXT,UNIQUE(execution_id,attempt_no));
CREATE TABLE IF NOT EXISTS execution_reconciliation(reconciliation_id TEXT PRIMARY KEY,execution_id TEXT NOT NULL REFERENCES execution_task(execution_id),action_type TEXT NOT NULL,control_task_id TEXT,expected_state_json TEXT NOT NULL,status TEXT NOT NULL,attempt_count INTEGER NOT NULL DEFAULT 0,last_checked_at TEXT,next_check_at TEXT,evidence_json TEXT,request_ids_json TEXT,resolved_at TEXT,error_message TEXT);
CREATE INDEX IF NOT EXISTS idx_reconciliation_due ON execution_reconciliation(status,next_check_at);

CREATE TABLE IF NOT EXISTS background_job(job_uid TEXT PRIMARY KEY,job_type TEXT NOT NULL,priority INTEGER NOT NULL DEFAULT 100,payload_json TEXT NOT NULL,status TEXT NOT NULL,due_at TEXT NOT NULL,lease_owner TEXT,lease_expires_at TEXT,fencing_token INTEGER,progress_current INTEGER NOT NULL DEFAULT 0,progress_total INTEGER NOT NULL DEFAULT 0,progress_message TEXT,created_at TEXT NOT NULL,started_at TEXT,finished_at TEXT,updated_at TEXT NOT NULL,result_json TEXT,error_message TEXT);
CREATE INDEX IF NOT EXISTS idx_job_claim ON background_job(status,due_at,priority);
CREATE TABLE IF NOT EXISTS task_lease(resource_key TEXT PRIMARY KEY,owner_instance_id TEXT NOT NULL,task_uid TEXT NOT NULL,priority INTEGER NOT NULL,acquired_at TEXT NOT NULL,heartbeat_at TEXT NOT NULL,expires_at TEXT NOT NULL,fencing_token INTEGER NOT NULL CHECK(fencing_token>=1));

CREATE TABLE IF NOT EXISTS api_error_event(error_id TEXT PRIMARY KEY,module TEXT NOT NULL,error_scope TEXT NOT NULL,advertiser_id TEXT,ad_id TEXT,material_id TEXT,control_task_id TEXT,endpoint TEXT,http_status INTEGER,api_code TEXT,request_id TEXT,error_type TEXT NOT NULL,message TEXT NOT NULL,retryable INTEGER NOT NULL DEFAULT 0 CHECK(retryable IN(0,1)),blocked_capabilities_json TEXT,occurred_at TEXT NOT NULL,resolved_at TEXT);
CREATE TABLE IF NOT EXISTS external_change_event(change_id TEXT PRIMARY KEY,advertiser_id TEXT NOT NULL,ad_id TEXT,object_type TEXT NOT NULL,object_id TEXT NOT NULL,change_type TEXT NOT NULL,before_json TEXT,after_json TEXT NOT NULL,detected_at TEXT NOT NULL,source TEXT NOT NULL,related_execution_id TEXT REFERENCES execution_task(execution_id),request_id TEXT);
CREATE TABLE IF NOT EXISTS operation_event(operation_event_id TEXT PRIMARY KEY,advertiser_id TEXT NOT NULL,ad_id TEXT,action_type TEXT NOT NULL,object_type TEXT NOT NULL,object_id TEXT NOT NULL,source TEXT NOT NULL,result TEXT NOT NULL,operator_label TEXT,occurred_at TEXT NOT NULL,execution_id TEXT REFERENCES execution_task(execution_id),request_id TEXT,summary_json TEXT);
CREATE TABLE IF NOT EXISTS feishu_inbox(inbox_id TEXT PRIMARY KEY,event_id TEXT NOT NULL UNIQUE,event_type TEXT NOT NULL,received_at TEXT NOT NULL,payload_redacted_json TEXT NOT NULL,status TEXT NOT NULL,processed_at TEXT,error_message TEXT);
CREATE TABLE IF NOT EXISTS feishu_outbox(outbox_id TEXT PRIMARY KEY,notification_type TEXT NOT NULL,route_id TEXT,related_candidate_id TEXT REFERENCES candidate_batch(candidate_id),related_execution_id TEXT REFERENCES execution_task(execution_id),payload_json TEXT NOT NULL,status TEXT NOT NULL,attempt_count INTEGER NOT NULL DEFAULT 0,next_attempt_at TEXT,claim_owner TEXT,claim_expires_at TEXT,created_at TEXT NOT NULL,sent_at TEXT,last_error_message TEXT);
CREATE TABLE IF NOT EXISTS notification_event(notification_id TEXT PRIMARY KEY,channel TEXT NOT NULL,notification_type TEXT NOT NULL,candidate_id TEXT REFERENCES candidate_batch(candidate_id),execution_id TEXT REFERENCES execution_task(execution_id),delivery_status TEXT NOT NULL,created_at TEXT NOT NULL,delivered_at TEXT,clicked_at TEXT,expired_at TEXT,error_message TEXT);
CREATE TABLE IF NOT EXISTS maintenance_log(maintenance_id TEXT PRIMARY KEY,maintenance_type TEXT NOT NULL,started_at TEXT NOT NULL,finished_at TEXT,status TEXT NOT NULL,before_db_bytes INTEGER,after_db_bytes INTEGER,before_wal_bytes INTEGER,after_wal_bytes INTEGER,disk_free_before INTEGER,disk_free_after INTEGER,rows_deleted INTEGER,details_json TEXT,error_message TEXT);
"""

REQUIRED_TABLES = frozenset({
    "schema_meta","schema_migration_log","qianchuan_auth_profile","license_runtime_state",
    "qianchuan_account","qianchuan_account_auth","monitor_plan","collection_batch",
    "material_registry","material_latest","material_5m","material_daily","material_monthly",
    "material_topic_metric_latest","material_topic_metric_daily","material_topic_metric_monthly",
    "control_task_registry","control_task_material","control_task_latest","control_task_5m",
    "control_task_daily","control_task_monthly","strategy_config","strategy_version","strategy_hit",
    "candidate_batch","candidate_item","execution_task","execution_task_material","execution_attempt",
    "execution_reconciliation","background_job","task_lease","api_error_event","external_change_event",
    "operation_event","feishu_inbox","feishu_outbox","notification_event","maintenance_log"
})


def create_schema_v1(conn: sqlite3.Connection, *, app_version: str = "0.1.0", schema_sql: str | None = None) -> None:
    """在一个事务中创建完整 V1 Schema。

    sqlite3.executescript() 会先结束调用方已有事务，所以 BEGIN 必须放在 script
    自身内部。schema_sql 只用于故障注入测试。
    """
    now = _now()
    sql = SCHEMA_V1_SQL if schema_sql is None else schema_sql
    try:
        conn.executescript("BEGIN IMMEDIATE;\n" + sql)
        conn.execute("INSERT OR REPLACE INTO schema_meta(key,value,updated_at) VALUES(?,?,?)",("schema_version",str(SCHEMA_VERSION),now))
        conn.execute("INSERT OR IGNORE INTO schema_meta(key,value,updated_at) VALUES(?,?,?)",("created_by_app_version",app_version,now))
        conn.execute("INSERT OR IGNORE INTO schema_meta(key,value,updated_at) VALUES(?,?,?)",("last_migrated_at",now,now))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def current_schema_version(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    return int(row[0]) if row is not None else None


def table_names(conn: sqlite3.Connection) -> set[str]:
    return {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
