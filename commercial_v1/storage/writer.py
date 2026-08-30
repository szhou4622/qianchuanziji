"""单线程 SQLite Writer。

所有商业业务写入最终都必须排队到同一个 Writer。每个提交任务自动使用独立短事务；
网络请求不得在 work callable 内执行。
"""
from __future__ import annotations

import queue
import sqlite3
import threading
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Callable, Generic, Iterable, TypeVar

from .database import Database

T = TypeVar("T")
Work = Callable[[sqlite3.Connection], T]


class WriterClosedError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExecuteResult:
    rowcount: int
    lastrowid: int | None


@dataclass
class _WorkItem(Generic[T]):
    work: Work[T]
    future: Future[T]


_STOP = object()


class StorageWriter:
    """拥有唯一写连接和唯一写线程。"""

    def __init__(self, database: Database, *, queue_maxsize: int = 0, name: str = "storage-writer") -> None:
        self._database = database
        self._queue: queue.Queue[_WorkItem[Any] | object] = queue.Queue(maxsize=queue_maxsize)
        self._name = name
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._accepting = False
        self._fatal_error: BaseException | None = None
        self._processed = 0
        self._failed = 0

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if self._thread is not None:
                raise RuntimeError("StorageWriter cannot be restarted after it has stopped")
            self._accepting = True
            self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
            self._thread.start()

    def submit(self, work: Work[T]) -> Future[T]:
        with self._lock:
            if not self._accepting or self._thread is None:
                raise WriterClosedError("StorageWriter is not accepting work")
            if self._fatal_error is not None:
                raise RuntimeError("StorageWriter has failed") from self._fatal_error
        future: Future[T] = Future()
        self._queue.put(_WorkItem(work=work, future=future))
        return future

    def execute(self, sql: str, params: Iterable[Any] = ()) -> Future[ExecuteResult]:
        frozen_params = tuple(params)

        def work(conn: sqlite3.Connection) -> ExecuteResult:
            cursor = conn.execute(sql, frozen_params)
            return ExecuteResult(rowcount=cursor.rowcount, lastrowid=cursor.lastrowid)

        return self.submit(work)

    def executemany(self, sql: str, params_list: Iterable[Iterable[Any]]) -> Future[ExecuteResult]:
        frozen = [tuple(params) for params in params_list]

        def work(conn: sqlite3.Connection) -> ExecuteResult:
            cursor = conn.executemany(sql, frozen)
            return ExecuteResult(rowcount=cursor.rowcount, lastrowid=cursor.lastrowid)

        return self.submit(work)

    def transaction(self, work: Work[T]) -> Future[T]:
        return self.submit(work)

    def close(self, *, drain: bool = True, timeout: float = 10.0) -> None:
        with self._lock:
            thread = self._thread
            if thread is None:
                self._accepting = False
                return
            self._accepting = False
        if not drain:
            self._cancel_pending()
        self._queue.put(_STOP)
        thread.join(timeout)
        if thread.is_alive():
            raise TimeoutError("StorageWriter did not stop within timeout")

    def health_snapshot(self) -> dict[str, Any]:
        thread = self._thread
        return {
            "accepting": self._accepting,
            "alive": bool(thread and thread.is_alive()),
            "queue_depth": self._queue.qsize(),
            "processed": self._processed,
            "failed": self._failed,
            "fatal_error": None if self._fatal_error is None else type(self._fatal_error).__name__,
        }

    def _cancel_pending(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            try:
                if isinstance(item, _WorkItem) and not item.future.done():
                    item.future.set_exception(WriterClosedError("StorageWriter closed before execution"))
            finally:
                self._queue.task_done()

    def _run(self) -> None:
        try:
            conn = self._database.connect()
        except BaseException as exc:
            self._fatal_error = exc
            self._fail_all_pending(exc)
            return
        try:
            while True:
                item = self._queue.get()
                try:
                    if item is _STOP:
                        return
                    assert isinstance(item, _WorkItem)
                    self._execute_item(conn, item)
                finally:
                    self._queue.task_done()
        except BaseException as exc:
            self._fatal_error = exc
            self._fail_all_pending(exc)
        finally:
            conn.close()
            with self._lock:
                self._accepting = False

    def _execute_item(self, conn: sqlite3.Connection, item: _WorkItem[Any]) -> None:
        if not item.future.set_running_or_notify_cancel():
            return
        try:
            conn.execute("BEGIN")
            result = item.work(conn)
            conn.commit()
        except BaseException as exc:
            conn.rollback()
            self._failed += 1
            item.future.set_exception(exc)
        else:
            self._processed += 1
            item.future.set_result(result)

    def _fail_all_pending(self, exc: BaseException) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                if isinstance(item, _WorkItem) and not item.future.done():
                    item.future.set_exception(exc)
            finally:
                self._queue.task_done()
