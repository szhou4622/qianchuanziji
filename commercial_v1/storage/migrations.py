"""可审计 Schema Migration Framework。"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from .backup import BackupResult, backup_database
from .database import Database
from .schema import current_schema_version

MigrationFn = Callable[[sqlite3.Connection], None]
BackupFn = Callable[[Database, Path], BackupResult]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class NewerSchemaError(RuntimeError):
    pass


class MigrationPlanError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    migration_id: str
    from_version: int
    to_version: int
    apply: MigrationFn


class MigrationRunner:
    def __init__(self, database: Database, backup_dir: Path, *, supported_version: int, backup_fn: BackupFn | None = None) -> None:
        self._database = database
        self._backup_dir = backup_dir
        self._supported = supported_version
        self._backup_fn = backup_fn or (lambda db, directory: backup_database(db, directory, label="migration"))

    def run(self, *, target_version: int, migrations: Iterable[Migration], app_version: str) -> list[str]:
        by_from = {migration.from_version: migration for migration in migrations}
        with self._database.connect() as conn:
            current = current_schema_version(conn)
        if current is None:
            raise MigrationPlanError("schema_version is missing")
        if current > self._supported:
            raise NewerSchemaError(f"database schema {current} is newer than app support {self._supported}")
        if target_version > self._supported:
            raise MigrationPlanError("target version exceeds app supported version")
        if current >= target_version:
            return []

        chain: list[Migration] = []
        cursor = current
        while cursor < target_version:
            migration = by_from.get(cursor)
            if migration is None or migration.to_version != cursor + 1:
                raise MigrationPlanError(f"missing sequential migration from {cursor}")
            chain.append(migration)
            cursor = migration.to_version

        backup = self._backup_fn(self._database, self._backup_dir)
        completed: list[str] = []
        for migration in chain:
            self._run_one(migration, app_version=app_version, backup=backup)
            completed.append(migration.migration_id)
        return completed

    def _run_one(self, migration: Migration, *, app_version: str, backup: BackupResult) -> None:
        started = _now()
        with self._database.connect() as conn:
            existing = conn.execute("SELECT status FROM schema_migration_log WHERE migration_id=?", (migration.migration_id,)).fetchone()
            if existing is not None and existing["status"] == "SUCCESS":
                raise MigrationPlanError(f"migration already succeeded: {migration.migration_id}")
            if existing is None:
                conn.execute(
                    "INSERT INTO schema_migration_log(migration_id,from_version,to_version,app_version,started_at,status,backup_path_hash) VALUES(?,?,?,?,?,'RUNNING',?)",
                    (migration.migration_id, migration.from_version, migration.to_version, app_version, started, backup.sha256),
                )
            else:
                conn.execute(
                    "UPDATE schema_migration_log SET from_version=?,to_version=?,app_version=?,started_at=?,finished_at=NULL,status='RUNNING',backup_path_hash=?,error_message=NULL,validation_json=NULL WHERE migration_id=?",
                    (migration.from_version, migration.to_version, app_version, started, backup.sha256, migration.migration_id),
                )
            conn.commit()

        conn = self._database.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            version = current_schema_version(conn)
            if version != migration.from_version:
                raise MigrationPlanError(f"migration {migration.migration_id} expected {migration.from_version}, got {version}")
            migration.apply(conn)
            now = _now()
            conn.execute("UPDATE schema_meta SET value=?,updated_at=? WHERE key='schema_version'", (str(migration.to_version), now))
            conn.execute("INSERT OR REPLACE INTO schema_meta(key,value,updated_at) VALUES('last_migrated_at',?,?)", (now, now))
            conn.commit()
        except BaseException as exc:
            conn.rollback()
            conn.close()
            with self._database.connect() as log_conn:
                log_conn.execute("UPDATE schema_migration_log SET status='FAILED',finished_at=?,error_message=? WHERE migration_id=?", (_now(), type(exc).__name__, migration.migration_id))
                log_conn.commit()
            raise
        else:
            conn.close()
            with self._database.connect() as log_conn:
                log_conn.execute("UPDATE schema_migration_log SET status='SUCCESS',finished_at=? WHERE migration_id=?", (_now(), migration.migration_id))
                log_conn.commit()
