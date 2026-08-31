from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from commercial_v1.feishu.channel_adapter import (
    FeishuChannelBridge,
    FeishuRuntimeConfig,
    render_candidate_confirmation_card,
)
from commercial_v1.feishu.service import CANDIDATE_CONFIRM, ClaimedOutbox


PAYLOAD = {
    "contract_version": 1,
    "notification_type": "CANDIDATE_CONFIRM",
    "candidate_id": "candidate-123",
    "strategy_id": "strategy-1",
    "strategy_version_id": "strategy-v3",
    "action_type": "CREATE_RETARGET",
    "advertiser_id": "111111",
    "ad_id": "222222",
    "execution_mode": "MANUAL",
    "grouping_mode": "MERGED",
    "execution_params": {"budget": "888.80", "duration": "3"},
    "items": [
        {
            "candidate_item_id": "item-1",
            "hit_id": "hit-1",
            "object_uid": "material:111111:222222:900001",
            "material_id": "900001",
            "control_task_id": None,
            "metric_snapshot": {"overall_cost": "100"},
            "before_state": {"source_batch_id": "batch-1"},
        },
        {
            "candidate_item_id": "item-2",
            "hit_id": "hit-2",
            "object_uid": "material:111111:222222:900002",
            "material_id": "900002",
            "control_task_id": None,
            "metric_snapshot": {"overall_cost": "101"},
            "before_state": {"source_batch_id": "batch-1"},
        },
    ],
    "created_at": "2026-08-31T12:00:00+00:00",
    "expires_at": "2026-08-31T12:30:00+00:00",
    "actions": ["APPROVE", "REJECT"],
}


@dataclass
class FakeSendResult:
    success: bool = True
    message_id: str = "om_message_1"
    error: Any = None


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

    async def emit_card_action(self, event: Any) -> dict[str, Any]:
        handler = self.handlers["cardAction"]
        return await handler(event)


class FakeInbox:
    def __init__(self, *, status: str = "APPROVED") -> None:
        self.status = status
        self.calls: list[dict[str, Any]] = []

    def receive_candidate_action(self, event_id: str, **kwargs: Any) -> Any:
        self.calls.append({"event_id": event_id, **kwargs})
        return SimpleNamespace(candidate_status=self.status, changed=True)


def _claimed() -> ClaimedOutbox:
    return ClaimedOutbox(
        outbox_id="feishu_outbox_abc",
        notification_type=CANDIDATE_CONFIRM,
        route_id="route-main",
        related_candidate_id="candidate-123",
        related_execution_id=None,
        payload=PAYLOAD,
        attempt_count=1,
        claim_owner="worker-1",
        claim_expires_at="2026-08-31T12:01:00+00:00",
    )


def test_card_renderer_uses_only_frozen_payload_and_embeds_idempotent_actions() -> None:
    card = render_candidate_confirmation_card(PAYLOAD)
    assert card["schema"] == "2.0"
    assert card["header"]["title"]["content"] == "千川策略执行确认"
    markdown = card["body"]["elements"][0]["content"]
    assert "900001" in markdown
    assert "900002" in markdown
    assert "888.80" in markdown

    columns = card["body"]["elements"][2]["columns"]
    approve = columns[0]["elements"][0]["value"]
    reject = columns[1]["elements"][0]["value"]
    assert approve == {"candidate_id": "candidate-123", "action": "APPROVE"}
    assert reject == {"candidate_id": "candidate-123", "action": "REJECT"}


def test_bridge_sends_card_with_durable_outbox_id_as_feishu_uuid() -> None:
    fake_channel = FakeChannel()
    inbox = FakeInbox()
    bridge = FeishuChannelBridge(
        FeishuRuntimeConfig("cli_test", "secret", connect_timeout_seconds=1, send_timeout_seconds=1),
        inbox,  # type: ignore[arg-type]
        channel_factory=lambda _config: fake_channel,
        route_resolver=lambda route: {"route-main": "oc_chat_1"}[route],
    )
    bridge.start()
    try:
        message_id = bridge.send_outbox(_claimed())
        assert message_id == "om_message_1"
        assert len(fake_channel.sent) == 1
        target, message, opts = fake_channel.sent[0]
        assert target == "oc_chat_1"
        assert message["card"]["schema"] == "2.0"
        assert opts == {"uuid": "feishu_outbox_abc"}
        assert bridge.health_snapshot()["sent_count"] == 1
    finally:
        bridge.stop()
    assert fake_channel.stopped is True


def test_card_action_uses_platform_event_id_and_drives_persistent_inbox_contract() -> None:
    fake_channel = FakeChannel()
    inbox = FakeInbox(status="APPROVED")
    bridge = FeishuChannelBridge(
        FeishuRuntimeConfig("cli_test", "secret", connect_timeout_seconds=1),
        inbox,  # type: ignore[arg-type]
        channel_factory=lambda _config: fake_channel,
    )
    bridge.start()
    try:
        event = SimpleNamespace(
            message_id="om_message_1",
            chat_id="oc_chat_1",
            operator=SimpleNamespace(open_id="ou_user_1"),
            action=SimpleNamespace(
                tag="button",
                value={"candidate_id": "candidate-123", "action": "APPROVE"},
            ),
            raw={"header": {"event_id": "event-platform-1"}},
        )
        result = asyncio.run_coroutine_threadsafe(
            fake_channel.emit_card_action(event),
            bridge._loop,  # type: ignore[arg-type]
        ).result(timeout=2)
        assert result == {"toast": {"type": "success", "content": "已确认"}}
        assert len(inbox.calls) == 1
        call = inbox.calls[0]
        assert call["event_id"] == "event-platform-1"
        assert call["candidate_id"] == "candidate-123"
        assert call["action"] == "APPROVE"
        assert call["payload"]["operator_open_id"] == "ou_user_1"
    finally:
        bridge.stop()


def test_card_action_without_event_id_still_gets_stable_business_dedup_key() -> None:
    fake_channel = FakeChannel()
    inbox = FakeInbox(status="REJECTED")
    bridge = FeishuChannelBridge(
        FeishuRuntimeConfig("cli_test", "secret", connect_timeout_seconds=1),
        inbox,  # type: ignore[arg-type]
        channel_factory=lambda _config: fake_channel,
    )
    bridge.start()
    try:
        event = SimpleNamespace(
            message_id="om_message_1",
            chat_id="oc_chat_1",
            operator=SimpleNamespace(open_id="ou_user_1"),
            action=SimpleNamespace(
                tag="button",
                value={"candidate_id": "candidate-123", "action": "REJECT"},
            ),
            raw={},
        )
        for _ in range(2):
            result = asyncio.run_coroutine_threadsafe(
                fake_channel.emit_card_action(event),
                bridge._loop,  # type: ignore[arg-type]
            ).result(timeout=2)
            assert result == {"toast": {"type": "info", "content": "已拒绝"}}
        assert inbox.calls[0]["event_id"].startswith("card-action-")
        assert inbox.calls[0]["event_id"] == inbox.calls[1]["event_id"]
    finally:
        bridge.stop()


def test_installed_channel_sdk_exposes_the_surface_used_by_bridge() -> None:
    # 这是依赖契约测试：升级 SDK 时如果入口/配置类变化，CI 必须先红，而不是线上才发现。
    from lark_channel import FeishuChannel, OutboundConfig, RetryConfig, SecurityConfig

    assert FeishuChannel is not None
    assert OutboundConfig(retry=RetryConfig(max_attempts=1)).retry.max_attempts == 1
    assert SecurityConfig(mode="audit").mode == "audit"
