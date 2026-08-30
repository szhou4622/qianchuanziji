from pathlib import Path

import pytest

from commercial_v1.qianchuan.accounts import AccountDiscoveryService, AccountStore
from commercial_v1.qianchuan.client import ApiResponse
from commercial_v1.qianchuan.contracts import (
    ADVERTISER_PUBLIC_INFO,
    EBP_ADVERTISER_LIST,
    OAUTH_ADVERTISER_GET,
)
from commercial_v1.qianchuan.errors import (
    OpenApiContractError,
    OpenApiResponseError,
    OpenApiTokenError,
)
from commercial_v1.qianchuan.normalizers import normalize_final_advertiser, normalize_plan
from commercial_v1.qianchuan.pagination import get_all_pages
from commercial_v1.storage.database import Database, DatabaseConfig
from commercial_v1.storage.schema import create_schema_v1
from commercial_v1.storage.writer import StorageWriter


def _api(data, request_id="rid"):
    return ApiResponse(data=data, raw={"code": 0, "data": data}, request_id=request_id, code="0", message="", local_request_uid="local")


class FakeTokenProvider:
    def get_access_token(self, auth_profile_id: str, *, force_refresh: bool = False) -> str:
        assert auth_profile_id == "auth-1"
        return "token"


class DiscoveryClient:
    def __init__(self):
        self.calls = []

    def get(self, endpoint, *, query=None, access_token, advertiser_id=""):
        self.calls.append((endpoint, dict(query or {}), advertiser_id))
        assert access_token == "token"
        if endpoint == OAUTH_ADVERTISER_GET:
            return _api(
                {
                    "list": [
                        {
                            "advertiser_id": "111111",
                            "advertiser_name": "OAuth Principal",
                            "role": "ADVERTISER",
                        }
                    ]
                },
                "oauth-rid",
            )
        if endpoint == EBP_ADVERTISER_LIST:
            return _api(
                {
                    "account_list": [
                        {
                            "account_id": "222222",
                            "account_name": "Final Qianchuan Account",
                            "account_type": "QIANCHUAN",
                        }
                    ],
                    "page_info": {"page": 1, "page_size": 100, "total_number": 1},
                },
                "ebp-rid",
            )
        if endpoint == ADVERTISER_PUBLIC_INFO:
            requested = list((query or {}).get("advertiser_ids") or [])
            rows = []
            if "222222" in requested:
                rows.append({"account_id": "222222", "account_name": "Final Name"})
            # OAuth principal 111111 intentionally does not appear: it cannot become final advertiser.
            return _api({"account_list": rows}, "public-rid")
        raise AssertionError(endpoint)


def _database(tmp_path: Path) -> Database:
    db = Database(DatabaseConfig(tmp_path / "runtime.db"))
    with db.connect() as conn:
        create_schema_v1(conn)
    return db


def test_pagination_requires_verifiable_metadata() -> None:
    class Client:
        def get(self, endpoint, *, query=None, access_token, advertiser_id=""):
            return _api({"list": [{"id": "1"}]})

    with pytest.raises(OpenApiResponseError, match="pagination metadata"):
        get_all_pages(Client(), "/open_api/test/", query={}, access_token="x")  # type: ignore[arg-type]


def test_pagination_reads_all_pages_and_validates_total() -> None:
    class Client:
        def get(self, endpoint, *, query=None, access_token, advertiser_id=""):
            page = int((query or {}).get("page", 1))
            if page == 1:
                return _api(
                    {
                        "list": [{"id": "1"}, {"id": "2"}],
                        "page_info": {"page_size": 2, "total_number": 3},
                    },
                    "r1",
                )
            return _api(
                {
                    "list": [{"id": "3"}],
                    "page_info": {"page_size": 2, "total_number": 3},
                },
                "r2",
            )

    rows, request_ids = get_all_pages(
        Client(),  # type: ignore[arg-type]
        "/open_api/test/",
        query={},
        access_token="x",
        page_size=2,
        identity_getter=lambda row: row["id"],
    )
    assert [row["id"] for row in rows] == ["1", "2", "3"]
    assert request_ids == ["r1", "r2"]


def test_pagination_refreshes_token_at_most_once_and_retries_current_page() -> None:
    calls = []
    refresh_calls = []

    class Client:
        def get(self, endpoint, *, query=None, access_token, advertiser_id=""):
            page = int((query or {}).get("page", 1))
            calls.append((page, access_token))
            if page == 1 and access_token == "old-token":
                raise OpenApiTokenError("access_token expired", code="TOKEN_EXPIRED")
            return _api(
                {
                    "list": [{"id": str(page)}],
                    "page_info": {"page_size": 1, "total_number": 2},
                },
                f"r{page}",
            )

    def refresh():
        refresh_calls.append(True)
        return "new-token"

    rows, request_ids = get_all_pages(
        Client(),  # type: ignore[arg-type]
        "/open_api/test/",
        query={},
        access_token="old-token",
        page_size=1,
        identity_getter=lambda row: row["id"],
        refresh_access_token=refresh,
    )
    assert [row["id"] for row in rows] == ["1", "2"]
    assert request_ids == ["r1", "r2"]
    assert refresh_calls == [True]
    assert calls == [(1, "old-token"), (1, "new-token"), (2, "new-token")]


def test_pagination_does_not_refresh_repeatedly_after_new_token_fails() -> None:
    refresh_calls = []

    class Client:
        def get(self, endpoint, *, query=None, access_token, advertiser_id=""):
            raise OpenApiTokenError("still expired", code="TOKEN_EXPIRED")

    def refresh():
        refresh_calls.append(True)
        return "new-token"

    with pytest.raises(OpenApiTokenError):
        get_all_pages(
            Client(),  # type: ignore[arg-type]
            "/open_api/test/",
            query={},
            access_token="old-token",
            refresh_access_token=refresh,
        )
    assert refresh_calls == [True]


def test_ebp_final_identity_never_falls_back_to_oauth_style_id() -> None:
    with pytest.raises(OpenApiContractError, match="account_id"):
        normalize_final_advertiser(
            {"advertiser_id": "123456", "advertiser_name": "not enough evidence"},
            source="EBP",
        )


def test_plan_classification_conflict_is_explicit() -> None:
    plan = normalize_plan(
        {
            "ad_info": {
                "id": "900001",
                "name": "x",
                "marketing_goal": "VIDEO_PROM_GOODS",
                "adlab_scene": "UNI_PROJECT",
                "status": "DELIVERY_OK",
            }
        },
        advertiser_id="222222",
        expected_marketing_goal="LIVE_PROM_GOODS",
        expected_adlab_scene="OVERALL_PROJECT",
    )
    assert plan.classification_status == "CONFLICT"
    assert plan.is_delivering is True


def test_discovery_persists_only_verified_final_advertiser(tmp_path: Path) -> None:
    db = _database(tmp_path)
    writer = StorageWriter(db)
    writer.start()
    try:
        now = "2026-08-30T00:00:00+00:00"
        writer.execute(
            """INSERT INTO qianchuan_auth_profile(
               auth_profile_id,app_id,encrypted_app_secret,auth_status,created_at,updated_at
               ) VALUES('auth-1','123456','cipher','ACTIVE',?,?)""",
            (now, now),
        ).result(timeout=5)
        store = AccountStore(db, writer)
        service = AccountDiscoveryService(DiscoveryClient(), FakeTokenProvider(), store)  # type: ignore[arg-type]
        result = service.discover("auth-1")
        assert [account.advertiser_id for account in result.accounts] == ["222222"]
        rows = store.list_accounts()
        assert len(rows) == 1
        assert rows[0]["advertiser_id"] == "222222"
        assert rows[0]["account_name"] == "Final Name"
        assert rows[0]["enabled"] == 0
        # 111111 is an OAuth principal only and PublicInfo did not confirm it.
        assert result.complete is False
    finally:
        writer.close()


def test_account_enable_limit_is_ten(tmp_path: Path) -> None:
    db = _database(tmp_path)
    writer = StorageWriter(db)
    writer.start()
    try:
        now = "2026-08-30T00:00:00+00:00"
        writer.execute(
            """INSERT INTO qianchuan_auth_profile(
               auth_profile_id,app_id,encrypted_app_secret,auth_status,created_at,updated_at
               ) VALUES('auth-1','123456','cipher','ACTIVE',?,?)""",
            (now, now),
        ).result(timeout=5)
        store = AccountStore(db, writer)
        accounts = tuple(
            normalize_final_advertiser(
                {"account_id": str(500000 + i), "account_name": f"a{i}"}, source="EBP"
            )
            for i in range(11)
        )
        store.upsert_discovered(
            accounts,
            auth_profile_id="auth-1",
            discovery_sources={account.advertiser_id: ["EBP"] for account in accounts},
        )
        for account in accounts[:10]:
            store.set_enabled(account.advertiser_id, True)
        with pytest.raises(ValueError, match="10"):
            store.set_enabled(accounts[10].advertiser_id, True)
    finally:
        writer.close()
