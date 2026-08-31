"""Phase 5 飞书传输生命周期管理。

Manager 自身始终是一个本地线程；只有当商业授权允许正常业务时才真正建立飞书 WebSocket
和启动 Outbox 发送 Worker。授权失效后只停止飞书传输，不影响其他独立本地能力。
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from commercial_v1.security.redaction import sanitize_text

from .channel_adapter import (
    ChannelFactory,
    FeishuChannelBridge,
    FeishuOutboxWorker,
    FeishuRuntimeConfig,
    RouteResolver,
    _default_channel_factory,
    _identity_route,
)
from .service import FeishuInboxService, FeishuOutboxStore

BusinessAllowed = Callable[[], bool]


class FeishuTransportManager:
    """按 license gate 动态启停真实飞书长连接与持久 Outbox Worker。"""

    def __init__(
        self,
        config: FeishuRuntimeConfig,
        inbox: FeishuInboxService,
        outbox: FeishuOutboxStore,
        *,
        business_allowed: BusinessAllowed,
        channel_factory: ChannelFactory = _default_channel_factory,
        route_resolver: RouteResolver = _identity_route,
        check_interval_seconds: float = 1.0,
        reconnect_delay_seconds: float = 5.0,
    ) -> None:
        config.validate()
        self._config = config
        self._inbox = inbox
        self._outbox = outbox
        self._business_allowed = business_allowed
        self._channel_factory = channel_factory
        self._route_resolver = route_resolver
        self._check_interval = max(0.1, float(check_interval_seconds))
        self._reconnect_delay = max(0.5, float(reconnect_delay_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._bridge: FeishuChannelBridge | None = None
        self._outbox_worker: FeishuOutboxWorker | None = None
        self._last_error: str | None = None
        self._last_connect_attempt_at: float | None = None
        self._next_connect_allowed_at = 0.0
        self._connect_failures = 0

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="feishu-transport-manager",
                daemon=True,
            )
            self._thread.start()

    def stop(self, *, timeout: float = 15.0) -> None:
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                raise TimeoutError("FeishuTransportManager did not stop within timeout")
        with self._lock:
            if self._thread is thread:
                self._thread = None

    def restart(self) -> None:
        self.stop()
        self.start()

    def health_snapshot(self) -> dict[str, Any]:
        with self._lock:
            thread = self._thread
            bridge = self._bridge
            worker = self._outbox_worker
            result: dict[str, Any] = {
                "configured": True,
                "alive": bool(thread and thread.is_alive()),
                "business_allowed": bool(self._business_allowed()),
                "network_active": bridge is not None,
                "last_error": self._last_error,
                "last_connect_attempt_at": self._last_connect_attempt_at,
                "connect_failures": self._connect_failures,
            }
            if bridge is not None:
                result["bridge"] = bridge.health_snapshot()
            if worker is not None:
                result["outbox_worker"] = worker.health_snapshot()
            return result

    def reconcile_once(self) -> str:
        """执行一次 license/transport 对齐，方便测试和诊断手动触发。"""

        allowed = bool(self._business_allowed())
        if not allowed:
            self._deactivate()
            return "LICENSE_BLOCKED"

        with self._lock:
            bridge = self._bridge
        if bridge is not None and bridge.ready:
            return "ACTIVE"

        now = time.monotonic()
        if now < self._next_connect_allowed_at:
            return "BACKOFF"
        try:
            self._activate()
        except Exception as exc:
            self._connect_failures += 1
            self._last_error = sanitize_text(f"{type(exc).__name__}: {exc}")[:1000]
            self._next_connect_allowed_at = time.monotonic() + self._reconnect_delay
            self._deactivate()
            return "CONNECT_FAILED"
        return "ACTIVE"

    def _activate(self) -> None:
        with self._lock:
            if self._bridge is not None and self._bridge.ready:
                return
            self._last_connect_attempt_at = time.time()

        bridge = FeishuChannelBridge(
            self._config,
            self._inbox,
            channel_factory=self._channel_factory,
            route_resolver=self._route_resolver,
        )
        bridge.start()
        worker = FeishuOutboxWorker(
            self._outbox,
            bridge,
            business_allowed=self._business_allowed,
        )
        worker.start()
        with self._lock:
            self._bridge = bridge
            self._outbox_worker = worker
            self._last_error = None
            self._next_connect_allowed_at = 0.0

    def _deactivate(self) -> None:
        with self._lock:
            worker = self._outbox_worker
            bridge = self._bridge
            self._outbox_worker = None
            self._bridge = None
        if worker is not None:
            try:
                worker.stop()
            except Exception as exc:
                self._last_error = sanitize_text(f"Feishu outbox stop failed: {exc}")[:1000]
        if bridge is not None:
            try:
                bridge.stop()
            except Exception as exc:
                self._last_error = sanitize_text(f"Feishu bridge stop failed: {exc}")[:1000]

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self.reconcile_once()
                except Exception as exc:
                    self._last_error = sanitize_text(f"Feishu manager loop failed: {exc}")[:1000]
                self._stop.wait(self._check_interval)
        finally:
            self._deactivate()
