"""SQLite 连接、目录和基础健康配置。

Phase 1 只建立新的商业版数据库；禁止搜索或迁移旧版数据库。
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

APP_DIR_NAME = "QCSCKP"
COMMERCIAL_DATA_DIR_NAME = "commercial-v1"
DEFAULT_DB_NAME = "runtime.db"


@dataclass(frozen=True)
class DatabaseConfig:
    path: Path
    busy_timeout_ms: int = 5000

    @classmethod
    def default(cls) -> "DatabaseConfig":
        return cls(path=default_data_dir() / DEFAULT_DB_NAME)


def default_data_dir() -> Path:
    """返回商业版独立数据目录，不探测任何旧版路径。"""
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        # 非 Windows 仅供测试/开发；正式产品边界仍是 Windows。
        root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return root / APP_DIR_NAME / COMMERCIAL_DATA_DIR_NAME


def ensure_data_layout(root: Path | None = None) -> dict[str, Path]:
    base = root or default_data_dir()
    paths = {
        "root": base,
        "logs": base / "logs",
        "backups": base / "backups",
        "diagnostics": base / "diagnostics",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


class Database:
    """SQLite 连接工厂。

    连接建立时强制开启 WAL、外键和 busy_timeout。业务写入后续统一走
    StorageWriter；这里保留 transaction() 仅供初始化、迁移和测试底座使用。
    """

    def __init__(self, config: DatabaseConfig | None = None) -> None:
        self.config = config or DatabaseConfig.default()
        self.config.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        if readonly:
            uri = f"file:{self.config.path.as_posix()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=self.config.busy_timeout_ms / 1000)
        else:
            conn = sqlite3.connect(
                self.config.path,
                timeout=self.config.busy_timeout_ms / 1000,
            )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {int(self.config.busy_timeout_ms)}")
        if not readonly:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def pragma_snapshot(self) -> dict[str, int | str]:
        with self.connect() as conn:
            return {
                "journal_mode": str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
                "foreign_keys": int(conn.execute("PRAGMA foreign_keys").fetchone()[0]),
                "busy_timeout": int(conn.execute("PRAGMA busy_timeout").fetchone()[0]),
                "synchronous": int(conn.execute("PRAGMA synchronous").fetchone()[0]),
            }
