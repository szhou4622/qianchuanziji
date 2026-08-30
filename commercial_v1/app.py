"""千川商业版 V1 本地 Runtime 入口。

Phase 3 已接入只读素材/调控任务热采集、异常对象证据确认、账户公平调度与单账户并发
上限。应用启动本身不访问千川网络；只有用户显式读取，或激活有效且存在到期
WATCHING / ACTIVE_COLLECTING / 确认任务时，才由受控 Worker 发起官方 GET。
所有千川投放业务 POST 仍被 OpenApiClient 禁止。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from commercial_v1.diagnostics.service import DiagnosticsService
from commercial_v1.license.runtime_state import LicenseRuntimeStateStore
from commercial_v1.qianchuan import (
    CONTROL_5M,
    CONTROL_CONFIRM,
    MATERIAL_5M,
    MATERIAL_CONFIRM,
    PLAN_STATUS_CHECK,
    AccountDiscoveryService,
    AccountStore,
    AdvertiserConcurrencyGate,
    FairHotCollectionScheduler,
    HotCollectionHandler,
    HotCollectionService,
    HotConfirmationHandler,
    HotConfirmationScheduler,
    HotConfirmationService,
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

APP_VERSION = "0.3.0"


class AlreadyRunningError(RuntimeError):
    pass


class RuntimeBlockedError(RuntimeError):
    pass


class CommercialApplication:
    """商业版应用容器。"""

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
        self.hot_workers: list[JobWorker] = []
        self.lease_recovery_worker: LeaseRecoveryWorker | None = None
        self.plan_state_scheduler: PlanStateScheduler | None = None
        self.hot_collection_scheduler: FairHotCollectionScheduler | None = None
        self.hot_confirmation_scheduler: HotConfirmationScheduler | None = None
        self.hot_account_gate: AdvertiserConcurrencyGate | None = None
        self.diagnostics: DiagnosticsService | None = None

        self.open_api_client: OpenApiClient | None = None
        self.oauth_tokens: OAuthTokenProvider | None = None
        self.account_store: AccountStore | None = None
        self.account_discovery: AccountDiscoveryService | None = None
        self.plan_catalog: PlanCatalogService | None = None
        self.monitor_plan_store: MonitorPlanStore | None = None
        self.plan_monitor: PlanMonitorService | None = None
        self.plan_state_handler: PlanStateCheckHandler | None = None
        self.hot_collection: HotCollectionService | None = None
        self.material_hot_handler: HotCollectionHandler | None = None
        self.control_hot_handler: HotCollectionHandler | None = None
        self.hot_confirmation: HotConfirmationService | None = None
        self.material_confirmation_handler: HotConfirmationHandler | None = None
        self.control_confirmation_handler: HotConfirmationHandler | None = None

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

            # 这里只构造服务对象，禁止启动时主动访问千川网络。
            open_api_client = OpenApiClient()
            oauth_tokens = OAuthTokenProvider(database, writer, open_api_client)
            account_store = AccountStore(database, writer)
            account_discovery = AccountDiscoveryService(open_api_client, oauth_tokens, account_store)
            plan_catalog = PlanCatalogService(open_api_client, oauth_tokens)
            monitor_plan_store = MonitorPlanStore(database, writer)
            plan_monitor = PlanMonitorService(plan_catalog, monitor_plan_store, writer)
            hot_collection = HotCollectionService(database, writer, open_api_client, oauth_tokens)
            hot_confirmation = HotConfirmationService(database, writer, open_api_client, oauth_tokens)

            def business_allowed() -> bool:
                return license_state.get().normal_business_allowed

            plan_state_handler = PlanStateCheckHandler(
                database, writer, monitor_plan_store, plan_monitor, business_allowed=business_allowed
            )
            plan_state_scheduler = PlanStateScheduler(database, writer, business_allowed=business_allowed)

            material_hot_handler = HotCollectionHandler(
                hot_collection, writer, MATERIAL_5M, business_allowed=business_allowed
            )
            control_hot_handler = HotCollectionHandler(
                hot_collection, writer, CONTROL_5M, business_allowed=business_allowed
            )
            material_confirmation_handler = HotConfirmationHandler(
                hot_confirmation, MATERIAL_CONFIRM, business_allowed=business_allowed
            )
            control_confirmation_handler = HotConfirmationHandler(
                hot_confirmation, CONTROL_CONFIRM, business_allowed=business_allowed
            )

            hot_collection_scheduler = FairHotCollectionScheduler(
                database, writer, business_allowed=business_allowed
            )
            hot_confirmation_scheduler = HotConfirmationScheduler(
                database, writer, business_allowed=business_allowed
            )
            hot_account_gate = AdvertiserConcurrencyGate(database, max_per_advertiser=2)

            job_worker = JobWorker(
                jobs,
                handlers={PLAN_STATUS_CHECK: plan_state_handler},
                instance_id="plan-state-worker",
            )

            # 全局热读池 6；同一 advertiser 由共享 Gate 硬限制最多 2 个并发读取。
            # Scheduler 先按账户轮转再分层 priority，避免 100 计划时头部账户霸占队列。
            hot_handlers = {
                MATERIAL_5M: hot_account_gate.wrap(material_hot_handler),
                CONTROL_5M: hot_account_gate.wrap(control_hot_handler),
                MATERIAL_CONFIRM: hot_account_gate.wrap(material_confirmation_handler),
                CONTROL_CONFIRM: hot_account_gate.wrap(control_confirmation_handler),
            }
            hot_workers = [
                JobWorker(
                    jobs,
                    handlers=hot_handlers,
                    instance_id=f"hot-read-worker-{index}",
                    lease_seconds=90,
                )
                for index in range(1, 7)
            ]

            lease_recovery = LeaseRecoveryWorker(jobs, DEFAULT_RECOVERY_POLICY)
            supervisor = RuntimeSupervisor()
            self.supervisor = supervisor

            supervisor.register(
                ComponentSpec(
                    name="storage_writer", start=writer.start, stop=writer.close,
                    health=writer.health_snapshot, restart=None, critical=True,
                )
            )
            # Phase 2 已暴露该诊断组件名；升级不能破坏旧契约。
            supervisor.register(
                ComponentSpec(
                    name="job_worker", start=job_worker.start, stop=job_worker.stop,
                    health=job_worker.health_snapshot, restart=job_worker.restart, critical=True,
                )
            )
            for index, worker in enumerate(hot_workers, 1):
                supervisor.register(
                    ComponentSpec(
                        name=f"hot_read_worker_{index}", start=worker.start, stop=worker.stop,
                        health=worker.health_snapshot, restart=worker.restart, critical=True,
                    )
                )
            supervisor.register(
                ComponentSpec(
                    name="lease_recovery", start=lease_recovery.start, stop=lease_recovery.stop,
                    health=lease_recovery.health_snapshot, restart=lease_recovery.restart, critical=True,
                )
            )
            supervisor.register(
                ComponentSpec(
                    name="plan_state_scheduler", start=plan_state_scheduler.start,
                    stop=plan_state_scheduler.stop, health=plan_state_scheduler.health_snapshot,
                    restart=plan_state_scheduler.restart, critical=True,
                )
            )
            supervisor.register(
                ComponentSpec(
                    name="hot_collection_scheduler", start=hot_collection_scheduler.start,
                    stop=hot_collection_scheduler.stop, health=hot_collection_scheduler.health_snapshot,
                    restart=hot_collection_scheduler.restart, critical=True,
                )
            )
            supervisor.register(
                ComponentSpec(
                    name="hot_confirmation_scheduler", start=hot_confirmation_scheduler.start,
                    stop=hot_confirmation_scheduler.stop, health=hot_confirmation_scheduler.health_snapshot,
                    restart=hot_confirmation_scheduler.restart, critical=True,
                )
            )

            watchdog = RuntimeWatchdog(supervisor)

            def restart_watchdog() -> None:
                watchdog.stop()
                watchdog.start()

            supervisor.register(
                ComponentSpec(
                    name="watchdog", start=watchdog.start, stop=watchdog.stop,
                    health=watchdog.health_snapshot, restart=restart_watchdog, critical=True,
                )
            )
            supervisor.start_all()

            diagnostics = DiagnosticsService(
                database, health_service, writer, jobs, license_state,
                supervisor=supervisor, app_version=self._app_version,
            )

            self.database = database
            self.writer = writer
            self.jobs = jobs
            self.license_state = license_state
            self.health = health_service
            self.recovery_report = recovery_report
            self.watchdog = watchdog
            self.job_worker = job_worker
            self.hot_workers = hot_workers
            self.lease_recovery_worker = lease_recovery
            self.plan_state_scheduler = plan_state_scheduler
            self.hot_collection_scheduler = hot_collection_scheduler
            self.hot_confirmation_scheduler = hot_confirmation_scheduler
            self.hot_account_gate = hot_account_gate
            self.diagnostics = diagnostics

            self.open_api_client = open_api_client
            self.oauth_tokens = oauth_tokens
            self.account_store = account_store
            self.account_discovery = account_discovery
            self.plan_catalog = plan_catalog
            self.monitor_plan_store = monitor_plan_store
            self.plan_monitor = plan_monitor
            self.plan_state_handler = plan_state_handler
            self.hot_collection = hot_collection
            self.material_hot_handler = material_hot_handler
            self.control_hot_handler = control_hot_handler
            self.hot_confirmation = hot_confirmation
            self.material_confirmation_handler = material_confirmation_handler
            self.control_confirmation_handler = control_confirmation_handler

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
        if self.hot_account_gate is not None:
            result["hot_account_concurrency"] = self.hot_account_gate.snapshot()
        return result

    def __enter__(self) -> "CommercialApplication":
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()
