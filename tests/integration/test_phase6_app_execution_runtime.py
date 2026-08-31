from __future__ import annotations

from pathlib import Path

from commercial_v1.app import CommercialApplication


class FakeMutex:
    def acquire(self) -> bool:
        return True

    def close(self) -> None:
        pass


def test_execution_runtime_starts_without_network_or_business_post(tmp_path: Path) -> None:
    app = CommercialApplication(data_dir=tmp_path, mutex=FakeMutex())  # type: ignore[arg-type]
    app.start()
    try:
        assert app.execution_service is not None
        assert app.execution_preflight is not None
        assert app.execution_worker is not None
        assert app.execution_scheduler is not None
        assert app.license_state is not None
        assert app.database is not None

        # Fresh runtime has no valid license: execution scheduler must stay locally idle.
        assert app.license_state.get().normal_business_allowed is False
        assert app.execution_scheduler.run_once() == {
            "prepare_enqueued": 0,
            "prepare_requeued": 0,
            "preflight_enqueued": 0,
            "preflight_requeued": 0,
        }

        snapshot = app.diagnostics_snapshot()
        assert snapshot["runtime"]["components"]["execution_worker"]["alive"] is True
        assert snapshot["runtime"]["components"]["execution_scheduler"]["alive"] is True
        assert snapshot["execution"]["execution_attempt_rows"] == 0
        assert snapshot["execution"]["business_post_enabled"] is False
        with app.database.connect(readonly=True) as conn:
            assert conn.execute("SELECT COUNT(*) FROM execution_attempt").fetchone()[0] == 0
    finally:
        app.stop()
