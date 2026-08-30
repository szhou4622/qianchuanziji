from pathlib import Path

from commercial_v1.storage.database import Database, DatabaseConfig
from commercial_v1.storage.health import DatabaseHealthService, GiB
from commercial_v1.storage.schema import create_schema_v1


def _db(tmp_path: Path) -> Database:
    db = Database(DatabaseConfig(tmp_path / "runtime.db"))
    with db.connect() as conn:
        create_schema_v1(conn)
    return db


def test_health_high_risk_blocks_new_business_writes(tmp_path: Path) -> None:
    db = _db(tmp_path)
    health = DatabaseHealthService(db, disk_usage=lambda _p: (100*GiB, 98*GiB, 2*GiB)).check()
    assert health.status == "HIGH_RISK"
    assert health.business_write_allowed is False


def test_health_does_not_block_only_because_database_is_large(tmp_path: Path) -> None:
    db = _db(tmp_path)

    def fake_size(path: Path) -> int:
        return 9 * GiB if path == db.config.path else 0

    health = DatabaseHealthService(
        db,
        disk_usage=lambda _p: (500*GiB, 100*GiB, 400*GiB),
        file_size=fake_size,
    ).check()
    assert health.status == "MAINTENANCE"
    assert health.business_write_allowed is True
    assert "DB_SIZE_STRONG_WARNING" in health.reasons


def test_health_blocks_half_finished_migration(tmp_path: Path) -> None:
    db = _db(tmp_path)
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO schema_migration_log(
               migration_id,from_version,to_version,app_version,started_at,status
               ) VALUES(?,?,?,?,?,'RUNNING')""",
            ("m1", 1, 2, "0.2.0", "2026-08-30T00:00:00Z"),
        )
        conn.commit()
    health = DatabaseHealthService(db).check()
    assert health.status == "BLOCKED"
    assert health.business_write_allowed is False
    assert "DB_MIGRATION_INCOMPLETE" in health.reasons


def test_health_blocks_missing_required_index(tmp_path: Path) -> None:
    db = _db(tmp_path)
    with db.connect() as conn:
        conn.execute("DROP INDEX idx_job_claim")
        conn.commit()
    health = DatabaseHealthService(db).check()
    assert health.status == "BLOCKED"
    assert "DB_REQUIRED_INDEX_MISSING" in health.reasons
