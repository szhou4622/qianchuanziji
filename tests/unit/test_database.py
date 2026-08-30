from pathlib import Path

from commercial_v1.storage.database import Database, DatabaseConfig, ensure_data_layout


def test_database_enforces_sqlite_pragmas(tmp_path: Path) -> None:
    db = Database(DatabaseConfig(tmp_path / "runtime.db"))
    snapshot = db.pragma_snapshot()
    assert snapshot["journal_mode"] == "wal"
    assert snapshot["foreign_keys"] == 1
    assert snapshot["busy_timeout"] == 5000
    assert snapshot["synchronous"] == 1


def test_data_layout_is_commercial_v1_only(tmp_path: Path) -> None:
    layout = ensure_data_layout(tmp_path / "commercial-v1")
    assert layout["root"].name == "commercial-v1"
    assert layout["logs"].is_dir()
    assert layout["backups"].is_dir()
    assert layout["diagnostics"].is_dir()
