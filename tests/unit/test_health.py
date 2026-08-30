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
