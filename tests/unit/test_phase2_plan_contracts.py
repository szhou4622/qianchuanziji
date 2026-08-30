from pathlib import Path

from commercial_v1.qianchuan.client import ApiResponse
from commercial_v1.qianchuan.contracts import PLAN_DETAIL
from commercial_v1.qianchuan.errors import OpenApiTokenError
from commercial_v1.qianchuan.normalizers import normalize_plan
from commercial_v1.qianchuan.plans import MonitorPlanStore, PlanCatalogService
from commercial_v1.storage.database import Database, DatabaseConfig
from commercial_v1.storage.schema import create_schema_v1
from commercial_v1.storage.writer import StorageWriter


def _api(data, request_id="rid"):
    return ApiResponse(
        data=data,
        raw={"code": 0, "data": data},
        request_id=request_id,
        code="0",
        message="",
        local_request_uid="local",
    )


def _database(tmp_path: Path) -> Database:
    db = Database(DatabaseConfig(tmp_path / "runtime.db"))
    with db.connect() as conn:
        create_schema_v1(conn)
    return db


def _seed_enabled_account(writer: StorageWriter) -> None:
    now = "2026-08-30T00:00:00+00:00"
    writer.execute(
        """INSERT INTO qianchuan_auth_profile(
           auth_profile_id,app_id,encrypted_app_secret,auth_status,created_at,updated_at
           ) VALUES('auth-1','123456','cipher','ACTIVE',?,?)""",
        (now, now),
    ).result(timeout=5)
    writer.execute(
        """INSERT INTO qianchuan_account(
           account_uid,advertiser_id,account_name,account_type,enabled,auth_status,created_at,updated_at
           ) VALUES('qc:222222','222222','account','QIANCHUAN',1,'ACTIVE',?,?)""",
        (now, now),
    ).result(timeout=5)
    writer.execute(
        """INSERT INTO qianchuan_account_auth(
           account_uid,auth_profile_id,is_primary,bound_at,created_at
           ) VALUES('qc:222222','auth-1',1,?,?)""",
        (now, now),
    ).result(timeout=5)


def test_plan_budget_is_canonical_decimal_text_and_persists_without_unit_guess(tmp_path: Path) -> None:
    plan = normalize_plan(
        {
            "ad_id": "950001",
            "name": "budget-plan",
            "marketing_goal": "VIDEO_PROM_GOODS",
            "adlab_scene": "OVERALL_PROJECT",
            "status": "DELIVERY_OK",
            "delivery_setting": {"budget": "123.4500"},
        },
        advertiser_id="222222",
        expected_marketing_goal="VIDEO_PROM_GOODS",
        expected_adlab_scene="OVERALL_PROJECT",
    )
    assert plan.budget_decimal == "123.45"

    db = _database(tmp_path)
    writer = StorageWriter(db)
    writer.start()
    try:
        _seed_enabled_account(writer)
        store = MonitorPlanStore(db, writer)
        target_uid = store.enroll_verified(plan)
        target = store.get_target(target_uid)
        assert target["budget_decimal"] == "123.45"
        assert isinstance(target["budget_decimal"], str)
    finally:
        writer.close()


def test_plan_detail_refreshes_expired_access_token_once() -> None:
    class Tokens:
        def __init__(self) -> None:
            self.calls = []

        def get_access_token(self, auth_profile_id: str, *, force_refresh: bool = False) -> str:
            assert auth_profile_id == "auth-1"
            self.calls.append(force_refresh)
            return "new-token" if force_refresh else "old-token"

    class Client:
        def __init__(self) -> None:
            self.calls = []

        def get(self, endpoint, *, query=None, access_token, advertiser_id=""):
            assert endpoint == PLAN_DETAIL
            self.calls.append(access_token)
            if access_token == "old-token":
                raise OpenApiTokenError("access_token expired", code="TOKEN_EXPIRED")
            return _api(
                {
                    "ad_id": "950002",
                    "name": "refreshed",
                    "marketing_goal": "LIVE_PROM_GOODS",
                    "adlab_scene": "OVERALL_PROJECT",
                    "status": "DELIVERY_OK",
                },
                "detail-rid",
            )

    tokens = Tokens()
    client = Client()
    service = PlanCatalogService(client, tokens)  # type: ignore[arg-type]
    plan, request_id = service.get_detail(
        "auth-1",
        "222222",
        "950002",
        expected_marketing_goal="LIVE_PROM_GOODS",
        expected_adlab_scene="OVERALL_PROJECT",
    )
    assert plan.ad_id == "950002"
    assert request_id == "detail-rid"
    assert tokens.calls == [False, True]
    assert client.calls == ["old-token", "new-token"]
