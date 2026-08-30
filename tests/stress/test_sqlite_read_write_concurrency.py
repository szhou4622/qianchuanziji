from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from commercial_v1.storage.database import Database, DatabaseConfig
from commercial_v1.storage.writer import StorageWriter


def test_fifty_readers_can_run_while_single_writer_commits(tmp_path: Path) -> None:
    db = Database(DatabaseConfig(tmp_path / "runtime.db"))
    with db.connect() as conn:
        conn.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY, value INTEGER NOT NULL)")
        conn.commit()

    writer = StorageWriter(db)
    writer.start()
    try:
        def reader(_worker: int) -> int:
            observed = 0
            for _ in range(20):
                with db.connect(readonly=True) as conn:
                    observed = max(observed, int(conn.execute("SELECT COUNT(*) FROM sample").fetchone()[0]))
            return observed

        with ThreadPoolExecutor(max_workers=51) as pool:
            reader_futures = [pool.submit(reader, i) for i in range(50)]
            write_futures = [
                writer.execute("INSERT INTO sample(id,value) VALUES(?,?)", (i, i * 2))
                for i in range(1, 501)
            ]
            for future in write_futures:
                future.result(timeout=15)
            for future in reader_futures:
                future.result(timeout=15)

        with db.connect(readonly=True) as conn:
            assert conn.execute("SELECT COUNT(*) FROM sample").fetchone()[0] == 500
        health = writer.health_snapshot()
        assert health["failed"] == 0
        assert health["queue_depth"] == 0
    finally:
        writer.close()
