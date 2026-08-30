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


class IsolationClient:
    def __init__(self) -> None:
        self.material_by_ad: dict[str, list[dict]] = {}
        self.control_by_ad: dict[str, list[dict]] = {}

    def get(self, endpoint, *, query=None, access_token, advertiser_id=""):
        query = dict(query or {})
        ad_id = str(query.get("ad_id") or "")
        if endpoint == MATERIAL_GET:
            rows = list(self.material_by_ad.get(ad_id, []))
            return ApiResponse(
                data={
                    "material_list": rows,
                    "page_info": {"page_size": query["page_size"], "total_number": len(rows)},
                },
                raw={}, request_id=f"m-{ad_id}", code="0", message="", local_request_uid=f"ml-{ad_id}",
            )
        if endpoint == CONTROL_TASK_LIST:
            rows = list(self.control_by_ad.get(ad_id, []))
            return ApiResponse(
                data={
                    "task_list": rows,
                    "page_info": {"page_size": query["page_size"], "total_number": len(rows)},
                },
                raw={}, request_id=f"c-{ad_id}", code="0", message="", local_request_uid=f"cl-{ad_id}",
            )
        raise AssertionError(endpoint)


def _material(cost: str, *, status: str = "DELIVERY_OK") -> dict:
    return {
        "material_id": "900001",
        "video_id": "800001",
        "title": "same-material-id",
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


def _control(cost: str) -> dict:
    return {
        "id": "700001",
        "name": "control",
        "scene": "MATERIAL_ADD_BUDGET",
        "task_status": "PROCESSING",
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
           ) VALUES('auth-1','123','cipher','ACTIVE',?,?)""", (now, now)
    ).result(timeout=5)
    writer.execute(
        """INSERT INTO qianchuan_account(
           account_uid,advertiser_id,account_name,account_type,enabled,auth_status,created_at,updated_at
           ) VALUES('acc','111111','A','QIANCHUAN',1,'ACTIVE',?,?)""", (now, now)
    ).result(timeout=5)
    writer.execute(
        """INSERT INTO qianchuan_account_auth(account_uid,auth_profile_id,is_primary,bound_at,created_at)
           VALUES('acc','auth-1',1,?,?)""", (now, now)
    ).result(timeout=5)
    for target_uid, ad_id in (("target-1", "222222"), ("target-2", "333333")):
        writer.execute(
            """INSERT INTO monitor_plan(
               target_uid,account_uid,advertiser_id,ad_id,plan_name,plan_system,promotion_scene,
               official_status,monitor_enabled,lifecycle_state,collection_active,strategy_eligible,
               write_eligible,sync_state,created_at,updated_at
               ) VALUES(?, 'acc','111111',?,?,'UNI_PROJECT','VIDEO_PROM_GOODS',
                        'DELIVERY_OK',1,'ACTIVE_COLLECTING',1,1,1,'TRUSTED',?,?)""",
            (target_uid, ad_id, target_uid, now, now),
        ).result(timeout=5)
    client = IsolationClient()
    service = HotCollectionService(db, writer, client, FakeTokens())  # type: ignore[arg-type]
    return db, writer, client, service


def test_same_material_id_in_two_plans_never_cross_contaminates(tmp_path: Path) -> None:
    db, writer, client, service = _setup(tmp_path)
    try:
        client.material_by_ad["222222"] = [_material("10")]
        client.material_by_ad["333333"] = [_material("99")]
        now = datetime(2026, 8, 30, 7, 0, tzinfo=timezone.utc)
        service.collect_materials("target-1", scheduled_at="2026-08-30T07:00:00+00:00", now=now)
        service.collect_materials("target-2", scheduled_at="2026-08-30T07:00:00+00:00", now=now)

        with db.connect(readonly=True) as conn:
            rows = conn.execute(
                """SELECT ad_id,material_id,overall_cost_decimal FROM material_latest
                   WHERE material_id='900001' ORDER BY ad_id"""
            ).fetchall()
            assert [(row["ad_id"], row["material_id"], row["overall_cost_decimal"]) for row in rows] == [
                ("222222", "900001", "10"),
                ("333333", "900001", "99"),
            ]
    finally:
        writer.close()


def test_material_contract_failure_does_not_block_control_pipeline(tmp_path: Path) -> None:
    db, writer, client, service = _setup(tmp_path)
    try:
        client.material_by_ad["222222"] = [_material("999", status="DELIVERY_NOT")]
        client.control_by_ad["222222"] = [_control("12")]
        now = datetime(2026, 8, 30, 7, 0, tzinfo=timezone.utc)

        with pytest.raises(OpenApiResponseError) as captured:
            service.collect_materials("target-1", scheduled_at="2026-08-30T07:00:00+00:00", now=now)
        assert captured.value.code == "MATERIAL_ACTIVE_FILTER_MISMATCH"

        control = service.collect_controls(
            "target-1",
            scheduled_at="2026-08-30T07:00:00+00:00",
            now=now,
        )
        assert control.status == "SUCCESS"

        with db.connect(readonly=True) as conn:
            assert conn.execute("SELECT COUNT(*) FROM material_latest").fetchone()[0] == 0
            latest = conn.execute(
                "SELECT assist_cost_decimal,sync_state,strategy_eligible,write_eligible FROM control_task_latest"
            ).fetchone()
            assert latest["assist_cost_decimal"] == "12"
            assert latest["sync_state"] == "TRUSTED"
            assert latest["strategy_eligible"] == 1
            assert latest["write_eligible"] == 1
            batches = conn.execute(
                "SELECT pipeline_type,status FROM collection_batch ORDER BY pipeline_type"
            ).fetchall()
            assert {(row["pipeline_type"], row["status"]) for row in batches} == {
                ("MATERIAL_5M", "FAILED"),
                ("CONTROL_5M", "SUCCESS"),
            }
    finally:
        writer.close()
