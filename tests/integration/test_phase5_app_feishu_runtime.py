from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from commercial_v1.app import CommercialApplication
from commercial_v1.feishu import FeishuRuntimeConfig


class FakeMutex:
    def __init__(self) -> None:
        self.closed = False

    def acquire(self) -> bool:
        return True

    def close(self) -> None:
        self.closed = True


class FakeSendResult:
    success = True
    message_id = "om_fake_message"
    error = None


class FakeChannel:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}
        self.connected = False
        self.stopped = False
        self.sent: list[tuple[str, Any, Any]] = []

    def on(self, name: str, handler: Any) -> None:
        self.handlers[name] = handler

    async def connect_until_ready(self, *, timeout: float | None = 30.0) -> None:
        self.connected = True

    async def stop_background(self) -> None:
        self.stopped = True

    async def send(self, to: str, message: Any, opts: Any = None) -> FakeSendResult:
        self.sent.append((to, message, opts))
        return FakeSendResult()


NOW = "2026-08-31T13:00:00+00:00"
FUTURE = "2099-01-01T00:00:00+00:00"


def _insert_manual_candidate(app: CommercialApplication, candidate_id: str) -> None:
    assert app.writer is not None
    app.writer.execute(
        """INSERT INTO candidate_batch(
           candidate_id,action_type,advertiser_id,ad_id,execution_mode,grouping_mode,
           execution_params_json,group_fingerprint,status,created_at,expires_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            candidate_id,
            "CREATE_RETARGET",
            "111111",
            "222222",
            "MANUAL",
            "SEPARATE",
            '{"budget":"88.8"}',
            f"fingerprint-{candidate_id}",
            "WAITING_CONFIRMATION",
            NOW,
            FUTURE,
        ),
    ).result(timeout=5)


def _wait_until(predicate, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition did not become true before timeout")


def test_default_runtime_has_local_feishu_persistence_but_no_network_component(tmp_path: Path) -> None:
    app = CommercialApplication(data_dir=tmp_path, mutex=FakeMutex())  # type: ignore[arg-type]
    app.start()
    try:
        assert app.feishu_cards is not None
        assert app.feishu_outbox is not None
        assert app.feishu_inbox is not None
        assert app.candidate_feishu_notifier is None
        assert app.feishu_transport is None
        snapshot = app.diagnostics_snapshot()
        assert snapshot["feishu"]["outbox_status_counts"] == {}
        assert snapshot["feishu"]["inbox_status_counts"] == {}
        assert "feishu_transport" not in snapshot["runtime"]["components"]
    finally:
        app.stop()


def test_license_gate_connects_sends_and_disconnects_feishu_without_touching_qianchuan_post(tmp_path: Path) -> None:
    created_channels: list[FakeChannel] = []

    def factory(_config: FeishuRuntimeConfig) -> FakeChannel:
        channel = FakeChannel()
        created_channels.append(channel)
        return channel

    app = CommercialApplication(
        data_dir=tmp_path,
        mutex=FakeMutex(),  # type: ignore[arg-type]
        feishu_config=FeishuRuntimeConfig(
            "cli_test",
            "secret",
            connect_timeout_seconds=1,
            send_timeout_seconds=1,
        ),
        feishu_route_resolver=lambda candidate: (
            "oc_chat_1" if candidate["advertiser_id"] == "111111" else None
        ),
        feishu_channel_factory=factory,
    )
    app.start()
    try:
        assert app.feishu_transport is not None
        assert app.candidate_feishu_notifier is not None
        assert app.license_state is not None
        assert app.database is not None

        # 初始 license=INVALID：Manager 线程可以活着，但绝不建立飞书网络。
        assert app.feishu_transport.reconcile_once() == "LICENSE_BLOCKED"
        assert created_channels == []
        transport_health = app.diagnostics_snapshot()["feishu"]["transport"]
        assert transport_health["business_allowed"] is False
        assert transport_health["network_active"] is False

        _insert_manual_candidate(app, "candidate-runtime")
        notify = app.candidate_feishu_notifier.notify_candidates(["candidate-runtime"])
        assert notify.queued == 1
        with app.database.connect(readonly=True) as conn:
            assert conn.execute(
                "SELECT status FROM feishu_outbox WHERE related_candidate_id='candidate-runtime'"
            ).fetchone()[0] == "QUEUED"

        # 激活后才允许长连接与 Outbox 发送。
        app.license_state.mark_online_valid()
        assert app.feishu_transport.reconcile_once() in {"ACTIVE", "BACKOFF"}
        _wait_until(lambda: bool(created_channels and created_channels[-1].connected))
        _wait_until(lambda: bool(created_channels[-1].sent))
        channel = created_channels[-1]
        target, message, opts = channel.sent[0]
        assert target == "oc_chat_1"
        assert message["card"]["schema"] == "2.0"
        assert opts["uuid"].startswith("feishu_outbox_")

        with app.database.connect(readonly=True) as conn:
            assert conn.execute(
                "SELECT status FROM feishu_outbox WHERE related_candidate_id='candidate-runtime'"
            ).fetchone()[0] == "SENT"

        # 真正的 cardAction 进入持久 Inbox，并改变 Candidate；仍没有任何千川业务 POST。
        event = SimpleNamespace(
            message_id="om_fake_message",
            chat_id="oc_chat_1",
            operator=SimpleNamespace(open_id="ou_user_1"),
            action=SimpleNamespace(
                tag="button",
                value={"candidate_id": "candidate-runtime", "action": "APPROVE"},
            ),
            raw={"header": {"event_id": "evt-runtime-1"}},
        )
        result = asyncio.run(channel.handlers["cardAction"](event))
        assert result == {"toast": {"type": "success", "content": "已确认"}}
        with app.database.connect(readonly=True) as conn:
            assert conn.execute(
                "SELECT status FROM candidate_batch WHERE candidate_id='candidate-runtime'"
            ).fetchone()[0] == "APPROVED"
            assert conn.execute(
                "SELECT status FROM feishu_inbox WHERE event_id='evt-runtime-1'"
            ).fetchone()[0] == "PROCESSED"
            # Phase 5 只完成确认链路，不能偷跑 Execution/千川写入。
            assert conn.execute("SELECT COUNT(*) FROM execution_attempt").fetchone()[0] == 0

        # 明确失效后只撤掉飞书网络能力；本地 Runtime 继续活着。
        app.license_state.mark_explicit_invalid("LICENSE_TEST_INVALID")
        assert app.feishu_transport.reconcile_once() == "LICENSE_BLOCKED"
        _wait_until(lambda: channel.stopped)
        snapshot = app.diagnostics_snapshot()
        assert snapshot["runtime"]["components"]["feishu_transport"]["alive"] is True
        assert snapshot["feishu"]["transport"]["network_active"] is False
        assert snapshot["runtime"]["components"]["storage_writer"]["alive"] is True
    finally:
        app.stop()
