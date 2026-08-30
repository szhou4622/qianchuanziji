"""结构化诊断快照。

Phase 1 只提供后端 dict；阶段 7 再接完整 UI。所有输出最后经过统一 redact。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from commercial_v1.license.runtime_state import LicenseRuntimeStateStore
from commercial_v1.runtime.jobs import PersistentJobStore
from commercial_v1.runtime.supervisor import RuntimeSupervisor
from commercial_v1.security.redaction import redact
from commercial_v1.storage.database import Database
from commercial_v1.storage.health import DatabaseHealthService
from commercial_v1.storage.writer import StorageWriter


class DiagnosticsService:
    def __init__(
        self,
        database: Database,
        health: DatabaseHealthService,
        writer: StorageWriter,
        jobs: PersistentJobStore,
        license_state: LicenseRuntimeStateStore,
        *,
        supervisor: RuntimeSupervisor | None = None,
        app_version: str = "0.1.0",
    ) -> None:
        self._database = database
        self._health = health
        self._writer = writer
        self._jobs = jobs
        self._license = license_state
        self._supervisor = supervisor
        self._app_version = app_version

    def snapshot(self) -> dict[str, Any]:
        db_health = self._health.check()
        license_state = self._license.get()
        with self._database.connect(readonly=True) as conn:
            active_leases = int(conn.execute("SELECT COUNT(*) FROM task_lease").fetchone()[0])
            migration = conn.execute(
                "SELECT migration_id,from_version,to_version,status,started_at,finished_at,error_message FROM schema_migration_log ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            maintenance = conn.execute(
                "SELECT maintenance_id,maintenance_type,status,started_at,finished_at,error_message FROM maintenance_log ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            recent_errors = [
                dict(row)
                for row in conn.execute(
                    "SELECT error_id,module,error_scope,error_type,message,api_code,request_id,occurred_at,resolved_at FROM api_error_event ORDER BY occurred_at DESC LIMIT 20"
                ).fetchall()
            ]
            unresolved = int(
                conn.execute(
                    "SELECT COUNT(*) FROM execution_task WHERE status IN ('SUBMITTING','SUBMITTED','VERIFYING','UNKNOWN_REQUIRES_REVIEW')"
                ).fetchone()[0]
            )

        result: dict[str, Any] = {
            "app_version": self._app_version,
            "schema_version": db_health.schema_version,
            "database": {
                "status": db_health.status,
                "business_write_allowed": db_health.business_write_allowed,
                "reasons": list(db_health.reasons),
                "quick_check": db_health.quick_check,
                "database_bytes": db_health.database_bytes,
                "wal_bytes": db_health.wal_bytes,
                "disk_total_bytes": db_health.disk_total_bytes,
                "disk_free_bytes": db_health.disk_free_bytes,
                "disk_free_ratio": db_health.disk_free_ratio,
            },
            "storage_writer": self._writer.health_snapshot(),
            "jobs": {
                "queue_counts": self._jobs.queue_counts(),
                "active_leases": active_leases,
            },
            "license_runtime": {
                "status": license_state.status,
                "normal_business_allowed": license_state.normal_business_allowed,
                "last_online_verified_at": license_state.last_online_verified_at,
                "first_network_failure_at": license_state.first_network_failure_at,
                "network_grace_until": license_state.network_grace_until,
                "last_error_code": license_state.last_error_code,
            },
            "unresolved_executions": unresolved,
            "last_migration": dict(migration) if migration else None,
            "last_maintenance": dict(maintenance) if maintenance else None,
            "recent_errors": recent_errors,
        }
        if self._supervisor is not None:
            result["runtime"] = self._supervisor.health_snapshot()
        return redact(result)
