from pathlib import Path

import pytest

from commercial_v1.app import CommercialApplication, RuntimeBlockedError
from commercial_v1.qianchuan import (
    CONTROL_5M,
    CONTROL_CONFIRM,
    MATERIAL_5M,
    MATERIAL_CONFIRM,
    PLAN_STATUS_CHECK,
)
from commercial_v1.storage.database import Database, DatabaseConfig
from commercial_v1.storage.schema import create_schema_v1
from commercial_v1.strategy import STRATEGY_CONTROL_EVALUATE, STRATEGY_MATERIAL_EVALUATE


class FakeMutex:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.closed = False

    def acquire(self) -> bool:
        return self.allowed

    def close(self) -> None:
        self.closed = True


def test_application_starts_fresh_database_and_exposes_phase4_services(tmp_path: Path) -> None:
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
        assert app.hot_confirmation is not None
        assert app.hot_confirmation_scheduler is not None
        assert app.hot_account_gate is not None
        assert len(app.hot_workers) == 6

        assert app.strategy_store is not None
        assert app.strategy_evaluation is not None
        assert app.strategy_enqueuer is not None
        assert app.material_strategy_handler is not None
        assert app.control_strategy_handler is not None
        assert app.strategy_worker is not None

        snapshot = app.diagnostics_snapshot()
        assert snapshot["schema_version"] == 1
        assert snapshot["runtime"]["state"] == "RUNNING"
        assert snapshot["storage_writer"]["alive"] is True
        assert snapshot["startup_recovery"]["unresolved_execution_count"] == 0
        assert snapshot["license_runtime"]["status"] == "INVALID"
        assert snapshot["hot_account_concurrency"]["max_per_advertiser"] == 2
        assert snapshot["strategy"]["enabled_strategies"] == 0
        assert snapshot["strategy"]["hit_rows"] == 0

        # `job_worker` 是 Phase 2 既有诊断契约，后续阶段不允许破坏。
        assert PLAN_STATUS_CHECK in snapshot["runtime"]["components"]["job_worker"]["job_types"]
        assert "plan_state_scheduler" in snapshot["runtime"]["components"]

        hot_types = set()
        for index in range(1, 7):
            component = snapshot["runtime"]["components"][f"hot_read_worker_{index}"]
            hot_types.update(component["job_types"])
        assert {MATERIAL_5M, CONTROL_5M, MATERIAL_CONFIRM, CONTROL_CONFIRM} <= hot_types
        assert "hot_collection_scheduler" in snapshot["runtime"]["components"]
        assert "hot_confirmation_scheduler" in snapshot["runtime"]["components"]

        strategy_component = snapshot["runtime"]["components"]["strategy_worker"]
        assert set(strategy_component["job_types"]) == {
            STRATEGY_MATERIAL_EVALUATE,
            STRATEGY_CONTROL_EVALUATE,
        }

        # 未激活时三条网络 Scheduler 都不产生千川 Job；策略 Worker 也没有来源可消费。
        assert app.plan_state_scheduler.run_once() == 0
        assert app.hot_collection_scheduler.run_once()["enqueued"] == 0
        assert app.hot_confirmation_scheduler.run_once() == 0
        assert app.plan_state_scheduler.health_snapshot()["license_blocked"] is True
        assert app.hot_collection_scheduler.health_snapshot()["license_blocked"] is True
        assert app.hot_confirmation_scheduler.health_snapshot()["license_blocked"] is True
        with app.database.connect(readonly=True) as conn:  # type: ignore[union-attr]
            assert conn.execute("SELECT COUNT(*) FROM background_job").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM collection_batch").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM strategy_hit").fetchone()[0] == 0
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
