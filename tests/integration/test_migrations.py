import sqlite3
from pathlib import Path

import pytest

from commercial_v1.storage.database import Database, DatabaseConfig
from commercial_v1.storage.migrations import Migration, MigrationRunner
from commercial_v1.storage.schema import create_schema_v1, current_schema_version, table_names


def _db(tmp_path: Path) -> Database:
    db = Database(DatabaseConfig(tmp_path / "runtime.db"))
    with db.connect() as conn:
        create_schema_v1(conn)
    return db


def test_migration_v1_to_v2_is_backed_up_and_logged(tmp_path: Path) -> None:
    db = _db(tmp_path)
    migration = Migration("002_probe", 1, 2, lambda conn: conn.execute("CREATE TABLE migration_probe(id INTEGER PRIMARY KEY)"))
    runner = MigrationRunner(db, tmp_path / "backups", supported_version=2)
    assert runner.run(target_version=2, migrations=[migration], app_version="0.2.0") == ["002_probe"]
    with db.connect(readonly=True) as conn:
        assert current_schema_version(conn) == 2
        assert "migration_probe" in table_names(conn)
        row = conn.execute("SELECT status,backup_path_hash FROM schema_migration_log WHERE migration_id='002_probe'").fetchone()
        assert row["status"] == "SUCCESS"
        assert len(row["backup_path_hash"]) == 64
    assert list((tmp_path / "backups").glob("*.db"))


def test_failed_migration_rolls_back_and_logs_failure(tmp_path: Path) -> None:
    db = _db(tmp_path)
    def broken(conn):
        conn.execute("CREATE TABLE should_rollback(id INTEGER)")
        conn.execute("THIS IS INVALID SQL")
    runner = MigrationRunner(db, tmp_path / "backups", supported_version=2)
    with pytest.raises(sqlite3.OperationalError):
        runner.run(target_version=2, migrations=[Migration("002_broken", 1, 2, broken)], app_version="0.2.0")
    with db.connect(readonly=True) as conn:
        assert current_schema_version(conn) == 1
        assert "should_rollback" not in table_names(conn)
        assert conn.execute("SELECT status FROM schema_migration_log WHERE migration_id='002_broken'").fetchone()[0] == "FAILED"
