from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from commercial_v1.qianchuan.client import ApiResponse
from commercial_v1.qianchuan.contracts import OAUTH_ACCESS_TOKEN, OAUTH_REFRESH_TOKEN
from commercial_v1.qianchuan.errors import OpenApiTokenError
from commercial_v1.qianchuan.token_provider import OAuthTokenProvider
from commercial_v1.storage.database import Database, DatabaseConfig
from commercial_v1.storage.schema import create_schema_v1
from commercial_v1.storage.writer import StorageWriter


class FakeProtector:
    def protect(self, value: str) -> str:
        return "cipher:" + value[::-1]

    def unprotect(self, value: str) -> str:
        assert value.startswith("cipher:")
        return value[len("cipher:"):][::-1]


class FakeClient:
    def __init__(self) -> None:
        self.calls = []
        self.responses = []

    def post_oauth(self, endpoint, payload):
        self.calls.append((endpoint, dict(payload)))
        data = self.responses.pop(0)
        return ApiResponse(data=data, raw={"code": 0, "data": data}, request_id="rid", code="0", message="", local_request_uid="local")


def _setup(tmp_path: Path, now: datetime):
    db = Database(DatabaseConfig(tmp_path / "runtime.db"))
    with db.connect() as conn:
        create_schema_v1(conn)
    writer = StorageWriter(db)
    writer.start()
    client = FakeClient()
    provider = OAuthTokenProvider(
        db,
        writer,
        client,  # type: ignore[arg-type]
        protector=FakeProtector(),
        clock=lambda: now,
    )
    return db, writer, client, provider


def test_credentials_and_tokens_are_not_stored_plaintext(tmp_path: Path) -> None:
    now = datetime(2026, 8, 30, tzinfo=timezone.utc)
    db, writer, client, provider = _setup(tmp_path, now)
    try:
        profile = provider.save_credentials("123456", "app-secret-value", auth_profile_id="p1")
        client.responses.append({"access_token": "access-secret-value", "refresh_token": "refresh-secret-value", "expires_in": 86400})
        bundle = provider.exchange_authorization_code(profile, "auth-code-123")
        assert bundle.access_token == "access-secret-value"

        with db.connect(readonly=True) as conn:
            row = conn.execute("SELECT * FROM qianchuan_auth_profile WHERE auth_profile_id='p1'").fetchone()
            rendered = "|".join(str(value or "") for value in row)
        assert "app-secret-value" not in rendered
        assert "access-secret-value" not in rendered
        assert "refresh-secret-value" not in rendered
        assert str(row["auth_status"]) == "ACTIVE"
        assert client.calls[0][0] == OAUTH_ACCESS_TOKEN
        assert client.calls[0][1] == {"app_id": "123456", "secret": "app-secret-value", "auth_code": "auth-code-123"}
    finally:
        writer.close()


def test_unexpired_access_token_does_not_refresh(tmp_path: Path) -> None:
    now = datetime(2026, 8, 30, tzinfo=timezone.utc)
    _db, writer, client, provider = _setup(tmp_path, now)
    try:
        provider.save_credentials("123456", "app-secret-value", auth_profile_id="p1")
        client.responses.append({"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 86400})
        provider.exchange_authorization_code("p1", "auth-code-123")
        client.calls.clear()
        assert provider.get_access_token("p1") == "access-1"
        assert client.calls == []
    finally:
        writer.close()


def test_expired_access_refresh_rotates_both_tokens_atomically(tmp_path: Path) -> None:
    now = datetime(2026, 8, 30, tzinfo=timezone.utc)
    db, writer, client, provider = _setup(tmp_path, now)
    try:
        provider.save_credentials("123456", "app-secret-value", auth_profile_id="p1")
        client.responses.append({"access_token": "access-old", "refresh_token": "refresh-old", "expires_in": 1})
        provider.exchange_authorization_code("p1", "auth-code-123")

        later = now + timedelta(minutes=10)
        provider._clock = lambda: later  # test clock injection after persisted bundle
        client.calls.clear()
        client.responses.append({"access_token": "access-new", "refresh_token": "refresh-new", "expires_in": 86400})
        assert provider.get_access_token("p1") == "access-new"
        assert client.calls == [
            (
                OAUTH_REFRESH_TOKEN,
                {
                    "app_id": "123456",
                    "secret": "app-secret-value",
                    "grant_type": "refresh_token",
                    "refresh_token": "refresh-old",
                },
            )
        ]
        bundle = provider.load_bundle("p1")
        assert bundle.access_token == "access-new"
        assert bundle.refresh_token == "refresh-new"
        with db.connect(readonly=True) as conn:
            row = conn.execute("SELECT auth_status,last_refresh_at FROM qianchuan_auth_profile WHERE auth_profile_id='p1'").fetchone()
        assert row["auth_status"] == "ACTIVE"
        assert row["last_refresh_at"] is not None
    finally:
        writer.close()


def test_refresh_token_default_lifetime_is_thirty_days_when_api_omits_ttl(tmp_path: Path) -> None:
    now = datetime(2026, 8, 30, tzinfo=timezone.utc)
    _db, writer, client, provider = _setup(tmp_path, now)
    try:
        provider.save_credentials("123456", "app-secret-value", auth_profile_id="p1")
        client.responses.append({"access_token": "access", "refresh_token": "refresh", "expires_in": 86400})
        bundle = provider.exchange_authorization_code("p1", "auth-code-123")
        assert bundle.refresh_token_expires_at == now + timedelta(days=30)
    finally:
        writer.close()


def test_changing_app_secret_clears_existing_tokens(tmp_path: Path) -> None:
    now = datetime(2026, 8, 30, tzinfo=timezone.utc)
    _db, writer, client, provider = _setup(tmp_path, now)
    try:
        provider.save_credentials("123456", "app-secret-value", auth_profile_id="p1")
        client.responses.append({"access_token": "access", "refresh_token": "refresh", "expires_in": 86400})
        provider.exchange_authorization_code("p1", "auth-code-123")
        provider.save_credentials("123456", "new-secret-value", auth_profile_id="p1")
        with pytest.raises(OpenApiTokenError, match="尚未完成授权"):
            provider.load_bundle("p1")
    finally:
        writer.close()
