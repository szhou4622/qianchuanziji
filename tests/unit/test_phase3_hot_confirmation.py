from datetime import datetime, timezone
from pathlib import Path

from commercial_v1.qianchuan.client import ApiResponse
from commercial_v1.qianchuan.contracts import CONTROL_TASK_LIST, MATERIAL_GET
from commercial_v1.qianchuan.hot_confirmation import (
    CONTROL_CONFIRM,
    MATERIAL_CONFIRM,
    HotConfirmationScheduler,
    HotConfirmationService,
)
from commercial_v1.storage.database import Database, DatabaseConfig
from commercial_v1.storage.schema import create_schema_v1
from commercial_v1.storage.writer import StorageWriter


class Tokens:
    def get_access_token(self, auth_profile_id: str, *, force_refresh: bool = False) -> str:
        assert auth_profile_id == "auth-1"
        return "token"


class ConfirmClient:
    def __init__(self) -> None:
        self.material_rows = []
        self.control_rows = []

    def get(self, endpoint, *, query=None, access_token, advertiser_id=""):
        query = dict(query or {})
        assert access_token == "token"
        rows = self.material_rows if endpoint == MATERIAL_GET else self.control_rows
        key = "material_list" if endpoint == MATERIAL_GET else "task_list"
        return ApiResponse(
            data={key: list(rows), "page_info": {"page_size": query["page_size"], "total_number": len(rows)}},
            raw={}, request_id="rid", code="0", message="", local_request_uid="local",
        )


def _setup(tmp_path: Path):
    db = Database(DatabaseConfig(tmp_path / "runtime.db"))
    with db.connect() as conn:
        create_schema_v1(conn)
    writer = StorageWriter(db)
    writer.start()
    t = "2026-08-30T07:00:00+00:00"
    writer.execute(
        "INSERT INTO qianchuan_auth_profile(auth_profile_id,app_id,encrypted_app_secret,auth_status,created_at,updated_at) VALUES('auth-1','1','x','ACTIVE',?,?)",
        (t,t),
    ).result(timeout=5)
    writer.execute(
        "INSERT INTO qianchuan_account(account_uid,advertiser_id,enabled,auth_status,created_at,updated_at) VALUES('acc','111111',1,'ACTIVE',?,?)",
        (t,t),
    ).result(timeout=5)
    writer.execute(
        "INSERT INTO qianchuan_account_auth(account_uid,auth_profile_id,is_primary,bound_at,created_at) VALUES('acc','auth-1',1,?,?)",
        (t,t),
    ).result(timeout=5)
    writer.execute(
        """INSERT INTO monitor_plan(target_uid,account_uid,advertiser_id,ad_id,plan_system,promotion_scene,
           official_status,monitor_enabled,lifecycle_state,collection_active,strategy_eligible,write_eligible,
           sync_state,created_at,updated_at)
           VALUES('target','acc','111111','222222','UNI_PROJECT','VIDEO_PROM_GOODS','DELIVERY_OK',1,
                  'ACTIVE_COLLECTING',1,1,1,'TRUSTED',?,?)""",
        (t,t),
    ).result(timeout=5)
    writer.execute(
        "INSERT INTO collection_batch(batch_id,account_uid,target_uid,advertiser_id,ad_id,pipeline_type,started_at,status,created_at) VALUES('old-batch','acc','target','111111','222222','MATERIAL_5M',?,'SUSPICIOUS_EMPTY',?)",
        (t,t),
    ).result(timeout=5)
    writer.execute(
        "INSERT INTO material_registry(material_uid,advertiser_id,ad_id,material_id,first_seen_at,last_seen_at,last_official_status,created_at,updated_at) VALUES('material:111111:222222:900001','111111','222222','900001',?,?, 'DELIVERY_OK',?,?)",
        (t,t,t,t),
    ).result(timeout=5)
    writer.execute(
        """INSERT INTO material_latest(material_uid,advertiser_id,ad_id,material_id,official_material_status,
           overall_cost_decimal,stat_date,collected_at,batch_id,sync_state,strategy_eligible,updated_at)
           VALUES('material:111111:222222:900001','111111','222222','900001','DELIVERY_OK','10','2026-08-30',?,
                  'old-batch','SUSPICIOUS_EMPTY',0,?)""",
        (t,t),
    ).result(timeout=5)
    client = ConfirmClient()
    service = HotConfirmationService(db, writer, client, Tokens())  # type: ignore[arg-type]
    return db,writer,client,service


def test_material_confirmation_accepts_explicit_nonactive_status_only(tmp_path: Path) -> None:
    db,writer,client,service = _setup(tmp_path)
    try:
        client.material_rows = [{
            "material_id":"900001","material_status":"DELIVERY_NOT","audit_status":"PASS",
            "stats_info":{"stat_cost_for_roi2":"11.00"},
        }]
        result = service.confirm_materials(
            "target", now=datetime(2026,8,30,7,1,tzinfo=timezone.utc)
        )
        assert result.status == "CONFIRMED"
        assert result.confirmed_inactive == 1
        with db.connect(readonly=True) as conn:
            row = conn.execute("SELECT official_material_status,overall_cost_decimal,sync_state,strategy_eligible FROM material_latest").fetchone()
            assert tuple(row) == ("DELIVERY_NOT","11","TRUSTED_FINAL_STATE",0)
            registry = conn.execute("SELECT ended_at,last_official_status FROM material_registry").fetchone()
            assert registry["ended_at"] is not None
            assert registry["last_official_status"] == "DELIVERY_NOT"
    finally:
        writer.close()


def test_material_confirmation_missing_stays_untrusted_and_is_not_repeated_forever(tmp_path: Path) -> None:
    db,writer,client,service = _setup(tmp_path)
    try:
        client.material_rows = []
        result = service.confirm_materials(
            "target", now=datetime(2026,8,30,7,1,tzinfo=timezone.utc)
        )
        assert result.status == "INCONCLUSIVE"
        assert result.unresolved == 1
        with db.connect(readonly=True) as conn:
            row = conn.execute("SELECT official_material_status,overall_cost_decimal,sync_state,strategy_eligible,updated_at FROM material_latest").fetchone()
            assert row["official_material_status"] == "DELIVERY_OK"
            assert row["overall_cost_decimal"] == "10"
            assert row["sync_state"] == "MISSING_REQUIRES_CONFIRMATION"
            assert row["strategy_eligible"] == 0
            # Inconclusive confirmation intentionally does not advance pending updated_at.
            assert row["updated_at"] == "2026-08-30T07:00:00+00:00"

        scheduler = HotConfirmationScheduler(db,writer,delay_seconds=60)
        # A finished confirmation newer than the original pending timestamp suppresses endless retries.
        assert scheduler.run_once(now=datetime(2026,8,30,7,2,tzinfo=timezone.utc)) == 0
    finally:
        writer.close()


def test_confirmation_scheduler_creates_one_short_delay_job(tmp_path: Path) -> None:
    db,writer,_client,_service = _setup(tmp_path)
    try:
        scheduler = HotConfirmationScheduler(db,writer,delay_seconds=60)
        assert scheduler.run_once(now=datetime(2026,8,30,7,0,10,tzinfo=timezone.utc)) == 1
        assert scheduler.run_once(now=datetime(2026,8,30,7,0,20,tzinfo=timezone.utc)) == 0
        with db.connect(readonly=True) as conn:
            row = conn.execute("SELECT job_type,due_at FROM background_job").fetchone()
            assert row["job_type"] == MATERIAL_CONFIRM
            assert row["due_at"] == "2026-08-30T07:01:00+00:00"
    finally:
        writer.close()


def test_control_confirmation_keeps_unknown_missing_and_proves_explicit_end(tmp_path: Path) -> None:
    db,writer,client,service = _setup(tmp_path)
    try:
        t = "2026-08-30T07:00:00+00:00"
        writer.execute(
            "INSERT INTO control_task_registry(control_task_uid,advertiser_id,ad_id,control_task_id,first_seen_at,last_seen_at,last_official_status,created_at,updated_at) VALUES('control:111111:222222:700001','111111','222222','700001',?,?,'PROCESSING',?,?)",
            (t,t,t,t),
        ).result(timeout=5)
        writer.execute(
            """INSERT INTO control_task_latest(control_task_uid,advertiser_id,ad_id,control_task_id,official_task_status,
               assist_cost_decimal,stat_date,collected_at,batch_id,sync_state,strategy_eligible,write_eligible,updated_at)
               VALUES('control:111111:222222:700001','111111','222222','700001','PROCESSING','20','2026-08-30',?,
                      'old-batch','SUSPICIOUS_EMPTY',0,0,?)""",
            (t,t),
        ).result(timeout=5)
        client.control_rows = [{
            "id":"700001","task_status":"DISABLE","name":"x","scene":"MATERIAL_ADD_BUDGET",
            "budget":"100","duration":"2","material_list":[{"material_id":"900001"}],
            "metrics":{"stat_cost_for_roi2_assist":"22"},
        }]
        result = service.confirm_controls(
            "target", now=datetime(2026,8,30,7,1,tzinfo=timezone.utc)
        )
        assert result.confirmed_inactive == 1
        with db.connect(readonly=True) as conn:
            row = conn.execute("SELECT official_task_status,assist_cost_decimal,sync_state,strategy_eligible,write_eligible FROM control_task_latest").fetchone()
            assert tuple(row) == ("DISABLE","22","TRUSTED_FINAL_STATE",0,0)
            relation = conn.execute("SELECT material_id FROM control_task_material").fetchone()
            assert relation["material_id"] == "900001"
    finally:
        writer.close()
