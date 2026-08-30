"""千川商业版 V1 本地 Runtime 入口。

Phase 2 已接入 OAuth / advertiser / 计划目录和 10 分钟监控计划活跃状态检查。
应用启动本身不访问千川网络；只有用户显式发现账户/读取计划，或激活有效且存在到期
WATCHING 计划时，才会由受控 Worker 发起官方 GET。投放业务 POST 仍被客户端禁止。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from commercial_v1.diagnostics.service import DiagnosticsService
from commercial_v1.license.runtime_state import LicenseRuntimeStateStore
from commercial_v1.qianchuan import (
    PLAN_STATUS_CHECK,
    AccountDiscoveryService,
    AccountStore,
    MonitorPlanStore,
    OAuthTokenProvider,
    OpenApiClient,
    PlanCatalogService,
    PlanMonitorService,
    PlanStateCheckHandler,
    PlanStateScheduler,
)
from commercial_v1.runtime.jobs import PersistentJobStore
from commercial_v1.runtime.recovery import DEFAULT_RECOVERY_POLICY, StartupRecoveryReport, StartupRecoveryService
from commercial_v1.runtime.single_instance import GlobalUserMutex
from commercial_v1.runtime.supervisor import ComponentSpec, RuntimeSupervisor, RuntimeWatchdog
from commercial_v1.runtime.workers import JobWorker, LeaseRecoveryWorker
from commercial_v1.storage.database import Database, DatabaseConfig, ensure_data_layout
from commercial_v1.storage.health import DatabaseHealthService
from commercial_v1.storage.schema import create_schema_v1
from commercial_v1.storage.writer import StorageWriter

APP_VERSION = "0.2.0"


class AlreadyRunningError(RuntimeError):
    pass


class RuntimeBlockedError(RuntimeError):
    pass


class CommercialApplication:
    """商业版应用容器。

    启动顺序严格为：单实例 → 数据目录/Schema → DB Health → Writer → Recovery →
    千川服务对象（无网络）→ Supervisor/Workers/Scheduler → Diagnostics。

    任何一步失败都必须释放已获得资源。激活状态不允许正常业务时，计划状态 Scheduler
    保持存活但不生成新 Job；已排队的状态 Job 也会在 Handler 中被 LICENSE_BLOCKED，
    因而不会触发网络调用。
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
        self.plan_state_scheduler: PlanStateScheduler | None = None
        self.diagnostics: DiagnosticsService | None = None

        # Phase 2 千川服务。构造这些对象不得访问网络。
        self.open_api_client: OpenApiClient | None = None
        self.oauth_tokens: OAuthTokenProvider | None = None
        self.account_store: AccountStore | None = None
        self.account_discovery: AccountDiscoveryService | None = None
        self.plan_catalog: PlanCatalogService | None = None
        self.monitor_plan_store: MonitorPlanStore | None = None
        self.plan_monitor: PlanMonitorService | None = None
        self.plan_state_handler: PlanStateCheckHandler | None = None

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

            # Phase 2 服务对象构造：不得在此调用 get_access_token/discover/list_all。
            open_api_client = OpenApiClient()
            oauth_tokens = OAuthTokenProvider(database, writer, open_api_client)
            account_store = AccountStore(database, writer)
            account_discovery = AccountDiscoveryService(
                open_api_client,
                oauth_tokens,
                account_store,
            )
            plan_catalog = PlanCatalogService(open_api_client, oauth_tokens)
            monitor_plan_store = MonitorPlanStore(database, writer)
            plan_monitor = PlanMonitorService(plan_catalog, monitor_plan_store, writer)

            def business_allowed() -> bool:
                return license_state.get().normal_business_allowed

            plan_state_handler = PlanStateCheckHandler(
                database,
                writer,
                monitor_plan_store,
                plan_monitor,
                business_allowed=business_allowed,
            )
            plan_state_scheduler = PlanStateScheduler(
                database,
                writer,
                business_allowed=business_allowed,
            )

            job_worker = JobWorker(
                jobs,
                handlers={PLAN_STATUS_CHECK: plan_state_handler},
            )
            lease_recovery = LeaseRecoveryWorker(jobs, DEFAULT_RECOVERY_POLICY)
            supervisor = RuntimeSupervisor()
            # 提前挂到 self，保证 supervisor.start_all 或 Diagnostics 构造失败时也能完整清理。
            self.supervisor = supervisor

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
            supervisor.register(
                ComponentSpec(
                    name="plan_state_scheduler",
                    start=plan_state_scheduler.start,
                    stop=plan_state_scheduler.stop,
                    health=plan_state_scheduler.health_snapshot,
                    restart=plan_state_scheduler.restart,
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
            self.watchdog = watchdog
            self.job_worker = job_worker
            self.lease_recovery_worker = lease_recovery
            self.plan_state_scheduler = plan_state_scheduler
            self.diagnostics = diagnostics

            self.open_api_client = open_api_client
            self.oauth_tokens = oauth_tokens
            self.account_store = account_store
            self.account_discovery = account_discovery
            self.plan_catalog = plan_catalog
            self.monitor_plan_store = monitor_plan_store
            self.plan_monitor = plan_monitor
            self.plan_state_handler = plan_state_handler

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
