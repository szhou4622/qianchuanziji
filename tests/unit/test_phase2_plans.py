from pathlib import Path

import pytest

from commercial_v1.qianchuan.client import ApiResponse
from commercial_v1.qianchuan.normalizers import NormalizedPlan, normalize_plan
from commercial_v1.qianchuan.plans import (
    FOUR_PLAN_CLASSES,
    MonitorPlanStore,
    PlanCatalogService,
    PlanMonitorService,
)
from commercial_v1.storage.database import Database, DatabaseConfig
from commercial_v1.storage.schema import create_schema_v1
from commercial_v1.storage.writer import StorageWriter


def _api(data, request_id="rid"):
    return ApiResponse(data=data, raw={"code": 0, "data": data}, request_id=request_id, code="0", message="", local_request_uid="local")


class FakeTokens:
    def get_access_token(self, auth_profile_id: str, *, force_refresh: bool = False) -> str:
        assert auth_profile_id == "auth-1"
        return "token"


class FourClassClient:
    def __init__(self):
        self.class_queries = []

    def get(self, endpoint, *, query=None, access_token, advertiser_id=""):
        assert access_token == "token"
        query = dict(query or {})
        goal = query.get("marketing_goal")
        scene = query.get("adlab_scene")
        self.class_queries.append((goal, scene))
        index = [
            (g, s) for _key, g, s in FOUR_PLAN_CLASSES
        ].index((goal, scene))
        ad_id = str(700001 + index)
        return _api(
            {
                "ad_list": [
                    {
                        "ad_info": {
                            "id": ad_id,
                            "name": f"plan-{index}",
                            "marketing_goal": goal,
                            "adlab_scene": scene,
                            "status": "DELIVERY_OK",
                        }
                    }
                ],
                "page_info": {"page_size": 100, "total_number": 1},
            },
            f"rid-{index}",
        )


def _database(tmp_path: Path) -> Database:
    db = Database(DatabaseConfig(tmp_path / "runtime.db"))
    with db.connect() as conn:
        create_schema_v1(conn)
    return db


def _seed_enabled_account(db: Database, writer: StorageWriter, advertiser_id: str = "222222") -> None:
    now = "2026-08-30T00:00:00+00:00"
    writer.execute(
        """INSERT INTO qianchuan_auth_profile(
           auth_profile_id,app_id,encrypted_app_secret,auth_status,created_at,updated_at
           ) VALUES('auth-1','123456','cipher','ACTIVE',?,?)""",
        (now, now),
    ).result(timeout=5)
    writer.execute(
        """INSERT INTO qianchuan_account(
           account_uid,advertiser_id,account_name,account_type,enabled,auth_status,
           created_at,updated_at
           ) VALUES(?,?,?,'QIANCHUAN',1,'ACTIVE',?,?)""",
        (f"qc:{advertiser_id}", advertiser_id, "account", now, now),
    ).result(timeout=5)
    writer.execute(
        """INSERT INTO qianchuan_account_auth(
           account_uid,auth_profile_id,is_primary,bound_at,created_at
           ) VALUES(?,?,1,?,?)""",
        (f"qc:{advertiser_id}", "auth-1", now, now),
    ).result(timeout=5)


def _plan(ad_id: str, *, goal: str, status: str, scene: str = "OVERALL_PROJECT") -> NormalizedPlan:
    return normalize_plan(
        {
            "id": ad_id,
            "name": ad_id,
            "marketing_goal": goal,
            "adlab_scene": scene,
            "status": status,
        },
        advertiser_id="222222",
        expected_marketing_goal=goal,
        expected_adlab_scene=scene,
    )


def test_catalog_queries_all_four_classes_explicitly() -> None:
    client = FourClassClient()
    service = PlanCatalogService(client, FakeTokens())  # type: ignore[arg-type]
    result = service.list_all(
        "auth-1",
        "222222",
        start_time="2026-08-01 00:00:00",
        end_time="2026-08-30 23:59:59",
    )
    assert result.complete is True
    assert len(result.plans) == 4
    assert set(client.class_queries) == {(g, s) for _key, g, s in FOUR_PLAN_CLASSES}
    assert all(plan.classification_status == "VERIFIED" for plan in result.plans)


def test_product_plan_has_only_delivery_lifecycle_not_live_semantics(tmp_path: Path) -> None:
    db = _database(tmp_path)
    writer = StorageWriter(db)
    writer.start()
    try:
        _seed_enabled_account(db, writer)
        store = MonitorPlanStore(db, writer)
        product = _plan("810001", goal="VIDEO_PROM_GOODS", status="OFFLINE_BUDGET")
        target_uid = store.enroll_verified(product)
        target = store.get_target(target_uid)
        assert target["promotion_scene"] == "VIDEO_PROM_GOODS"
        assert target["lifecycle_state"] == "WATCHING"
        assert target["collection_active"] == 0
        assert target["next_status_check_at"] is not None
        assert target["next_hot_collect_at"] is None
    finally:
        writer.close()


def test_delivery_ok_enters_hot_collection_and_deleted_is_terminal(tmp_path: Path) -> None:
    db = _database(tmp_path)
    writer = StorageWriter(db)
    writer.start()
    try:
        _seed_enabled_account(db, writer)
        store = MonitorPlanStore(db, writer)
        active = _plan("810002", goal="LIVE_PROM_GOODS", status="DELIVERY_OK")
        active_uid = store.enroll_verified(active)
        active_target = store.get_target(active_uid)
        assert active_target["lifecycle_state"] == "ACTIVE_COLLECTING"
        assert active_target["collection_active"] == 1
        assert active_target["strategy_eligible"] == 1
        assert active_target["write_eligible"] == 1
        assert active_target["next_status_check_at"] is None
        assert active_target["next_hot_collect_at"] is not None

        deleted = _plan("810003", goal="VIDEO_PROM_GOODS", status="DELETED")
        deleted_uid = store.enroll_verified(deleted)
        deleted_target = store.get_target(deleted_uid)
        assert deleted_target["lifecycle_state"] == "TERMINAL"
        assert deleted_target["collection_active"] == 0
        assert deleted_target["terminal_at"] is not None
        assert deleted_target["next_status_check_at"] is None
    finally:
        writer.close()


def test_per_account_monitor_limit_is_ten(tmp_path: Path) -> None:
    db = _database(tmp_path)
    writer = StorageWriter(db)
    writer.start()
    try:
        _seed_enabled_account(db, writer)
        store = MonitorPlanStore(db, writer)
        for i in range(10):
            store.enroll_verified(
                _plan(str(820000 + i), goal="VIDEO_PROM_GOODS", status="OFFLINE_BUDGET")
            )
        with pytest.raises(ValueError, match="10"):
            store.enroll_verified(
                _plan("829999", goal="VIDEO_PROM_GOODS", status="OFFLINE_BUDGET")
            )
    finally:
        writer.close()


def test_state_check_moves_product_plan_from_watching_to_active(tmp_path: Path) -> None:
    db = _database(tmp_path)
    writer = StorageWriter(db)
    writer.start()
    try:
        _seed_enabled_account(db, writer)
        store = MonitorPlanStore(db, writer)
        waiting = _plan("830001", goal="VIDEO_PROM_GOODS", status="OFFLINE_BUDGET")
        target_uid = store.enroll_verified(waiting)

        class Catalog:
            def get_detail(self, auth_profile_id, advertiser_id, ad_id, *, expected_marketing_goal, expected_adlab_scene):
                assert auth_profile_id == "auth-1"
                return _plan(ad_id, goal="VIDEO_PROM_GOODS", status="DELIVERY_OK"), "detail-rid"

        monitor = PlanMonitorService(Catalog(), store, writer)  # type: ignore[arg-type]
        result = monitor.check_active_state(target_uid)
        assert result.official_status == "DELIVERY_OK"
        target = store.get_target(target_uid)
        assert target["lifecycle_state"] == "ACTIVE_COLLECTING"
        assert target["collection_active"] == 1
        assert target["next_status_check_at"] is None
        assert target["next_hot_collect_at"] is not None
    finally:
        writer.close()


def test_monitor_disable_stops_all_scheduling(tmp_path: Path) -> None:
    db = _database(tmp_path)
    writer = StorageWriter(db)
    writer.start()
    try:
        _seed_enabled_account(db, writer)
        store = MonitorPlanStore(db, writer)
        target_uid = store.enroll_verified(
            _plan("840001", goal="LIVE_PROM_GOODS", status="DELIVERY_OK")
        )
        store.set_monitor_enabled(target_uid, False)
        target = store.get_target(target_uid)
        assert target["lifecycle_state"] == "MONITOR_DISABLED"
        assert target["collection_active"] == 0
        assert target["strategy_eligible"] == 0
        assert target["write_eligible"] == 0
        assert target["next_status_check_at"] is None
        assert target["next_hot_collect_at"] is None
    finally:
        writer.close()
