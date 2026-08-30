from datetime import datetime, timezone
from pathlib import Path

import pytest

from commercial_v1.qianchuan.client import ApiResponse
from commercial_v1.qianchuan.contracts import CONTROL_TASK_LIST, MATERIAL_GET
from commercial_v1.qianchuan.errors import OpenApiResponseError
from commercial_v1.qianchuan.hot_collection import HotCollectionService
from commercial_v1.storage.database import Database, DatabaseConfig
from commercial_v1.storage.schema import create_schema_v1
from commercial_v1.storage.writer import StorageWriter


class FakeTokens:
    def get_access_token(self, auth_profile_id: str, *, force_refresh: bool = False) -> str:
        assert auth_profile_id == "auth-1"
        return "token-refreshed" if force_refresh else "token"


class ContractClient:
    def __init__(self) -> None:
        self.material_rows = []
        self.control_rows = []

    def get(self, endpoint, *, query=None, access_token, advertiser_id=""):
        query = dict(query or {})
        if endpoint == MATERIAL_GET:
            rows = list(self.material_rows)
            return ApiResponse(
                data={
                    "material_list": rows,
                    "page_info": {
                        "page_size": query["page_size"],
                        "total_number": len(rows),
                    },
                },
                raw={},
                request_id="material-rid",
                code="0",
                message="",
                local_request_uid="material-local",
            )
        if endpoint == CONTROL_TASK_LIST:
            rows = list(self.control_rows)
            return ApiResponse(
                data={
                    "task_list": rows,
                    "page_info": {
                        "page_size": query["page_size"],
                        "total_number": len(rows),
                    },
                },
                raw={},
                request_id="control-rid",
                code="0",
                message="",
                local_request_uid="control-local",
            )
        raise AssertionError(endpoint)


def _setup(tmp_path: Path):
    db = Database(DatabaseConfig(tmp_path / "runtime.db"))
    with db.connect() as conn:
        create_schema_v1(conn)
    writer = StorageWriter(db)
    writer.start()
    now = "2026-08-30T00:00:00+00:00"
    writer.execute(
        """INSERT INTO qianchuan_auth_profile(
           auth_profile_id,app_id,encrypted_app_secret,auth_status,created_at,updated_at
           ) VALUES('auth-1','123','cipher','ACTIVE',?,?)""",
        (now, now),
    ).result(timeout=5)
    writer.execute(
        """INSERT INTO qianchuan_account(
           account_uid,advertiser_id,account_name,account_type,enabled,auth_status,created_at,updated_at
           ) VALUES('acc','111111','A','QIANCHUAN',1,'ACTIVE',?,?)""",
        (now, now),
    ).result(timeout=5)
    writer.execute(
        """INSERT INTO qianchuan_account_auth(
           account_uid,auth_profile_id,is_primary,bound_at,created_at
           ) VALUES('acc','auth-1',1,?,?)""",
        (now, now),
    ).result(timeout=5)
    writer.execute(
        """INSERT INTO monitor_plan(
           target_uid,account_uid,advertiser_id,ad_id,plan_name,plan_system,promotion_scene,
           official_status,monitor_enabled,lifecycle_state,collection_active,strategy_eligible,
           write_eligible,sync_state,created_at,updated_at
           ) VALUES('target','acc','111111','222222','P','UNI_PROJECT','VIDEO_PROM_GOODS',
                    'DELIVERY_OK',1,'ACTIVE_COLLECTING',1,1,1,'TRUSTED',?,?)""",
        (now, now),
    ).result(timeout=5)
    client = ContractClient()
    service = HotCollectionService(db, writer, client, FakeTokens())  # type: ignore[arg-type]
    return db, writer, client, service


def _material(*, status="DELIVERY_OK", cost="10"):
    return {
        "material_id": "900001",
        "video_id": "800001",
        "title": "素材1",
        "material_status": status,
        "audit_status": "PASS",
        "stats_info": {
            "stat_cost_for_roi2": cost,
            "total_order_settle_amount_for_roi2_1h": "20",
            "total_prepay_and_pay_settle_roi2_1h": "2",
            "total_order_settle_count_for_roi2_1h": "1",
            "total_pay_order_count_for_roi2": "1",
            "total_pay_order_gmv_include_coupon_for_roi2": "20",
            "total_prepay_and_pay_order_roi2": "2",
        },
    }


def _control(*, status="PROCESSING", scene="MATERIAL_ADD_BUDGET", cost="10"):
    return {
        "id": "700001",
        "name": "task-1",
        "scene": scene,
        "task_status": status,
        "budget": "500",
        "duration": "2",
        "material_list": [{"material_id": "900001"}],
        "metrics": {
            "stat_cost_for_roi2_assist": cost,
            "total_pay_order_count_for_roi2_assist": "1",
            "total_pay_order_gmv_include_coupon_for_roi2_assist": "20",
            "total_prepay_and_pay_order_roi2_assist": "2",
            "total_order_settle_amount_for_roi2_1h_assist": "15",
            "total_prepay_and_pay_settle_roi2_1h_assist": "1.5",
            "total_order_settle_count_for_roi2_1h_assist": "1",
        },
    }


def test_material_filter_contract_failure_keeps_previous_latest(tmp_path: Path) -> None:
    db, writer, client, service = _setup(tmp_path)
    try:
        client.material_rows = [_material(cost="10")]
        service.collect_materials(
            "target",
            scheduled_at="2026-08-30T07:00:00+00:00",
            now=datetime(2026, 8, 30, 7, 0, tzinfo=timezone.utc),
        )

        client.material_rows = [_material(status="DELIVERY_NOT", cost="999")]
        with pytest.raises(OpenApiResponseError) as captured:
            service.collect_materials(
                "target",
                scheduled_at="2026-08-30T07:05:00+00:00",
                now=datetime(2026, 8, 30, 7, 5, tzinfo=timezone.utc),
            )
        assert captured.value.code == "MATERIAL_ACTIVE_FILTER_MISMATCH"

        with db.connect(readonly=True) as conn:
            latest = conn.execute("SELECT * FROM material_latest").fetchone()
            assert latest["overall_cost_decimal"] == "10"
            assert latest["sync_state"] == "TRUSTED"
            assert latest["strategy_eligible"] == 1
            assert conn.execute("SELECT COUNT(*) FROM material_5m").fetchone()[0] == 1
            failed = conn.execute(
                """SELECT status,error_code FROM collection_batch
                   WHERE pipeline_type='MATERIAL_5M' AND error_code='MATERIAL_ACTIVE_FILTER_MISMATCH'
                   LIMIT 1"""
            ).fetchone()
            assert failed is not None
            assert failed["status"] == "FAILED"
    finally:
        writer.close()


def test_control_filter_contract_failure_keeps_previous_latest(tmp_path: Path) -> None:
    db, writer, client, service = _setup(tmp_path)
    try:
        client.control_rows = [_control(cost="10")]
        service.collect_controls(
            "target",
            scheduled_at="2026-08-30T07:00:00+00:00",
            now=datetime(2026, 8, 30, 7, 0, tzinfo=timezone.utc),
        )

        client.control_rows = [_control(status="DISABLE", cost="999")]
        with pytest.raises(OpenApiResponseError) as captured:
            service.collect_controls(
                "target",
                scheduled_at="2026-08-30T07:05:00+00:00",
                now=datetime(2026, 8, 30, 7, 5, tzinfo=timezone.utc),
            )
        assert captured.value.code == "CONTROL_ACTIVE_FILTER_MISMATCH"

        with db.connect(readonly=True) as conn:
            latest = conn.execute("SELECT * FROM control_task_latest").fetchone()
            assert latest["assist_cost_decimal"] == "10"
            assert latest["sync_state"] == "TRUSTED"
            assert latest["strategy_eligible"] == 1
            assert latest["write_eligible"] == 1
            assert conn.execute("SELECT COUNT(*) FROM control_task_5m").fetchone()[0] == 1
            failed = conn.execute(
                """SELECT status,error_code FROM collection_batch
                   WHERE pipeline_type='CONTROL_5M' AND error_code='CONTROL_ACTIVE_FILTER_MISMATCH'
                   LIMIT 1"""
            ).fetchone()
            assert failed is not None
            assert failed["status"] == "FAILED"
    finally:
        writer.close()
