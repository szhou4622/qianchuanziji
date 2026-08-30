from pathlib import Path

import pytest

from commercial_v1.storage.database import Database, DatabaseConfig


def test_database_transaction_rolls_back_all_rows(tmp_path: Path) -> None:
    db = Database(DatabaseConfig(tmp_path / "runtime.db"))
    with db.connect() as conn:
        conn.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY,value TEXT NOT NULL)")
        conn.commit()

    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            for i in range(473):
                if i == 219:
                    raise RuntimeError("forced failure at row 220")
                conn.execute("INSERT INTO evidence(value) VALUES(?)", (str(i),))

    with db.connect(readonly=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0
