from pathlib import Path

from commercial_v1.diagnostics.service import DiagnosticsService
from commercial_v1.license.runtime_state import LicenseRuntimeStateStore
from commercial_v1.runtime.jobs import PersistentJobStore
from commercial_v1.storage.database import Database, DatabaseConfig
from commercial_v1.storage.health import DatabaseHealthService
from commercial_v1.storage.schema import create_schema_v1
from commercial_v1.storage.writer import StorageWriter


def test_diagnostics_sanitizes_secrets_embedded_in_error_text(tmp_path: Path) -> None:
    db = Database(DatabaseConfig(tmp_path / "runtime.db"))
    with db.connect() as conn:
        create_schema_v1(conn)
    writer = StorageWriter(db)
    writer.start()
    try:
        now = "2026-08-30T00:00:00Z"
        writer.execute(
            """INSERT INTO api_error_event(
               error_id,module,error_scope,error_type,message,retryable,occurred_at
               ) VALUES(?,?,?,?,?,?,?)""",
            ("err", "test", "APP", "TEST", "request failed access_token=super-secret-value", 0, now),
        ).result(timeout=5)
        service = DiagnosticsService(
            db,
            DatabaseHealthService(db),
            writer,
            PersistentJobStore(db, writer),
            LicenseRuntimeStateStore(db, writer),
        )
        snapshot = service.snapshot()
        message = snapshot["recent_errors"][0]["message"]
        assert "super-secret-value" not in message
        assert "<redacted>" in message
    finally:
        writer.close()
