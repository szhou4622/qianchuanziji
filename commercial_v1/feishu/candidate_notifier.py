"""Phase 5 候选 → 飞书确认 Outbox 的本地桥接。

本模块只做本地数据库动作：读取已经冻结的 Candidate，并为需要人工确认的候选创建
持久 Feishu Outbox。它不联网，也不在发送时重新计算策略或重新选择素材。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from commercial_v1.candidate import CandidateService

from .service import FeishuCandidateCardService

RouteResolver = Callable[[Mapping[str, Any]], str | None]


@dataclass(frozen=True)
class CandidateNotifySummary:
    requested: int
    queued: int
    existing: int
    skipped_auto: int
    skipped_status: int
    skipped_no_route: int
    missing: int
    outbox_ids: tuple[str, ...]


class CandidateFeishuNotifier:
    """把 MANUAL + WAITING_CONFIRMATION 候选幂等写入 Feishu Outbox。"""

    def __init__(
        self,
        candidates: CandidateService,
        cards: FeishuCandidateCardService,
        route_resolver: RouteResolver,
    ) -> None:
        self._candidates = candidates
        self._cards = cards
        self._route_resolver = route_resolver

    def notify_candidates(self, candidate_ids: Sequence[str]) -> CandidateNotifySummary:
        requested = 0
        queued = 0
        existing = 0
        skipped_auto = 0
        skipped_status = 0
        skipped_no_route = 0
        missing = 0
        outbox_ids: list[str] = []

        # 去重但保持上游稳定顺序，避免一个 Durable Job 内重复做同一候选。
        seen: set[str] = set()
        for raw_id in candidate_ids:
            candidate_id = str(raw_id or "").strip()
            if not candidate_id or candidate_id in seen:
                continue
            seen.add(candidate_id)
            requested += 1

            candidate = self._candidates.get(candidate_id)
            if candidate is None:
                missing += 1
                continue
            if str(candidate.get("execution_mode") or "").upper() != "MANUAL":
                skipped_auto += 1
                continue
            if str(candidate.get("status") or "").upper() != "WAITING_CONFIRMATION":
                skipped_status += 1
                continue

            route_id = str(self._route_resolver(candidate) or "").strip()
            if not route_id:
                # 无 route 只影响“发送确认卡”这一能力，不能把候选构建本身判失败。
                skipped_no_route += 1
                continue

            envelope = self._cards.queue_candidate(candidate_id, route_id)
            outbox_ids.append(envelope.outbox_id)
            if envelope.created:
                queued += 1
            else:
                existing += 1

        return CandidateNotifySummary(
            requested=requested,
            queued=queued,
            existing=existing,
            skipped_auto=skipped_auto,
            skipped_status=skipped_status,
            skipped_no_route=skipped_no_route,
            missing=missing,
            outbox_ids=tuple(outbox_ids),
        )

    def __call__(self, candidate_ids: Sequence[str]) -> CandidateNotifySummary:
        return self.notify_candidates(candidate_ids)
