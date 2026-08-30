"""千川商业版 V1 本地 Runtime 入口。

Phase 1 只串联运行底座，不连接真实千川业务接口。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from commercial_v1.diagnostics.service import DiagnosticsService
from commercial_v1.license.runtime_state import LicenseRuntimeStateStore
from commercial_v1.runtime.jobs import PersistentJobStore
from commercial_v1.runtime.recovery import DEFAULT_RECOVERY_POLICY, StartupRecoveryReport, StartupRecoveryService
from commercial_v1.runtime.single_instance import GlobalUserMutex
from commercial_v1.runtime.supervisor import ComponentSpec, RuntimeSupervisor, RuntimeWatchdog
from commercial_v1.runtime.workers import JobWorker, LeaseRecoveryWorker
from commercial_v1.storage.database import Database, DatabaseConfig, ensure_data_layout
from commercial_v1.storage.health import DatabaseHealthService
from commercial_v1.storage.schema import create_schema_v1
from commercial_v1.storage.writer import StorageWriter

APP_VERSION = "0.1.0"


class AlreadyRunningError(RuntimeError):
    pass


class RuntimeBlockedError(RuntimeError):
    pass


class CommercialApplication:
    """Phase 1 应用容器。

    启动顺序严格为：单实例 → 数据目录/Schema → DB Health → Writer → Recovery →
    Supervisor/Workers → Diagnostics。任何一步失败都必须释放已获得的资源。
    """

    def __init__(
        self,
        *,
        data_dir: Path | None = None,
        app_version: str = APP_VERSION,
        mutex: GlobalUserMutex | None = None,
    ) -> None:
        self._data_dir = data_dir
        self._app_version = app_version
        self._mutex = mutex or GlobalUserMutex()
        self._started = False

        self.database: Database | None = None
        self.writer: StorageWriter | None = None
        self.jobs: PersistentJobStore | None = None
        self.license_state: LicenseRuntimeStateStore | None = None
        self.health: DatabaseHealthService | None = None
        self.recovery_report: StartupRecoveryReport | None = None
        self.supervisor: RuntimeSupervisor | None = None
        self.watchdog: RuntimeWatchdog | None = None
        self.job_worker: JobWorker | None = None
        self.lease_recovery_worker: LeaseRecoveryWorker | None = None
        self.diagnostics: DiagnosticsService | None = None

    @property
    def started(self) -> bool:
        return self._started

    def start(self) -> "CommercialApplication":
        if self._started:
            return self
        if not self._mutex.acquire():
            raise AlreadyRunningError("commercial runtime is already running for this Windows user")

        writer_started = False
        try:
            layout = ensure_data_layout(self._data_dir)
            db_path = layout["root"] / "runtime.db"
            database = Database(DatabaseConfig(db_path))
            is_new_database = not db_path.exists() or db_path.stat().st_size == 0
            if is_new_database:
                with database.connect() as conn:
                    create_schema_v1(conn, app_version=self._app_version)

            health_service = DatabaseHealthService(database)
            health = health_service.check()
            if health.status == "BLOCKED":
                raise RuntimeBlockedError(
                    "database health blocked runtime: " + ",".join(health.reasons)
                )

            writer = StorageWriter(database)
            writer.start()
            writer_started = True
            jobs = PersistentJobStore(database, writer)
            license_state = LicenseRuntimeStateStore(database, writer)
            recovery = StartupRecoveryService(database, writer, jobs)
            recovery_report = recovery.run()

            job_worker = JobWorker(jobs, handlers={})
            lease_recovery = LeaseRecoveryWorker(jobs, DEFAULT_RECOVERY_POLICY)
            supervisor = RuntimeSupervisor()

            supervisor.register(
                ComponentSpec(
                    name="storage_writer",
                    start=writer.start,
                    stop=writer.close,
                    health=writer.health_snapshot,
                    restart=None,
                    critical=True,
                )
            )
            supervisor.register(
                ComponentSpec(
                    name="job_worker",
                    start=job_worker.start,
                    stop=job_worker.stop,
                    health=job_worker.health_snapshot,
                    restart=job_worker.restart,
                    critical=True,
                )
            )
            supervisor.register(
                ComponentSpec(
                    name="lease_recovery",
                    start=lease_recovery.start,
                    stop=lease_recovery.stop,
                    health=lease_recovery.health_snapshot,
                    restart=lease_recovery.restart,
                    critical=True,
                )
            )

            watchdog = RuntimeWatchdog(supervisor)

            def restart_watchdog() -> None:
                watchdog.stop()
                watchdog.start()

            supervisor.register(
                ComponentSpec(
                    name="watchdog",
                    start=watchdog.start,
                    stop=watchdog.stop,
                    health=watchdog.health_snapshot,
                    restart=restart_watchdog,
                    critical=True,
                )
            )
            supervisor.start_all()

            diagnostics = DiagnosticsService(
                database,
                health_service,
                writer,
                jobs,
                license_state,
                supervisor=supervisor,
                app_version=self._app_version,
            )

            self.database = database
            self.writer = writer
            self.jobs = jobs
            self.license_state = license_state
            self.health = health_service
            self.recovery_report = recovery_report
            self.supervisor = supervisor
            self.watchdog = watchdog
            self.job_worker = job_worker
            self.lease_recovery_worker = lease_recovery
            self.diagnostics = diagnostics
            self._started = True
            return self
        except BaseException:
            if self.supervisor is not None:
                try:
                    self.supervisor.stop_all()
                except BaseException:
                    pass
            elif writer_started and "writer" in locals():
                try:
                    writer.close()
                except BaseException:
                    pass
            self._mutex.close()
            raise

    def stop(self) -> None:
        failure: BaseException | None = None
        try:
            if self.supervisor is not None:
                try:
                    self.supervisor.stop_all()
                except BaseException as exc:
                    failure = exc
        finally:
            self._started = False
            self._mutex.close()
        if failure is not None:
            raise failure

    def diagnostics_snapshot(self) -> dict[str, Any]:
        if not self._started or self.diagnostics is None:
            raise RuntimeError("application is not running")
        result = self.diagnostics.snapshot()
        if self.recovery_report is not None:
            result["startup_recovery"] = {
                "aborted_collection_batches": self.recovery_report.aborted_collection_batches,
                "job_recovery": dict(self.recovery_report.job_recovery),
                "unresolved_execution_count": self.recovery_report.unresolved_execution_count,
            }
        return result

    def __enter__(self) -> "CommercialApplication":
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()
