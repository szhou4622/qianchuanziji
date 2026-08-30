from pathlib import Path

import pytest

from commercial_v1.app import CommercialApplication, RuntimeBlockedError
from commercial_v1.storage.database import Database, DatabaseConfig
from commercial_v1.storage.schema import create_schema_v1


class FakeMutex:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.closed = False

    def acquire(self) -> bool:
        return self.allowed

    def close(self) -> None:
        self.closed = True


def test_application_starts_fresh_database_and_exposes_diagnostics(tmp_path: Path) -> None:
    mutex = FakeMutex()
    app = CommercialApplication(data_dir=tmp_path, mutex=mutex)  # type: ignore[arg-type]
    app.start()
    try:
        assert app.started is True
        assert (tmp_path / "runtime.db").exists()
        snapshot = app.diagnostics_snapshot()
        assert snapshot["schema_version"] == 1
        assert snapshot["runtime"]["state"] == "RUNNING"
        assert snapshot["storage_writer"]["alive"] is True
        assert snapshot["startup_recovery"]["unresolved_execution_count"] == 0
        assert snapshot["license_runtime"]["status"] == "INVALID"
    finally:
        app.stop()
    assert mutex.closed is True
    assert app.started is False


def test_application_refuses_database_newer_than_supported_schema(tmp_path: Path) -> None:
    db = Database(DatabaseConfig(tmp_path / "runtime.db"))
    with db.connect() as conn:
        create_schema_v1(conn)
        conn.execute("UPDATE schema_meta SET value='999' WHERE key='schema_version'")
        conn.commit()

    mutex = FakeMutex()
    app = CommercialApplication(data_dir=tmp_path, mutex=mutex)  # type: ignore[arg-type]
    with pytest.raises(RuntimeBlockedError, match="DB_SCHEMA_NEWER_THAN_APP"):
        app.start()
    assert mutex.closed is True
    assert app.started is False


def test_application_rejects_second_instance_before_touching_database(tmp_path: Path) -> None:
    mutex = FakeMutex(allowed=False)
    app = CommercialApplication(data_dir=tmp_path, mutex=mutex)  # type: ignore[arg-type]
    with pytest.raises(Exception, match="already running"):
        app.start()
    assert not (tmp_path / "runtime.db").exists()
