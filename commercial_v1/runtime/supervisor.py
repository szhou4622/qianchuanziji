"""Runtime Supervisor 与受控 Watchdog 基础设施。"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable

HealthFn = Callable[[], dict[str, Any]]
ActionFn = Callable[[], None]


@dataclass
class ComponentSpec:
    name: str
    start: ActionFn
    stop: ActionFn
    health: HealthFn
    restart: ActionFn | None = None
    critical: bool = True


class RuntimeSupervisor:
    def __init__(self) -> None:
        self._components: list[ComponentSpec] = []
        self._lock = threading.RLock()
        self._state = "NEW"
        self._fatal_components: set[str] = set()

    def register(self, component: ComponentSpec) -> None:
        with self._lock:
            if self._state != "NEW":
                raise RuntimeError("components can only be registered before startup")
            if any(item.name == component.name for item in self._components):
                raise ValueError(f"duplicate component: {component.name}")
            self._components.append(component)

    def start_all(self) -> None:
        with self._lock:
            if self._state == "RUNNING":
                return
            if self._state != "NEW":
                raise RuntimeError(f"cannot start supervisor from state {self._state}")
            started: list[ComponentSpec] = []
            try:
                for component in self._components:
                    component.start()
                    started.append(component)
            except BaseException:
                for component in reversed(started):
                    try:
                        component.stop()
                    except BaseException:
                        pass
                self._state = "FAILED"
                raise
            self._state = "RUNNING"

    def stop_all(self) -> None:
        with self._lock:
            if self._state == "STOPPED":
                return
            failures: list[BaseException] = []
            for component in reversed(self._components):
                try:
                    component.stop()
                except BaseException as exc:
                    failures.append(exc)
            self._state = "STOPPED" if not failures else "FAILED"
            if failures:
                raise RuntimeError(f"{len(failures)} runtime components failed to stop") from failures[0]

    def restart_component(self, name: str) -> bool:
        with self._lock:
            component = self._find(name)
            if component.restart is None:
                if component.critical:
                    self._fatal_components.add(name)
                return False
            try:
                component.restart()
            except BaseException:
                if component.critical:
                    self._fatal_components.add(name)
                return False
            self._fatal_components.discard(name)
            return True

    def mark_fatal(self, name: str) -> None:
        with self._lock:
            self._fatal_components.add(name)

    def health_snapshot(self) -> dict[str, Any]:
        with self._lock:
            components: dict[str, dict[str, Any]] = {}
            for component in self._components:
                try:
                    value = dict(component.health())
                except BaseException as exc:
                    value = {"alive": False, "health_error": type(exc).__name__}
                value.setdefault("alive", True)
                components[component.name] = value
            return {
                "state": self._state,
                "healthy": self._state == "RUNNING" and not self._fatal_components and all(v.get("alive", False) for v in components.values()),
                "fatal_components": sorted(self._fatal_components),
                "components": components,
            }

    def component_names(self) -> list[str]:
        return [item.name for item in self._components]

    def _find(self, name: str) -> ComponentSpec:
        for component in self._components:
            if component.name == name:
                return component
        raise KeyError(name)


class RuntimeWatchdog:
    def __init__(self, supervisor: RuntimeSupervisor, *, interval_seconds: float = 30.0, on_critical: Callable[[str], None] | None = None) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._supervisor = supervisor
        self._interval = interval_seconds
        self._on_critical = on_critical
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="runtime-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(1.0, self._interval * 2))

    def check_once(self) -> list[str]:
        snapshot = self._supervisor.health_snapshot()
        unhealthy: list[str] = []
        for name, health in snapshot["components"].items():
            if health.get("alive", False):
                continue
            unhealthy.append(name)
            restarted = self._supervisor.restart_component(name)
            if not restarted:
                self._supervisor.mark_fatal(name)
                if self._on_critical:
                    self._on_critical(name)
        return unhealthy

    def health_snapshot(self) -> dict[str, Any]:
        return {"alive": bool(self._thread and self._thread.is_alive()), "interval_seconds": self._interval}

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self.check_once()
