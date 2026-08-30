from datetime import datetime, timedelta, timezone
from pathlib import Path

from commercial_v1.license.runtime_state import LicenseRuntimeStateStore
from commercial_v1.storage.database import Database, DatabaseConfig
from commercial_v1.storage.schema import create_schema_v1
from commercial_v1.storage.writer import StorageWriter


class FakeClock:
    def __init__(self):
        self.value = datetime(2026, 8, 30, 0, 0, 0, tzinfo=timezone.utc)
    def __call__(self):
        return self.value
    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def _env(tmp_path: Path):
    db = Database(DatabaseConfig(tmp_path / "runtime.db"))
    with db.connect() as conn:
        create_schema_v1(conn)
    writer = StorageWriter(db)
    writer.start()
    clock = FakeClock()
    return writer, clock, LicenseRuntimeStateStore(db, writer, clock=clock)


def test_network_failure_allows_normal_business_for_30_minutes(tmp_path: Path) -> None:
    writer, clock, state = _env(tmp_path)
    state.mark_online_valid()
    grace = state.mark_network_failure()
    assert grace.status == "NETWORK_GRACE"
    assert grace.normal_business_allowed is True
    first_failure = grace.first_network_failure_at
    grace_until = grace.network_grace_until

    clock.advance(10 * 60)
    repeated = state.mark_network_failure()
    assert repeated.first_network_failure_at == first_failure
    assert repeated.network_grace_until == grace_until
    assert repeated.normal_business_allowed is True

    clock.advance(19 * 60 + 59)
    assert state.get().status == "NETWORK_GRACE"
    clock.advance(1)
    expired = state.get()
    assert expired.status == "INVALID"
    assert expired.last_error_code == "LICENSE_GRACE_EXPIRED"
    writer.close()


def test_explicit_invalid_cancels_grace_immediately(tmp_path: Path) -> None:
    writer, clock, state = _env(tmp_path)
    state.mark_online_valid()
    state.mark_network_failure()
    clock.advance(60)
    invalid = state.mark_explicit_invalid("DEVICE_MISMATCH")
    assert invalid.status == "INVALID"
    assert invalid.normal_business_allowed is False
    writer.close()


def test_no_previous_valid_license_means_no_network_grace(tmp_path: Path) -> None:
    writer, _, state = _env(tmp_path)
    result = state.mark_network_failure()
    assert result.status == "INVALID"
    assert result.normal_business_allowed is False
    writer.close()
