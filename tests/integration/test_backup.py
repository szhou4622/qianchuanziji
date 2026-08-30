from pathlib import Path

from commercial_v1.storage.backup import backup_database
from commercial_v1.storage.database import Database, DatabaseConfig
from commercial_v1.storage.schema import create_schema_v1


def test_sqlite_backup_contains_committed_wal_data(tmp_path: Path) -> None:
    db = Database(DatabaseConfig(tmp_path / "runtime.db"))
    with db.connect() as conn:
        create_schema_v1(conn)
        conn.execute("CREATE TABLE backup_probe(value TEXT NOT NULL)")
        conn.execute("INSERT INTO backup_probe(value) VALUES('present')")
        conn.commit()
    result = backup_database(db, tmp_path / "backups", label="test")
    assert result.quick_check == "ok"
    backup_db = Database(DatabaseConfig(result.path))
    with backup_db.connect(readonly=True) as conn:
        assert conn.execute("SELECT value FROM backup_probe").fetchone()[0] == "present"
