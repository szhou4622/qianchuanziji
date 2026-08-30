"""千川 OAuth 凭证与 Token Provider。

App Secret / Access Token / Refresh Token 只以 DPAPI 密文进入 SQLite。
刷新时 access_token 与 refresh_token 必须一起原子替换。
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

from commercial_v1.security.dpapi import protect_text, unprotect_text
from commercial_v1.storage.database import Database
from commercial_v1.storage.writer import StorageWriter

from .client import OpenApiClient
from .contracts import OAUTH_ACCESS_TOKEN, OAUTH_REFRESH_TOKEN
from .errors import OpenApiContractError, OpenApiTokenError


class TextProtector(Protocol):
    def protect(self, value: str) -> str: ...
    def unprotect(self, value: str) -> str: ...


class WindowsDpapiProtector:
    def protect(self, value: str) -> str:
        return protect_text(value)

    def unprotect(self, value: str) -> str:
        return unprotect_text(value)


Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class TokenBundle:
    auth_profile_id: str
    app_id: str
    access_token: str
    refresh_token: str
    access_token_expires_at: datetime
    refresh_token_expires_at: datetime | None

    def access_usable(self, now: datetime, *, skew_seconds: int = 120) -> bool:
        return bool(self.access_token) and self.access_token_expires_at > now + timedelta(seconds=skew_seconds)


class OAuthTokenProvider:
    def __init__(
        self,
        database: Database,
        writer: StorageWriter,
        client: OpenApiClient,
        *,
        protector: TextProtector | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self._database = database
        self._writer = writer
        self._client = client
        self._protector = protector or WindowsDpapiProtector()
        self._clock = clock
        self._lock = threading.RLock()

    def save_credentials(self, app_id: str, app_secret: str, *, auth_profile_id: str | None = None) -> str:
        aid = str(app_id or "").strip()
        secret = str(app_secret or "").strip()
        if not aid.isdigit() or len(aid) < 6:
            raise ValueError("App ID 格式不正确")
        if len(secret) < 6:
            raise ValueError("请输入有效 App Secret")
        profile_id = str(auth_profile_id or uuid.uuid4())
        encrypted_secret = self._protector.protect(secret)
        now = _iso(self._clock())

        def work(conn):
            existing = conn.execute(
                "SELECT app_id,encrypted_app_secret,created_at FROM qianchuan_auth_profile WHERE auth_profile_id=?",
                (profile_id,),
            ).fetchone()
            created_at = str(existing["created_at"]) if existing is not None else now
            same_credentials = bool(
                existing is not None
                and str(existing["app_id"]) == aid
                and str(existing["encrypted_app_secret"] or "")
                and self._safe_same_secret(str(existing["encrypted_app_secret"]), secret)
            )
            if same_credentials:
                conn.execute(
                    "UPDATE qianchuan_auth_profile SET app_id=?,encrypted_app_secret=?,updated_at=? WHERE auth_profile_id=?",
                    (aid, encrypted_secret, now, profile_id),
                )
            else:
                conn.execute(
                    """INSERT INTO qianchuan_auth_profile(
                       auth_profile_id,app_id,encrypted_app_secret,encrypted_access_token,
                       encrypted_refresh_token,access_token_expires_at,refresh_token_expires_at,
                       auth_status,last_refresh_at,last_error_code,last_error_message,created_at,updated_at
                       ) VALUES(?,?,?,NULL,NULL,NULL,NULL,'CONFIGURED',NULL,NULL,NULL,?,?)
                       ON CONFLICT(auth_profile_id) DO UPDATE SET
                         app_id=excluded.app_id,
                         encrypted_app_secret=excluded.encrypted_app_secret,
                         encrypted_access_token=NULL,
                         encrypted_refresh_token=NULL,
                         access_token_expires_at=NULL,
                         refresh_token_expires_at=NULL,
                         auth_status='CONFIGURED',
                         last_refresh_at=NULL,
                         last_error_code=NULL,
                         last_error_message=NULL,
                         updated_at=excluded.updated_at""",
                    (profile_id, aid, encrypted_secret, created_at, now),
                )
            return profile_id

        return str(self._writer.transaction(work).result(timeout=5))

    def exchange_authorization_code(self, auth_profile_id: str, auth_code: str) -> TokenBundle:
        code = str(auth_code or "").strip()
        if len(code) < 6:
            raise ValueError("auth_code 无效")
        with self._lock:
            profile = self._load_profile(auth_profile_id, require_tokens=False)
            response = self._client.post_oauth(
                OAUTH_ACCESS_TOKEN,
                {"app_id": profile["app_id"], "secret": profile["app_secret"], "auth_code": code},
            )
            bundle = self._bundle_from_response(auth_profile_id, profile["app_id"], response.data)
            self._save_bundle(bundle, status="ACTIVE", refreshed=False)
            return bundle

    def get_access_token(self, auth_profile_id: str, *, force_refresh: bool = False) -> str:
        with self._lock:
            bundle = self.load_bundle(auth_profile_id)
            if not force_refresh and bundle.access_usable(self._clock()):
                return bundle.access_token
            refreshed = self._refresh(bundle)
            return refreshed.access_token

    def load_bundle(self, auth_profile_id: str) -> TokenBundle:
        profile = self._load_profile(auth_profile_id, require_tokens=True)
        access_expiry = _parse_iso(profile["access_token_expires_at"])
        if access_expiry is None:
            raise OpenApiTokenError("本地 access_token 过期时间缺失", code="LOCAL_TOKEN_EXPIRY_MISSING")
        return TokenBundle(
            auth_profile_id=auth_profile_id,
            app_id=profile["app_id"],
            access_token=profile["access_token"],
            refresh_token=profile["refresh_token"],
            access_token_expires_at=access_expiry,
            refresh_token_expires_at=_parse_iso(profile["refresh_token_expires_at"]),
        )

    def mark_reauth_required(self, auth_profile_id: str, *, code: str = "") -> None:
        now = _iso(self._clock())
        self._writer.execute(
            "UPDATE qianchuan_auth_profile SET auth_status='REAUTH_REQUIRED',last_error_code=?,updated_at=? WHERE auth_profile_id=?",
            (str(code or ""), now, auth_profile_id),
        ).result(timeout=5)

    def _refresh(self, bundle: TokenBundle) -> TokenBundle:
        profile = self._load_profile(bundle.auth_profile_id, require_tokens=True)
        if bundle.refresh_token_expires_at is not None and bundle.refresh_token_expires_at <= self._clock():
            self.mark_reauth_required(bundle.auth_profile_id, code="LOCAL_REFRESH_TOKEN_EXPIRED")
            raise OpenApiTokenError("refresh_token 已过期，请重新授权", code="LOCAL_REFRESH_TOKEN_EXPIRED")
        try:
            response = self._client.post_oauth(
                OAUTH_REFRESH_TOKEN,
                {
                    "app_id": profile["app_id"],
                    "secret": profile["app_secret"],
                    "grant_type": "refresh_token",
                    "refresh_token": profile["refresh_token"],
                },
            )
            refreshed = self._bundle_from_response(bundle.auth_profile_id, profile["app_id"], response.data)
        except OpenApiTokenError as exc:
            self.mark_reauth_required(bundle.auth_profile_id, code=exc.code)
            raise
        self._save_bundle(refreshed, status="ACTIVE", refreshed=True)
        return refreshed

    def _bundle_from_response(self, auth_profile_id: str, app_id: str, data: object) -> TokenBundle:
        if not isinstance(data, dict):
            raise OpenApiContractError("OAuth 响应 data 不是对象", code="OAUTH_INVALID_DATA")
        access_token = str(data.get("access_token") or "").strip()
        refresh_token = str(data.get("refresh_token") or "").strip()
        try:
            expires_in = int(float(data.get("expires_in") or 0))
        except (TypeError, ValueError) as exc:
            raise OpenApiContractError("OAuth expires_in 无效", code="OAUTH_INVALID_EXPIRES_IN") from exc
        if not access_token or not refresh_token:
            raise OpenApiContractError("OAuth 响应缺少 access_token 或 refresh_token", code="OAUTH_TOKEN_MISSING")
        if expires_in <= 0:
            raise OpenApiContractError("OAuth 响应缺少有效 expires_in", code="OAUTH_EXPIRY_MISSING")

        now = self._clock()
        refresh_expiry: datetime | None = None
        raw_refresh_expires = data.get("refresh_expires_in") or data.get("refresh_token_expires_in")
        if raw_refresh_expires not in (None, ""):
            try:
                seconds = int(float(raw_refresh_expires))
            except (TypeError, ValueError) as exc:
                raise OpenApiContractError("OAuth refresh token expiry 无效", code="OAUTH_INVALID_REFRESH_EXPIRY") from exc
            if seconds > 0:
                refresh_expiry = now + timedelta(seconds=seconds)
        # 当前正式契约确认 refresh_token 默认约30天；仅在官方响应不返回 TTL 时使用该默认生命周期。
        if refresh_expiry is None:
            refresh_expiry = now + timedelta(days=30)

        return TokenBundle(
            auth_profile_id=auth_profile_id,
            app_id=app_id,
            access_token=access_token,
            refresh_token=refresh_token,
            access_token_expires_at=now + timedelta(seconds=expires_in),
            refresh_token_expires_at=refresh_expiry,
        )

    def _save_bundle(self, bundle: TokenBundle, *, status: str, refreshed: bool) -> None:
        now = _iso(self._clock())
        encrypted_access = self._protector.protect(bundle.access_token)
        encrypted_refresh = self._protector.protect(bundle.refresh_token)
        access_expiry = _iso(bundle.access_token_expires_at)
        refresh_expiry = _iso(bundle.refresh_token_expires_at) if bundle.refresh_token_expires_at else None

        def work(conn):
            changed = conn.execute(
                """UPDATE qianchuan_auth_profile
                   SET encrypted_access_token=?,encrypted_refresh_token=?,
                       access_token_expires_at=?,refresh_token_expires_at=?,auth_status=?,
                       last_refresh_at=?,last_error_code=NULL,last_error_message=NULL,updated_at=?
                   WHERE auth_profile_id=?""",
                (
                    encrypted_access,
                    encrypted_refresh,
                    access_expiry,
                    refresh_expiry,
                    status,
                    now if refreshed else None,
                    now,
                    bundle.auth_profile_id,
                ),
            ).rowcount
            if changed != 1:
                raise OpenApiContractError("auth profile 不存在", code="AUTH_PROFILE_MISSING")

        self._writer.transaction(work).result(timeout=5)

    def _load_profile(self, auth_profile_id: str, *, require_tokens: bool) -> dict[str, str | None]:
        with self._database.connect(readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM qianchuan_auth_profile WHERE auth_profile_id=?",
                (auth_profile_id,),
            ).fetchone()
        if row is None:
            raise OpenApiContractError("auth profile 不存在", code="AUTH_PROFILE_MISSING")
        encrypted_secret = str(row["encrypted_app_secret"] or "")
        if not encrypted_secret:
            raise OpenApiContractError("App Secret 未配置", code="APP_SECRET_MISSING")
        result: dict[str, str | None] = {
            "app_id": str(row["app_id"]),
            "app_secret": self._protector.unprotect(encrypted_secret),
            "access_token_expires_at": row["access_token_expires_at"],
            "refresh_token_expires_at": row["refresh_token_expires_at"],
            "access_token": "",
            "refresh_token": "",
        }
        if require_tokens:
            encrypted_access = str(row["encrypted_access_token"] or "")
            encrypted_refresh = str(row["encrypted_refresh_token"] or "")
            if not encrypted_access or not encrypted_refresh:
                raise OpenApiTokenError("千川 OAuth 尚未完成授权", code="LOCAL_TOKEN_MISSING")
            result["access_token"] = self._protector.unprotect(encrypted_access)
            result["refresh_token"] = self._protector.unprotect(encrypted_refresh)
        return result

    def _safe_same_secret(self, encrypted_secret: str, plain_secret: str) -> bool:
        try:
            return self._protector.unprotect(encrypted_secret) == plain_secret
        except Exception:
            return False
