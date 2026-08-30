"""SQLite Backup API 一致性备份。"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .database import Database


@dataclass(frozen=True)
class BackupResult:
    path: Path
    size_bytes: int
    sha256: str
    quick_check: str


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def backup_database(database: Database, backup_dir: Path, *, label: str = "backup") -> BackupResult:
    """通过 SQLite Backup API 创建单文件一致性快照，不直接复制 WAL 数据库。"""
    backup_dir.mkdir(parents=True, exist_ok=True)
    safe_label = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in label)[:64] or "backup"
    destination = backup_dir / f"runtime-{safe_label}-{_timestamp()}.db"
    counter = 1
    while destination.exists():
        destination = backup_dir / f"runtime-{safe_label}-{_timestamp()}-{counter}.db"
        counter += 1

    source = database.connect()
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
        target.commit()
        quick_check = str(target.execute("PRAGMA quick_check").fetchone()[0])
        if quick_check.lower() != "ok":
            raise sqlite3.DatabaseError(f"backup quick_check failed: {quick_check}")
    except BaseException:
        target.close()
        source.close()
        destination.unlink(missing_ok=True)
        raise
    else:
        target.close()
        source.close()

    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    return BackupResult(destination, destination.stat().st_size, digest, quick_check)
