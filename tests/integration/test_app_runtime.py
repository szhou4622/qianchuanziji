from pathlib import Path

import pytest

from commercial_v1.app import CommercialApplication, RuntimeBlockedError
from commercial_v1.qianchuan import CONTROL_5M, MATERIAL_5M, PLAN_STATUS_CHECK
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


def test_application_starts_fresh_database_and_exposes_phase3_services(tmp_path: Path) -> None:
    mutex = FakeMutex()
    app = CommercialApplication(data_dir=tmp_path, mutex=mutex)  # type: ignore[arg-type]
    app.start()
    try:
        assert app.started is True
        assert (tmp_path / "runtime.db").exists()
        assert app.open_api_client is not None
        assert app.oauth_tokens is not None
        assert app.account_discovery is not None
        assert app.plan_catalog is not None
        assert app.monitor_plan_store is not None
        assert app.plan_monitor is not None
        assert app.plan_state_handler is not None
        assert app.plan_state_scheduler is not None
        assert app.hot_collection is not None
        assert app.material_hot_handler is not None
        assert app.control_hot_handler is not None
        assert app.hot_collection_scheduler is not None
        assert len(app.hot_workers) == 2

        snapshot = app.diagnostics_snapshot()
        assert snapshot["schema_version"] == 1
        assert snapshot["runtime"]["state"] == "RUNNING"
        assert snapshot["storage_writer"]["alive"] is True
        assert snapshot["startup_recovery"]["unresolved_execution_count"] == 0
        assert snapshot["license_runtime"]["status"] == "INVALID"

        # `job_worker` 是 Phase 2 既有诊断契约，Phase 3 不允许破坏。
        assert PLAN_STATUS_CHECK in snapshot["runtime"]["components"]["job_worker"]["job_types"]
        assert "plan_state_scheduler" in snapshot["runtime"]["components"]

        # Phase 3 热采集已接入 Runtime，但软件激活无效时 Scheduler 不产生任何网络 Job。
        hot_types = set()
        for index in (1, 2):
            component = snapshot["runtime"]["components"][f"hot_read_worker_{index}"]
            hot_types.update(component["job_types"])
        assert {MATERIAL_5M, CONTROL_5M} <= hot_types
        assert "hot_collection_scheduler" in snapshot["runtime"]["components"]
        assert app.plan_state_scheduler.run_once() == 0
        assert app.hot_collection_scheduler.run_once()["enqueued"] == 0
        assert app.plan_state_scheduler.health_snapshot()["license_blocked"] is True
        assert app.hot_collection_scheduler.health_snapshot()["license_blocked"] is True
        with app.database.connect(readonly=True) as conn:  # type: ignore[union-attr]
            assert conn.execute("SELECT COUNT(*) FROM background_job").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM collection_batch").fetchone()[0] == 0
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
