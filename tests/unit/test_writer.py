from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from commercial_v1.storage.database import Database, DatabaseConfig
from commercial_v1.storage.writer import StorageWriter, WriterClosedError


def _database(tmp_path: Path) -> Database:
    db = Database(DatabaseConfig(tmp_path / "runtime.db"))
    with db.connect() as conn:
        conn.execute("CREATE TABLE counter(id INTEGER PRIMARY KEY AUTOINCREMENT,worker INTEGER NOT NULL,n INTEGER NOT NULL)")
        conn.commit()
    return db


def test_writer_serializes_50_threads_5000_writes(tmp_path: Path) -> None:
    db = _database(tmp_path)
    writer = StorageWriter(db)
    writer.start()

    def produce(worker: int) -> None:
        futures = [writer.execute("INSERT INTO counter(worker,n) VALUES(?,?)", (worker, n)) for n in range(100)]
        # 这里验证的是“50 个生产线程最终仍由单 Writer 无丢失串行落库”，不是把
        # GitHub Windows runner 的磁盘抖动当成 10 秒性能 SLA。真实业务批量写另有事务聚合。
        for future in futures:
            future.result(timeout=30)

    with ThreadPoolExecutor(max_workers=50) as pool:
        list(pool.map(produce, range(50)))

    snapshot = writer.health_snapshot()
    writer.close()
    with db.connect(readonly=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM counter").fetchone()[0] == 5000
    assert snapshot["failed"] == 0
    assert snapshot["processed"] == 5000


def test_writer_transaction_rolls_back_entire_work_item(tmp_path: Path) -> None:
    db = _database(tmp_path)
    writer = StorageWriter(db)
    writer.start()

    def work(conn):
        for i in range(473):
            if i == 219:
                raise RuntimeError("forced failure at row 220")
            conn.execute("INSERT INTO counter(worker,n) VALUES(?,?)", (1, i))

    with pytest.raises(RuntimeError, match="row 220"):
        writer.transaction(work).result(timeout=5)

    writer.close()
    with db.connect(readonly=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM counter").fetchone()[0] == 0


def test_writer_rejects_new_work_after_close(tmp_path: Path) -> None:
    writer = StorageWriter(_database(tmp_path))
    writer.start()
    writer.close()
    with pytest.raises(WriterClosedError):
        writer.execute("SELECT 1")
