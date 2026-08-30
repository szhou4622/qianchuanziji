from commercial_v1.runtime.supervisor import ComponentSpec, RuntimeSupervisor, RuntimeWatchdog


class FakeComponent:
    def __init__(self, *, fail_start=False):
        self.alive = False
        self.fail_start = fail_start
        self.starts = 0
        self.stops = 0
    def start(self):
        self.starts += 1
        if self.fail_start:
            raise RuntimeError("boom")
        self.alive = True
    def stop(self):
        self.stops += 1
        self.alive = False
    def health(self):
        return {"alive": self.alive}
    def restart(self):
        self.alive = True
        self.starts += 1


def test_supervisor_rolls_back_started_components_on_failure() -> None:
    a, b = FakeComponent(), FakeComponent(fail_start=True)
    sup = RuntimeSupervisor()
    sup.register(ComponentSpec("a", a.start, a.stop, a.health))
    sup.register(ComponentSpec("b", b.start, b.stop, b.health))
    try:
        sup.start_all()
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected start failure")
    assert a.stops == 1
    assert sup.health_snapshot()["state"] == "FAILED"


def test_watchdog_restarts_restartable_component() -> None:
    c = FakeComponent()
    sup = RuntimeSupervisor()
    sup.register(ComponentSpec("worker", c.start, c.stop, c.health, restart=c.restart))
    sup.start_all()
    c.alive = False
    watchdog = RuntimeWatchdog(sup, interval_seconds=1)
    assert watchdog.check_once() == ["worker"]
    assert c.alive is True
    assert sup.health_snapshot()["fatal_components"] == []
    sup.stop_all()
