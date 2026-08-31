"""Phase 5 飞书真实传输适配层。

这一层把已经持久化的 Feishu Outbox/Inbox 接到官方 Channel SDK：
- 使用 ``lark-channel-sdk`` 的 WebSocket 长连接接收 CardAction；
- 使用同一 Channel 发送 CardKit 2.0 确认卡；
- 每条 Outbox 使用稳定 ``uuid=outbox_id``，让崩溃/网络未知后的重发仍具平台幂等键；
- SDK 内部发送重试固定为 1 次，真正的重试节奏由本地持久 Outbox 统一管理；
- 本模块不创建任何千川 Execution，也不调用千川业务 POST。

真实凭据由上层注入；本文件不负责把 app_secret 落盘。商业版后续应从 DPAPI 保护的
配置存储读取后再构造 :class:`FeishuChannelBridge`。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from commercial_v1.security.redaction import sanitize_text

from .service import (
    ACTION_APPROVE,
    ACTION_REJECT,
    CANDIDATE_CONFIRM,
    ClaimedOutbox,
    FeishuInboxService,
    FeishuOutboxStore,
)


class FeishuTransportError(RuntimeError):
    """飞书传输错误；默认允许 Outbox 按自身策略重试。"""


class FeishuPermanentTransportError(FeishuTransportError):
    """本地 payload/route 明确无效，不应靠重复发送解决。"""


@dataclass(frozen=True)
class FeishuRuntimeConfig:
    app_id: str
    app_secret: str = field(repr=False)
    connect_timeout_seconds: float = 30.0
    send_timeout_seconds: float = 20.0

    def validate(self) -> None:
        if not str(self.app_id or "").strip():
            raise ValueError("Feishu app_id is required")
        if not str(self.app_secret or "").strip():
            raise ValueError("Feishu app_secret is required")
        if self.connect_timeout_seconds <= 0:
            raise ValueError("connect_timeout_seconds must be positive")
        if self.send_timeout_seconds <= 0:
            raise ValueError("send_timeout_seconds must be positive")


class _ChannelLike(Protocol):
    def on(self, name: str, handler: Callable[..., Any]) -> Any: ...

    async def connect_until_ready(self, *, timeout: float | None = 30.0) -> None: ...

    async def stop_background(self) -> None: ...

    async def send(self, to: str, message: Any, opts: Any = None) -> Any: ...


ChannelFactory = Callable[[FeishuRuntimeConfig], _ChannelLike]
RouteResolver = Callable[[str], str]
BusinessAllowed = Callable[[], bool]


def _always_allowed() -> bool:
    return True


def _identity_route(route_id: str) -> str:
    return route_id


def _default_channel_factory(config: FeishuRuntimeConfig) -> _ChannelLike:
    """延迟导入 SDK，避免只做本地单测/诊断时产生额外启动副作用。"""

    try:
        from lark_channel import FeishuChannel, OutboundConfig, RetryConfig, SecurityConfig
    except Exception as exc:  # pragma: no cover - 真实安装环境覆盖
        raise FeishuTransportError(
            "lark-channel-sdk is not installed or cannot be imported"
        ) from exc

    # Outbox 自己已经是持久重试系统。这里必须关闭 SDK 多次重试，否则一次 Outbox claim
    # 会在不可见处变成多次发送，破坏审计语义。
    outbound = OutboundConfig(retry=RetryConfig(max_attempts=1))
    security = SecurityConfig(mode="audit")
    return FeishuChannel(
        app_id=config.app_id,
        app_secret=config.app_secret,
        transport="ws",
        outbound=outbound,
        security=security,
    )


def _safe_text(value: Any, *, limit: int = 300) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _safe_json(value: Any, *, limit: int = 1200) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        text = "<unserializable>"
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def render_candidate_confirmation_card(payload: Mapping[str, Any]) -> dict[str, Any]:
    """把冻结的候选 envelope 渲染成 CardKit 2.0。

    这里只读冻结 payload，绝不在发送时重新查询/重选素材。
    """

    candidate_id = str(payload.get("candidate_id") or "").strip()
    if not candidate_id:
        raise FeishuPermanentTransportError("candidate card payload missing candidate_id")
    action_type = _safe_text(payload.get("action_type"), limit=80) or "UNKNOWN"
    advertiser_id = _safe_text(payload.get("advertiser_id"), limit=80)
    ad_id = _safe_text(payload.get("ad_id"), limit=80)
    strategy_id = _safe_text(payload.get("strategy_id"), limit=120)
    strategy_version_id = _safe_text(payload.get("strategy_version_id"), limit=120)
    expires_at = _safe_text(payload.get("expires_at"), limit=80)
    execution_params = payload.get("execution_params") or {}
    raw_items = payload.get("items") or []
    if not isinstance(raw_items, list):
        raise FeishuPermanentTransportError("candidate card items must be a list")

    object_lines: list[str] = []
    for item in raw_items[:20]:
        if not isinstance(item, Mapping):
            continue
        object_id = (
            item.get("material_id")
            or item.get("control_task_id")
            or item.get("object_uid")
            or "unknown"
        )
        object_lines.append(f"- `{_safe_text(object_id, limit=120)}`")
    if len(raw_items) > 20:
        object_lines.append(f"- …另有 {len(raw_items) - 20} 个对象")
    if not object_lines:
        object_lines.append("- 无可展示对象")

    detail_parts = [
        f"**动作**：`{action_type}`",
        f"**账户 / 计划**：`{advertiser_id}` / `{ad_id}`",
        f"**策略**：`{strategy_id}` · `{strategy_version_id}`",
        f"**候选对象（{len(raw_items)}）**：\n" + "\n".join(object_lines),
        f"**冻结执行参数**：`{_safe_json(execution_params)}`",
    ]
    if expires_at:
        detail_parts.append(f"**确认有效期至**：`{expires_at}`")

    approve_value = {"candidate_id": candidate_id, "action": ACTION_APPROVE}
    reject_value = {"candidate_id": candidate_id, "action": ACTION_REJECT}

    return {
        "schema": "2.0",
        "config": {},
        "header": {
            "title": {"tag": "plain_text", "content": "千川策略执行确认"},
            "template": "blue",
        },
        "body": {
            "elements": [
                {"tag": "markdown", "content": "\n\n".join(detail_parts)},
                {"tag": "hr"},
                {
                    "tag": "column_set",
                    "columns": [
                        {
                            "tag": "column",
                            "elements": [
                                {
                                    "tag": "button",
                                    "text": {"tag": "plain_text", "content": "确认执行"},
                                    "type": "primary",
                                    "value": approve_value,
                                }
                            ],
                        },
                        {
                            "tag": "column",
                            "elements": [
                                {
                                    "tag": "button",
                                    "text": {"tag": "plain_text", "content": "拒绝"},
                                    "type": "default",
                                    "value": reject_value,
                                }
                            ],
                        },
                    ],
                },
                {
                    "tag": "markdown",
                    "content": f"<font color='grey'>candidate: {candidate_id}</font>",
                },
            ]
        },
    }


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _extract_action_value(event: Any) -> Mapping[str, Any] | None:
    action = getattr(event, "action", None)
    value = getattr(action, "value", None)
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except Exception:
            return None
        return decoded if isinstance(decoded, Mapping) else None
    return None


def _extract_event_id(event: Any, candidate_id: str, action: str) -> str:
    raw = _mapping(getattr(event, "raw", None)) or {}
    header = _mapping(raw.get("header")) or {}
    event_id = str(header.get("event_id") or raw.get("event_id") or "").strip()
    if event_id:
        return event_id

    # Channel SDK 自身已有 WS 去重；这里仍生成稳定业务幂等键作为第二道保险。
    identity = {
        "message_id": str(getattr(event, "message_id", "") or ""),
        "chat_id": str(getattr(event, "chat_id", "") or ""),
        "operator": str(getattr(getattr(event, "operator", None), "open_id", "") or ""),
        "candidate_id": candidate_id,
        "action": action,
    }
    raw_bytes = json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return "card-action-" + hashlib.sha256(raw_bytes).hexdigest()


class FeishuChannelBridge:
    """在专用 asyncio 线程中维护官方 Channel WebSocket 长连接。"""

    def __init__(
        self,
        config: FeishuRuntimeConfig,
        inbox: FeishuInboxService,
        *,
        channel_factory: ChannelFactory = _default_channel_factory,
        route_resolver: RouteResolver = _identity_route,
    ) -> None:
        config.validate()
        self._config = config
        self._inbox = inbox
        self._channel_factory = channel_factory
        self._route_resolver = route_resolver
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._async_stop: asyncio.Event | None = None
        self._channel: _ChannelLike | None = None
        self._ready_event = threading.Event()
        self._stop_requested = threading.Event()
        self._fatal_error: str | None = None
        self._last_event_at: float | None = None
        self._sent_count = 0

    @property
    def ready(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive() and self._ready_event.is_set() and self._fatal_error is None)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if self._thread is not None:
                raise RuntimeError("FeishuChannelBridge cannot be restarted after stop; construct a new bridge")
            self._stop_requested.clear()
            self._fatal_error = None
            self._ready_event.clear()
            self._thread = threading.Thread(
                target=self._thread_main,
                name="feishu-channel",
                daemon=True,
            )
            self._thread.start()

        # Runtime Supervisor 的 start 语义要求连接建立失败能够显式失败，而不是后台静默坏死。
        deadline = time.monotonic() + self._config.connect_timeout_seconds + 2.0
        while time.monotonic() < deadline:
            if self._ready_event.wait(timeout=0.05):
                return
            thread = self._thread
            if thread is None or not thread.is_alive():
                break
        if self._fatal_error:
            raise FeishuTransportError(self._fatal_error)
        raise FeishuTransportError("Feishu Channel did not become ready before timeout")

    def stop(self, *, timeout: float = 10.0) -> None:
        self._stop_requested.set()
        with self._lock:
            loop = self._loop
            async_stop = self._async_stop
            thread = self._thread
        if loop is not None and async_stop is not None:
            try:
                loop.call_soon_threadsafe(async_stop.set)
            except RuntimeError:
                pass
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                raise TimeoutError("FeishuChannelBridge did not stop within timeout")

    def restart(self) -> None:
        # 官方 Channel 对象本身可自动重连；Supervisor 真正重启时重新构造组件更安全。
        # 当前 ComponentSpec 若直接调用 restart，明确拒绝隐藏地复用已停止线程。
        raise RuntimeError("FeishuChannelBridge restart requires reconstructing the bridge")

    def send_outbox(self, claimed: ClaimedOutbox) -> str | None:
        if claimed.notification_type != CANDIDATE_CONFIRM:
            raise FeishuPermanentTransportError(
                f"unsupported Feishu notification type: {claimed.notification_type}"
            )
        route_id = str(claimed.route_id or "").strip()
        if not route_id:
            raise FeishuPermanentTransportError("Feishu outbox route_id is empty")
        target = str(self._route_resolver(route_id) or "").strip()
        if not target:
            raise FeishuPermanentTransportError("Feishu route resolver returned empty target")
        if not self.ready:
            raise FeishuTransportError("Feishu Channel is not ready")

        card = render_candidate_confirmation_card(claimed.payload)
        with self._lock:
            loop = self._loop
            channel = self._channel
        if loop is None or channel is None:
            raise FeishuTransportError("Feishu Channel loop is unavailable")

        # uuid 直接用 durable outbox_id：即便进程在“飞书已收到、但本地未 mark_sent”时崩溃，
        # 下次 Outbox retry 仍携带同一幂等键。
        future = asyncio.run_coroutine_threadsafe(
            channel.send(target, {"card": card}, {"uuid": claimed.outbox_id}),
            loop,
        )
        try:
            result = future.result(timeout=self._config.send_timeout_seconds)
        except Exception as exc:
            raise FeishuTransportError(sanitize_text(f"Feishu send failed: {exc}")) from exc

        if not bool(getattr(result, "success", False)):
            error = getattr(result, "error", None)
            raise FeishuTransportError(
                sanitize_text(f"Feishu send returned failure: {error or 'unknown error'}")
            )
        message_id = getattr(result, "message_id", None)
        self._sent_count += 1
        return str(message_id) if message_id else None

    def health_snapshot(self) -> dict[str, Any]:
        thread = self._thread
        return {
            "configured": True,
            "ready": self.ready,
            "alive": bool(thread and thread.is_alive()),
            "fatal_error": self._fatal_error,
            "last_event_at": self._last_event_at,
            "sent_count": self._sent_count,
        }

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with self._lock:
            self._loop = loop
        try:
            loop.run_until_complete(self._async_main())
        except BaseException as exc:
            self._fatal_error = sanitize_text(f"{type(exc).__name__}: {exc}")[:1000]
        finally:
            self._ready_event.clear()
            with self._lock:
                self._channel = None
                self._async_stop = None
                self._loop = None
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            finally:
                loop.close()

    async def _async_main(self) -> None:
        channel = self._channel_factory(self._config)
        channel.on("cardAction", self._on_card_action)
        async_stop = asyncio.Event()
        with self._lock:
            self._channel = channel
            self._async_stop = async_stop
        await channel.connect_until_ready(timeout=self._config.connect_timeout_seconds)
        self._ready_event.set()
        if self._stop_requested.is_set():
            async_stop.set()
        try:
            await async_stop.wait()
        finally:
            await channel.stop_background()

    async def _on_card_action(self, event: Any) -> dict[str, Any]:
        self._last_event_at = time.time()
        value = _extract_action_value(event)
        if value is None:
            return {"toast": {"type": "error", "content": "无法识别该确认操作"}}
        candidate_id = str(value.get("candidate_id") or "").strip()
        action = str(value.get("action") or "").strip().upper()
        if not candidate_id or action not in {ACTION_APPROVE, ACTION_REJECT}:
            return {"toast": {"type": "error", "content": "确认参数无效"}}

        event_id = _extract_event_id(event, candidate_id, action)
        raw = _mapping(getattr(event, "raw", None)) or {}
        callback_payload = {
            "message_id": str(getattr(event, "message_id", "") or ""),
            "chat_id": str(getattr(event, "chat_id", "") or ""),
            "operator_open_id": str(
                getattr(getattr(event, "operator", None), "open_id", "") or ""
            ),
            "action_tag": str(getattr(getattr(event, "action", None), "tag", "") or ""),
            "action_value": dict(value),
            "raw": dict(raw),
        }
        try:
            decision = self._inbox.receive_candidate_action(
                event_id,
                candidate_id=candidate_id,
                action=action,
                payload=callback_payload,
            )
        except Exception:
            # 回调必须尽快给用户一个通用反馈；详细异常已经由 Inbox/本地诊断负责记录，
            # 不能把内部异常文本或 secret 回传到飞书客户端。
            return {"toast": {"type": "error", "content": "操作未完成，请稍后重试"}}

        if decision.candidate_status == "EXPIRED":
            return {"toast": {"type": "warning", "content": "该确认已过期"}}
        if decision.candidate_status == "APPROVED":
            return {"toast": {"type": "success", "content": "已确认"}}
        if decision.candidate_status == "REJECTED":
            return {"toast": {"type": "info", "content": "已拒绝"}}
        return {"toast": {"type": "info", "content": "操作已记录"}}


class FeishuOutboxWorker:
    """把持久 Outbox claim 后交给真实 Channel 发送。"""

    def __init__(
        self,
        outbox: FeishuOutboxStore,
        bridge: FeishuChannelBridge,
        *,
        owner: str = "feishu-outbox-worker",
        business_allowed: BusinessAllowed = _always_allowed,
        idle_seconds: float = 0.5,
        claim_lease_seconds: int = 45,
    ) -> None:
        self._outbox = outbox
        self._bridge = bridge
        self._owner = str(owner or "feishu-outbox-worker")
        self._business_allowed = business_allowed
        self._idle_seconds = max(0.05, float(idle_seconds))
        self._claim_lease_seconds = max(5, int(claim_lease_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._sent = 0
        self._failed = 0
        self._last_error: str | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if self._thread is not None:
                raise RuntimeError("FeishuOutboxWorker cannot be restarted after stop")
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name=self._owner, daemon=True)
            self._thread.start()

    def stop(self, *, timeout: float = 10.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                raise TimeoutError("FeishuOutboxWorker did not stop within timeout")

    def restart(self) -> None:
        raise RuntimeError("FeishuOutboxWorker restart requires reconstructing the worker")

    def health_snapshot(self) -> dict[str, Any]:
        thread = self._thread
        return {
            "alive": bool(thread and thread.is_alive()),
            "bridge_ready": self._bridge.ready,
            "sent": self._sent,
            "failed": self._failed,
            "last_error": self._last_error,
            "license_blocked": not self._business_allowed(),
        }

    def run_once(self) -> bool:
        if not self._business_allowed() or not self._bridge.ready:
            return False
        claimed = self._outbox.claim_next(self._owner, lease_seconds=self._claim_lease_seconds)
        if claimed is None:
            return False
        try:
            self._bridge.send_outbox(claimed)
        except FeishuPermanentTransportError as exc:
            self._failed += 1
            self._last_error = sanitize_text(str(exc))[:1000]
            self._outbox.mark_failed(claimed, self._last_error, retryable=False)
        except Exception as exc:
            self._failed += 1
            self._last_error = sanitize_text(f"{type(exc).__name__}: {exc}")[:1000]
            self._outbox.mark_failed(claimed, self._last_error, retryable=True)
        else:
            self._sent += 1
            self._last_error = None
            self._outbox.mark_sent(claimed)
        return True

    def _run(self) -> None:
        try:
            self._outbox.recover_expired_claims()
        except Exception as exc:
            self._last_error = sanitize_text(f"outbox recovery failed: {exc}")[:1000]
        while not self._stop.is_set():
            processed = False
            try:
                processed = self.run_once()
            except Exception as exc:
                self._last_error = sanitize_text(f"outbox worker failed: {exc}")[:1000]
            if not processed:
                self._stop.wait(self._idle_seconds)
