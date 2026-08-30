from datetime import datetime, timezone
from pathlib import Path

from commercial_v1.qianchuan.client import ApiResponse
from commercial_v1.qianchuan.contracts import CONTROL_TASK_LIST, MATERIAL_GET
from commercial_v1.qianchuan.hot_collection import HotCollectionService
from commercial_v1.qianchuan.hot_models import CONTROL_METRIC_FIELDS, MATERIAL_METRIC_FIELDS
from commercial_v1.storage.database import Database, DatabaseConfig
from commercial_v1.storage.schema import create_schema_v1
from commercial_v1.storage.writer import StorageWriter


class FakeTokens:
    def get_access_token(self, auth_profile_id: str, *, force_refresh: bool = False) -> str:
        assert auth_profile_id == "auth-1"
        return "token-refreshed" if force_refresh else "token"


class HotClient:
    def __init__(self) -> None:
        self.material_rows = []
        self.control_rows = []
        self.calls = []

    def get(self, endpoint, *, query=None, access_token, advertiser_id=""):
        query = dict(query or {})
        self.calls.append((endpoint, query, access_token, advertiser_id))
        assert access_token in {"token", "token-refreshed"}
        if endpoint == MATERIAL_GET:
            rows = list(self.material_rows)
            return ApiResponse(
                data={
                    "material_list": rows,
                    "page_info": {"page_size": query["page_size"], "total_number": len(rows)},
                },
                raw={}, request_id="material-rid", code="0", message="", local_request_uid="m-local",
            )
        if endpoint == CONTROL_TASK_LIST:
            rows = list(self.control_rows)
            return ApiResponse(
                data={
                    "task_list": rows,
                    "page_info": {"page_size": query["page_size"], "total_number": len(rows)},
                },
                raw={}, request_id="control-rid", code="0", message="", local_request_uid="c-local",
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
    client = HotClient()
    service = HotCollectionService(db, writer, client, FakeTokens())  # type: ignore[arg-type]
    return db, writer, client, service


def _material(cost="10.50"):
    return {
        "material_id": "900001",
        "video_id": "800001",
        "title": "素材1",
        "material_status": "DELIVERY_OK",
        "audit_status": "PASS",
        "stats_info": {
            "stat_cost_for_roi2": cost,
            "total_order_settle_amount_for_roi2_1h": "20",
            "total_prepay_and_pay_settle_roi2_1h": "1.9",
            "total_order_settle_count_for_roi2_1h": "1",
            "total_pay_order_count_for_roi2": "2",
            "total_pay_order_gmv_include_coupon_for_roi2": "25",
            "total_prepay_and_pay_order_roi2": "2.38",
        },
    }


def test_material_collection_writes_latest_sparse_snapshot_and_suspicious_empty(tmp_path: Path) -> None:
    db, writer, client, service = _setup(tmp_path)
    try:
        client.material_rows = [_material()]
        first = service.collect_materials(
            "target",
            scheduled_at="2026-08-30T07:00:00+00:00",
            now=datetime(2026, 8, 30, 7, 0, tzinfo=timezone.utc),
        )
        assert first.status == "SUCCESS"
        with db.connect(readonly=True) as conn:
            latest = conn.execute("SELECT * FROM material_latest").fetchone()
            assert latest["overall_cost_decimal"] == "10.5"
            assert latest["net_settle_order_count"] == 1
            assert latest["sync_state"] == "TRUSTED"
            assert conn.execute("SELECT COUNT(*) FROM material_5m").fetchone()[0] == 1

        # Same values refresh Latest freshness but do not create duplicate history.
        second = service.collect_materials(
            "target",
            scheduled_at="2026-08-30T07:05:00+00:00",
            now=datetime(2026, 8, 30, 7, 5, tzinfo=timezone.utc),
        )
        assert second.status == "SUCCESS"
        with db.connect(readonly=True) as conn:
            assert conn.execute("SELECT COUNT(*) FROM material_5m").fetchone()[0] == 1

        client.material_rows = []
        suspicious = service.collect_materials(
            "target",
            scheduled_at="2026-08-30T07:10:00+00:00",
            now=datetime(2026, 8, 30, 7, 10, tzinfo=timezone.utc),
        )
        assert suspicious.status == "SUSPICIOUS_EMPTY"
        with db.connect(readonly=True) as conn:
            latest = conn.execute("SELECT * FROM material_latest").fetchone()
            assert latest["overall_cost_decimal"] == "10.5"
            assert latest["sync_state"] == "SUSPICIOUS_EMPTY"
            assert latest["strategy_eligible"] == 0
            batch = conn.execute("SELECT status FROM collection_batch WHERE batch_id=?", (suspicious.batch_id,)).fetchone()
            assert batch["status"] == "SUSPICIOUS_EMPTY"

        material_call = next(call for call in client.calls if call[0] == MATERIAL_GET)
        assert material_call[1]["fields"] == list(MATERIAL_METRIC_FIELDS)
        assert material_call[1]["filtering"]["material_status"] == "DELIVERY_OK"
    finally:
        writer.close()


def test_control_collection_keeps_same_material_separate_by_task_id(tmp_path: Path) -> None:
    db, writer, client, service = _setup(tmp_path)
    try:
        client.control_rows = [
            {
                "id": "700001",
                "name": "task-1",
                "scene": "MATERIAL_ADD_BUDGET",
                "task_status": "PROCESSING",
                "budget": "500",
                "duration": "2",
                "material_list": [{"material_id": "900001"}],
                "metrics": {
                    "stat_cost_for_roi2_assist": "10",
                    "total_pay_order_count_for_roi2_assist": "1",
                    "total_pay_order_gmv_include_coupon_for_roi2_assist": "20",
                    "total_prepay_and_pay_order_roi2_assist": "2",
                    "total_order_settle_amount_for_roi2_1h_assist": "15",
                    "total_prepay_and_pay_settle_roi2_1h_assist": "1.5",
                    "total_order_settle_count_for_roi2_1h_assist": "1",
                },
            },
            {
                "id": "700002",
                "name": "task-2",
                "scene": "MATERIAL_ADD_BUDGET",
                "task_status": "PROCESSING",
                "budget": "600",
                "duration": "3",
                "material_list": [{"material_id": "900001"}],
                "metrics": {
                    "stat_cost_for_roi2_assist": "30",
                    "total_pay_order_count_for_roi2_assist": "4",
                    "total_pay_order_gmv_include_coupon_for_roi2_assist": "80",
                    "total_prepay_and_pay_order_roi2_assist": "2.66",
                    "total_order_settle_amount_for_roi2_1h_assist": "60",
                    "total_prepay_and_pay_settle_roi2_1h_assist": "2",
                    "total_order_settle_count_for_roi2_1h_assist": "3",
                },
            },
        ]
        result = service.collect_controls(
            "target",
            scheduled_at="2026-08-30T07:00:00+00:00",
            now=datetime(2026, 8, 30, 7, 0, tzinfo=timezone.utc),
        )
        assert result.status == "SUCCESS"
        assert result.row_count == 2
        with db.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT control_task_id,assist_cost_decimal,assist_net_order_count FROM control_task_latest ORDER BY control_task_id"
            ).fetchall()
            assert [(row[0], row[1], row[2]) for row in rows] == [
                ("700001", "10", 1),
                ("700002", "30", 3),
            ]
            relations = conn.execute(
                "SELECT control_task_uid,material_id FROM control_task_material ORDER BY control_task_uid"
            ).fetchall()
            assert len(relations) == 2
            assert {row[1] for row in relations} == {"900001"}

        control_call = next(call for call in client.calls if call[0] == CONTROL_TASK_LIST)
        assert control_call[1]["fields"] == list(CONTROL_METRIC_FIELDS)
        assert control_call[1]["filtering"] == {"task_status": "PROCESSING"}
    finally:
        writer.close()
