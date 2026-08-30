"""数据库和磁盘安全健康检查。"""
from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .database import Database
from .schema import REQUIRED_TABLES, SCHEMA_VERSION, current_schema_version, table_names

GiB = 1024 ** 3
DiskUsageFn = Callable[[Path], tuple[int, int, int]]
FileSizeFn = Callable[[Path], int]

REQUIRED_INDEXES = frozenset(
    {
        "ux_qianchuan_account_auth_primary",
        "idx_monitor_plan_account_state",
        "idx_collection_target_pipeline_time",
        "idx_collection_status_time",
        "idx_material_5m_object_time",
        "idx_material_daily_plan_date",
        "idx_control_5m_task_time",
        "idx_execution_status_created",
        "idx_execution_object",
        "idx_reconciliation_due",
        "idx_job_claim",
    }
)


@dataclass(frozen=True)
class DatabaseHealth:
    status: str
    business_write_allowed: bool
    reasons: tuple[str, ...]
    schema_version: int | None
    quick_check: str | None
    database_bytes: int
    wal_bytes: int
    disk_total_bytes: int
    disk_free_bytes: int
    disk_free_ratio: float


class DatabaseHealthService:
    def __init__(self, database: Database, *, disk_usage: DiskUsageFn | None = None, file_size: FileSizeFn | None = None, supported_schema_version: int = SCHEMA_VERSION) -> None:
        self._database = database
        self._disk_usage = disk_usage or (lambda path: shutil.disk_usage(path))
        self._file_size = file_size or (lambda path: path.stat().st_size if path.exists() else 0)
        self._supported = supported_schema_version

    def check(self) -> DatabaseHealth:
        path = self._database.config.path
        db_bytes = self._file_size(path)
        wal_path = Path(str(path) + "-wal")
        wal_bytes = self._file_size(wal_path)
        total, _used, free = self._disk_usage(path.parent)
        ratio = (free / total) if total else 0.0
        reasons: list[str] = []
        version: int | None = None
        quick: str | None = None

        try:
            with self._database.connect(readonly=True) as conn:
                quick = str(conn.execute("PRAGMA quick_check").fetchone()[0])
                if quick.lower() != "ok":
                    reasons.append("DB_INTEGRITY_FAILED")
                try:
                    version = current_schema_version(conn)
                except sqlite3.Error:
                    version = None
                if version is None:
                    reasons.append("DB_SCHEMA_MISSING")
                elif version > self._supported:
                    reasons.append("DB_SCHEMA_NEWER_THAN_APP")
                elif version < self._supported:
                    reasons.append("DB_MIGRATION_REQUIRED")

                missing_tables = REQUIRED_TABLES - table_names(conn)
                if missing_tables:
                    reasons.append("DB_REQUIRED_TABLE_MISSING")

                index_names = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
                    ).fetchall()
                }
                if REQUIRED_INDEXES - index_names:
                    reasons.append("DB_REQUIRED_INDEX_MISSING")

                try:
                    migration_row = conn.execute(
                        "SELECT 1 FROM schema_migration_log WHERE status='RUNNING' LIMIT 1"
                    ).fetchone()
                    if migration_row is not None:
                        reasons.append("DB_MIGRATION_INCOMPLETE")
                except sqlite3.Error:
                    # schema 表缺失会由上面的必需表检查阻断。
                    pass
        except (sqlite3.Error, OSError):
            reasons.append("DB_OPEN_FAILED")

        # 在正式业务启动前证明数据库仍能获得写事务。只 BEGIN/ROLLBACK，不写业务数据。
        if not any(reason.startswith("DB_SCHEMA") or reason in {"DB_OPEN_FAILED", "DB_REQUIRED_TABLE_MISSING"} for reason in reasons):
            try:
                with self._database.connect() as write_conn:
                    write_conn.execute("BEGIN IMMEDIATE")
                    write_conn.rollback()
            except (sqlite3.Error, OSError):
                reasons.append("DB_WRITE_PROBE_FAILED")

        blocking = {
            "DB_INTEGRITY_FAILED",
            "DB_SCHEMA_MISSING",
            "DB_SCHEMA_NEWER_THAN_APP",
            "DB_MIGRATION_REQUIRED",
            "DB_REQUIRED_TABLE_MISSING",
            "DB_REQUIRED_INDEX_MISSING",
            "DB_MIGRATION_INCOMPLETE",
            "DB_OPEN_FAILED",
            "DB_WRITE_PROBE_FAILED",
        }
        if any(reason in blocking for reason in reasons):
            return DatabaseHealth("BLOCKED", False, tuple(dict.fromkeys(reasons)), version, quick, db_bytes, wal_bytes, total, free, ratio)

        # 磁盘硬保护优先。DB>=8GB 单独不构成停止条件。
        if free < int(1.5 * GiB) or ratio < 0.01:
            reasons.append("DISK_EMERGENCY")
            status, write_allowed = "EMERGENCY", False
        elif free < 3 * GiB or ratio < 0.02:
            reasons.append("DISK_HIGH_RISK")
            status, write_allowed = "HIGH_RISK", False
        elif free < 6 * GiB or ratio < 0.03 or db_bytes >= 4 * GiB:
            reasons.append("DISK_MAINTENANCE")
            if db_bytes >= 8 * GiB:
                reasons.append("DB_SIZE_STRONG_WARNING")
            status, write_allowed = "MAINTENANCE", True
        elif free < 10 * GiB or ratio < 0.05 or db_bytes >= 2 * GiB:
            reasons.append("DISK_WARNING")
            status, write_allowed = "WARNING", True
        else:
            status, write_allowed = "OK", True

        return DatabaseHealth(status, write_allowed, tuple(reasons), version, quick, db_bytes, wal_bytes, total, free, ratio)
