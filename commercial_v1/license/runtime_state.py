"""30 分钟激活服务器网络宽限状态机。

仅“服务器网络不可达/临时网络类失败”可进入宽限；明确失效必须立即 INVALID。
重复网络失败绝不能重置首次失败时间。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from commercial_v1.storage.database import Database
from commercial_v1.storage.writer import StorageWriter

Clock = Callable[[], datetime]
GRACE_SECONDS = 30 * 60


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class LicenseRuntimeState:
    status: str
    last_online_verified_at: str | None
    first_network_failure_at: str | None
    network_grace_until: str | None
    last_error_code: str | None
    updated_at: str

    @property
    def normal_business_allowed(self) -> bool:
        return self.status in {"ACTIVE", "NETWORK_GRACE"}


class LicenseRuntimeStateStore:
    def __init__(self, database: Database, writer: StorageWriter, *, clock: Clock = utc_now) -> None:
        self._database = database
        self._writer = writer
        self._clock = clock

    def mark_online_valid(self) -> LicenseRuntimeState:
        now = _iso(self._clock())
        self._writer.execute(
            """INSERT INTO license_runtime_state(singleton_id,status,last_online_verified_at,first_network_failure_at,network_grace_until,last_error_code,updated_at)
            VALUES(1,'ACTIVE',?,NULL,NULL,NULL,?)
            ON CONFLICT(singleton_id) DO UPDATE SET status='ACTIVE',last_online_verified_at=excluded.last_online_verified_at,first_network_failure_at=NULL,network_grace_until=NULL,last_error_code=NULL,updated_at=excluded.updated_at""",
            (now, now),
        ).result(timeout=5)
        return self.get()

    def mark_network_failure(self, error_code: str = "LICENSE_NETWORK_ERROR") -> LicenseRuntimeState:
        now_dt = self._clock()
        now = _iso(now_dt)

        def work(conn):
            row = conn.execute("SELECT * FROM license_runtime_state WHERE singleton_id=1").fetchone()
            if row is None or not row["last_online_verified_at"] or row["status"] == "INVALID":
                conn.execute(
                    "INSERT INTO license_runtime_state(singleton_id,status,last_error_code,updated_at) VALUES(1,'INVALID',?,?) ON CONFLICT(singleton_id) DO UPDATE SET status='INVALID',last_error_code=excluded.last_error_code,updated_at=excluded.updated_at",
                    (error_code, now),
                )
                return

            if row["status"] == "NETWORK_GRACE" and row["first_network_failure_at"] and row["network_grace_until"]:
                if str(row["network_grace_until"]) <= now:
                    conn.execute("UPDATE license_runtime_state SET status='INVALID',last_error_code='LICENSE_GRACE_EXPIRED',updated_at=? WHERE singleton_id=1", (now,))
                else:
                    # 重复失败绝不刷新 first_network_failure_at / network_grace_until。
                    conn.execute("UPDATE license_runtime_state SET last_error_code=?,updated_at=? WHERE singleton_id=1", (error_code, now))
                return

            grace_until = _iso(now_dt + timedelta(seconds=GRACE_SECONDS))
            conn.execute(
                "UPDATE license_runtime_state SET status='NETWORK_GRACE',first_network_failure_at=?,network_grace_until=?,last_error_code=?,updated_at=? WHERE singleton_id=1",
                (now, grace_until, error_code, now),
            )

        self._writer.transaction(work).result(timeout=5)
        return self.get()

    def mark_explicit_invalid(self, error_code: str) -> LicenseRuntimeState:
        now = _iso(self._clock())
        self._writer.execute(
            """INSERT INTO license_runtime_state(singleton_id,status,last_error_code,updated_at)
            VALUES(1,'INVALID',?,?)
            ON CONFLICT(singleton_id) DO UPDATE SET status='INVALID',last_error_code=excluded.last_error_code,updated_at=excluded.updated_at""",
            (error_code, now),
        ).result(timeout=5)
        return self.get()

    def get(self) -> LicenseRuntimeState:
        now = _iso(self._clock())
        with self._database.connect(readonly=True) as conn:
            row = conn.execute("SELECT * FROM license_runtime_state WHERE singleton_id=1").fetchone()
        if row is None:
            return LicenseRuntimeState("INVALID", None, None, None, "LICENSE_NOT_VERIFIED", now)
        state = self._from_row(row)
        if state.status == "NETWORK_GRACE" and state.network_grace_until and state.network_grace_until <= now:
            return self._expire_grace(now)
        return state

    def _expire_grace(self, now: str) -> LicenseRuntimeState:
        self._writer.execute(
            "UPDATE license_runtime_state SET status='INVALID',last_error_code='LICENSE_GRACE_EXPIRED',updated_at=? WHERE singleton_id=1 AND status='NETWORK_GRACE'",
            (now,),
        ).result(timeout=5)
        with self._database.connect(readonly=True) as conn:
            row = conn.execute("SELECT * FROM license_runtime_state WHERE singleton_id=1").fetchone()
        assert row is not None
        return self._from_row(row)

    @staticmethod
    def _from_row(row) -> LicenseRuntimeState:
        return LicenseRuntimeState(
            status=str(row["status"]),
            last_online_verified_at=row["last_online_verified_at"],
            first_network_failure_at=row["first_network_failure_at"],
            network_grace_until=row["network_grace_until"],
            last_error_code=row["last_error_code"],
            updated_at=str(row["updated_at"]),
        )
