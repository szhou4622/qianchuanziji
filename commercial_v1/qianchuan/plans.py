"""四类计划目录、监控计划落库与 10 分钟活跃状态检查。

直播计划与商品计划分开分类，但本地热采集资格只有一个服务器事实：
``official_status == DELIVERY_OK``。商品计划绝不引入“开播/未开播”概念。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from commercial_v1.security.redaction import sanitize_text
from commercial_v1.storage.database import Database
from commercial_v1.storage.writer import StorageWriter

from .client import ApiResponse, OpenApiClient
from .contracts import PLAN_DETAIL, PLAN_LIST
from .errors import OpenApiContractError, OpenApiTokenError
from .normalizers import NormalizedPlan, normalize_plan, require_digit_id
from .pagination import get_all_pages
from .token_provider import OAuthTokenProvider

CHINA_TZ = timezone(timedelta(hours=8))

FOUR_PLAN_CLASSES: tuple[tuple[str, str, str], ...] = (
    ("chengfang_live", "LIVE_PROM_GOODS", "OVERALL_PROJECT"),
    ("chengfang_product", "VIDEO_PROM_GOODS", "OVERALL_PROJECT"),
    ("global_live", "LIVE_PROM_GOODS", "UNI_PROJECT"),
    ("global_product", "VIDEO_PROM_GOODS", "UNI_PROJECT"),
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _catalog_window(now: datetime | None = None, *, lookback_days: int = 180) -> tuple[str, str]:
    current = (now or _utc_now()).astimezone(CHINA_TZ)
    days = max(1, int(lookback_days))
    start = current - timedelta(days=days - 1)
    return start.strftime("%Y-%m-%d 00:00:00"), current.strftime("%Y-%m-%d 23:59:59")


def _plan_identity(row: Mapping[str, Any]) -> str:
    nested = row.get("ad_info")
    source = nested if isinstance(nested, Mapping) else row
    value = source.get("ad_id") if source.get("ad_id") not in (None, "") else source.get("id")
    return str(value or "").strip()


@dataclass(frozen=True)
class PlanCatalogResult:
    plans: tuple[NormalizedPlan, ...]
    complete: bool
    evidence: Mapping[str, Any]


class PlanCatalogService:
    def __init__(self, client: OpenApiClient, token_provider: OAuthTokenProvider) -> None:
        self._client = client
        self._tokens = token_provider

    def _get_with_refresh(
        self,
        auth_profile_id: str,
        endpoint: str,
        *,
        query: Mapping[str, Any],
        advertiser_id: str,
    ) -> ApiResponse:
        token = self._tokens.get_access_token(auth_profile_id)
        try:
            return self._client.get(
                endpoint,
                query=query,
                access_token=token,
                advertiser_id=advertiser_id,
            )
        except OpenApiTokenError:
            refreshed = self._tokens.get_access_token(auth_profile_id, force_refresh=True)
            return self._client.get(
                endpoint,
                query=query,
                access_token=refreshed,
                advertiser_id=advertiser_id,
            )

    def list_all(
        self,
        auth_profile_id: str,
        advertiser_id: str,
        *,
        start_time: str = "",
        end_time: str = "",
        lookback_days: int = 180,
    ) -> PlanCatalogResult:
        aid = require_digit_id(advertiser_id, "advertiser_id")
        if not start_time or not end_time:
            start_time, end_time = _catalog_window(lookback_days=lookback_days)

        combined: list[tuple[str, NormalizedPlan]] = []
        evidence: dict[str, Any] = {
            "complete": True,
            "advertiser_id": aid,
            "start_time": start_time,
            "end_time": end_time,
            "classes": {},
        }
        for class_key, marketing_goal, adlab_scene in FOUR_PLAN_CLASSES:
            try:
                rows, request_ids = get_all_pages(
                    self._client,
                    PLAN_LIST,
                    query={
                        "advertiser_id": aid,
                        "start_time": start_time,
                        "end_time": end_time,
                        "marketing_goal": marketing_goal,
                        "adlab_scene": adlab_scene,
                    },
                    access_token=self._tokens.get_access_token(auth_profile_id),
                    advertiser_id=aid,
                    page_size=100,
                    identity_getter=_plan_identity,
                    refresh_access_token=lambda: self._tokens.get_access_token(
                        auth_profile_id,
                        force_refresh=True,
                    ),
                )
                normalized = [
                    normalize_plan(
                        row,
                        advertiser_id=aid,
                        expected_marketing_goal=marketing_goal,
                        expected_adlab_scene=adlab_scene,
                    )
                    for row in rows
                ]
                combined.extend((class_key, plan) for plan in normalized)
                evidence["classes"][class_key] = {
                    "complete": True,
                    "count": len(normalized),
                    "marketing_goal": marketing_goal,
                    "adlab_scene": adlab_scene,
                    "request_ids": request_ids,
                }
            except Exception as exc:
                evidence["complete"] = False
                evidence["classes"][class_key] = {
                    "complete": False,
                    "count": 0,
                    "marketing_goal": marketing_goal,
                    "adlab_scene": adlab_scene,
                    "error": sanitize_text(exc),
                }

        deduped: dict[str, tuple[str, NormalizedPlan]] = {}
        for class_key, plan in combined:
            previous = deduped.get(plan.ad_id)
            if previous is None:
                deduped[plan.ad_id] = (class_key, plan)
                continue
            previous_key, previous_plan = previous
            if previous_key != class_key:
                conflict = replace(
                    previous_plan,
                    classification_status="CONFLICT",
                    classification_reason=f"same_ad_id_returned_by_multiple_classes:{previous_key},{class_key}",
                )
                deduped[plan.ad_id] = (previous_key, conflict)
                evidence["complete"] = False

        plans = tuple(sorted((item[1] for item in deduped.values()), key=lambda value: value.ad_id))
        evidence["plan_count"] = len(plans)
        evidence["classification_conflicts"] = [
            plan.ad_id for plan in plans if plan.classification_status == "CONFLICT"
        ]
        return PlanCatalogResult(plans=plans, complete=bool(evidence["complete"]), evidence=evidence)

    def get_detail(
        self,
        auth_profile_id: str,
        advertiser_id: str,
        ad_id: str,
        *,
        expected_marketing_goal: str,
        expected_adlab_scene: str,
    ) -> tuple[NormalizedPlan, str]:
        aid = require_digit_id(advertiser_id, "advertiser_id")
        pid = require_digit_id(ad_id, "ad_id")
        response = self._get_with_refresh(
            auth_profile_id,
            PLAN_DETAIL,
            query={"advertiser_id": aid, "ad_id": pid},
            advertiser_id=aid,
        )
        if not isinstance(response.data, Mapping):
            raise OpenApiContractError(
                "plan detail response data is not an object",
                code="PLAN_DETAIL_INVALID_DATA",
                request_id=response.request_id,
            )
        plan = normalize_plan(
            response.data,
            advertiser_id=aid,
            expected_marketing_goal=expected_marketing_goal,
            expected_adlab_scene=expected_adlab_scene,
        )
        if plan.ad_id != pid:
            raise OpenApiContractError(
                "plan detail ad_id does not match request",
                code="PLAN_DETAIL_ID_MISMATCH",
                request_id=response.request_id,
            )
        return plan, response.request_id


class MonitorPlanStore:
    MAX_MONITORED_PER_ACCOUNT = 10

    def __init__(self, database: Database, writer: StorageWriter) -> None:
        self._database = database
        self._writer = writer

    @staticmethod
    def target_uid(advertiser_id: str, ad_id: str) -> str:
        return f"plan:{require_digit_id(advertiser_id, 'advertiser_id')}:{require_digit_id(ad_id, 'ad_id')}"

    def enroll_verified(self, plan: NormalizedPlan) -> str:
        if plan.classification_status != "VERIFIED":
            raise ValueError("计划分类尚未由平台详情确认，不能加入自动监控")
        if plan.marketing_goal not in {"LIVE_PROM_GOODS", "VIDEO_PROM_GOODS"}:
            raise ValueError("计划推广场景不明确")
        if plan.adlab_scene not in {"OVERALL_PROJECT", "UNI_PROJECT"}:
            raise ValueError("计划体系不明确")

        target_uid = self.target_uid(plan.advertiser_id, plan.ad_id)
        now_dt = _utc_now()
        now = _iso(now_dt)
        lifecycle, collection_active = self._lifecycle(plan)
        next_status = _iso(now_dt + timedelta(minutes=10)) if lifecycle == "WATCHING" else None
        next_hot = now if collection_active else None

        def work(conn):
            account = conn.execute(
                "SELECT account_uid,enabled,auth_status FROM qianchuan_account WHERE advertiser_id=?",
                (plan.advertiser_id,),
            ).fetchone()
            if account is None:
                raise ValueError("千川账户尚未落库")
            if not int(account["enabled"]):
                raise ValueError("请先启用该千川账户")
            if str(account["auth_status"]) != "ACTIVE":
                raise ValueError("千川账户授权不可用")

            existing = conn.execute(
                "SELECT monitor_enabled,created_at FROM monitor_plan WHERE target_uid=?",
                (target_uid,),
            ).fetchone()
            if existing is None or not int(existing["monitor_enabled"]):
                count = conn.execute(
                    "SELECT COUNT(*) FROM monitor_plan WHERE advertiser_id=? AND monitor_enabled=1",
                    (plan.advertiser_id,),
                ).fetchone()[0]
                if int(count) >= self.MAX_MONITORED_PER_ACCOUNT:
                    raise ValueError("每个千川账户最多监控10个计划")
            created_at = str(existing["created_at"]) if existing is not None else now
            conn.execute(
                """INSERT INTO monitor_plan(
                   target_uid,account_uid,advertiser_id,ad_id,plan_name,plan_system,promotion_scene,
                   official_status,monitor_enabled,lifecycle_state,collection_active,strategy_eligible,
                   write_eligible,sync_state,budget_cent,official_modify_time,last_status_check_at,
                   next_status_check_at,last_hot_collect_at,next_hot_collect_at,last_catalog_seen_at,
                   last_active_at,terminal_at,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,1,?,?,?,?,?,NULL,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(target_uid) DO UPDATE SET
                     account_uid=excluded.account_uid,
                     plan_name=excluded.plan_name,
                     plan_system=excluded.plan_system,
                     promotion_scene=excluded.promotion_scene,
                     official_status=excluded.official_status,
                     monitor_enabled=1,
                     lifecycle_state=excluded.lifecycle_state,
                     collection_active=excluded.collection_active,
                     strategy_eligible=excluded.strategy_eligible,
                     write_eligible=excluded.write_eligible,
                     sync_state=excluded.sync_state,
                     official_modify_time=excluded.official_modify_time,
                     last_status_check_at=excluded.last_status_check_at,
                     next_status_check_at=excluded.next_status_check_at,
                     next_hot_collect_at=excluded.next_hot_collect_at,
                     last_catalog_seen_at=excluded.last_catalog_seen_at,
                     last_active_at=excluded.last_active_at,
                     terminal_at=excluded.terminal_at,
                     updated_at=excluded.updated_at""",
                (
                    target_uid,
                    str(account["account_uid"]),
                    plan.advertiser_id,
                    plan.ad_id,
                    plan.plan_name,
                    plan.adlab_scene,
                    plan.marketing_goal,
                    plan.official_status,
                    lifecycle,
                    1 if collection_active else 0,
                    1 if collection_active else 0,
                    1 if collection_active else 0,
                    "TRUSTED",
                    plan.modify_time or None,
                    now,
                    next_status,
                    None,
                    next_hot,
                    now,
                    now if collection_active else None,
                    now if lifecycle == "TERMINAL" else None,
                    created_at,
                    now,
                ),
            )

        self._writer.transaction(work).result(timeout=5)
        return target_uid

    def set_monitor_enabled(self, target_uid: str, enabled: bool) -> None:
        now_dt = _utc_now()
        now = _iso(now_dt)
        if enabled:
            lifecycle = "WATCHING"
            next_status = now
        else:
            lifecycle = "MONITOR_DISABLED"
            next_status = None

        changed = self._writer.execute(
            """UPDATE monitor_plan SET monitor_enabled=?,lifecycle_state=?,collection_active=0,
               strategy_eligible=0,write_eligible=0,next_status_check_at=?,next_hot_collect_at=NULL,
               updated_at=? WHERE target_uid=?""",
            (1 if enabled else 0, lifecycle, next_status, now, target_uid),
        ).result(timeout=5).rowcount
        if changed != 1:
            raise ValueError("监控计划不存在")

    def get_target(self, target_uid: str) -> dict[str, Any]:
        with self._database.connect(readonly=True) as conn:
            row = conn.execute(
                """SELECT p.*,a.auth_status,
                   (SELECT auth_profile_id FROM qianchuan_account_auth aa
                    WHERE aa.account_uid=p.account_uid AND aa.is_primary=1 LIMIT 1) AS auth_profile_id
                   FROM monitor_plan p
                   JOIN qianchuan_account a ON a.account_uid=p.account_uid
                   WHERE p.target_uid=?""",
                (target_uid,),
            ).fetchone()
        if row is None:
            raise ValueError("监控计划不存在")
        return dict(row)

    def list_targets(self, *, advertiser_id: str = "") -> list[dict[str, Any]]:
        with self._database.connect(readonly=True) as conn:
            if advertiser_id:
                rows = conn.execute(
                    "SELECT * FROM monitor_plan WHERE advertiser_id=? ORDER BY monitor_enabled DESC,plan_name,ad_id",
                    (require_digit_id(advertiser_id, "advertiser_id"),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM monitor_plan ORDER BY advertiser_id,monitor_enabled DESC,plan_name,ad_id"
                ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _lifecycle(plan: NormalizedPlan) -> tuple[str, bool]:
        if plan.official_status == "DELETED":
            return "TERMINAL", False
        if plan.official_status == "DELIVERY_OK":
            return "ACTIVE_COLLECTING", True
        return "WATCHING", False


class PlanMonitorService:
    """只负责“监控计划活跃状态检查”，不承担账户全目录刷新。"""

    def __init__(
        self,
        catalog: PlanCatalogService,
        store: MonitorPlanStore,
        writer: StorageWriter,
    ) -> None:
        self._catalog = catalog
        self._store = store
        self._writer = writer

    def enroll_from_catalog(self, auth_profile_id: str, catalog_plan: NormalizedPlan) -> str:
        if catalog_plan.classification_status == "CONFLICT":
            raise ValueError("计划分类冲突，禁止加入监控")
        detail, _request_id = self._catalog.get_detail(
            auth_profile_id,
            catalog_plan.advertiser_id,
            catalog_plan.ad_id,
            expected_marketing_goal=catalog_plan.marketing_goal,
            expected_adlab_scene=catalog_plan.adlab_scene,
        )
        if detail.classification_status != "VERIFIED":
            raise ValueError("计划详情无法确认计划分类，禁止自动监控")
        return self._store.enroll_verified(detail)

    def check_active_state(self, target_uid: str) -> NormalizedPlan:
        target = self._store.get_target(target_uid)
        if not int(target["monitor_enabled"]):
            raise ValueError("该计划已停止监控")
        auth_profile_id = str(target.get("auth_profile_id") or "")
        if not auth_profile_id:
            self._freeze_target(target_uid, "AUTH_PROFILE_MISSING")
            raise ValueError("计划所属账户缺少主授权")

        plan, _request_id = self._catalog.get_detail(
            auth_profile_id,
            str(target["advertiser_id"]),
            str(target["ad_id"]),
            expected_marketing_goal=str(target["promotion_scene"]),
            expected_adlab_scene=str(target["plan_system"]),
        )
        now_dt = _utc_now()
        now = _iso(now_dt)

        if plan.classification_status != "VERIFIED":
            self._writer.execute(
                """UPDATE monitor_plan SET official_status=?,collection_active=0,strategy_eligible=0,
                   write_eligible=0,lifecycle_state='WATCHING',sync_state='PLAN_CLASSIFICATION_CONFLICT',
                   last_status_check_at=?,next_status_check_at=?,next_hot_collect_at=NULL,updated_at=?
                   WHERE target_uid=?""",
                (
                    plan.official_status or None,
                    now,
                    _iso(now_dt + timedelta(minutes=10)),
                    now,
                    target_uid,
                ),
            ).result(timeout=5)
            return plan

        lifecycle, collection_active = self._store._lifecycle(plan)
        next_status = _iso(now_dt + timedelta(minutes=10)) if lifecycle == "WATCHING" else None
        next_hot = now if collection_active else None
        terminal_at = now if lifecycle == "TERMINAL" else None
        self._writer.execute(
            """UPDATE monitor_plan SET plan_name=?,official_status=?,official_modify_time=?,
               lifecycle_state=?,collection_active=?,strategy_eligible=?,write_eligible=?,sync_state='TRUSTED',
               last_status_check_at=?,next_status_check_at=?,next_hot_collect_at=?,
               last_active_at=CASE WHEN ?=1 THEN ? ELSE last_active_at END,
               terminal_at=CASE WHEN ? IS NOT NULL THEN ? ELSE terminal_at END,
               updated_at=? WHERE target_uid=?""",
            (
                plan.plan_name,
                plan.official_status or None,
                plan.modify_time or None,
                lifecycle,
                1 if collection_active else 0,
                1 if collection_active else 0,
                1 if collection_active else 0,
                now,
                next_status,
                next_hot,
                1 if collection_active else 0,
                now,
                terminal_at,
                terminal_at,
                now,
                target_uid,
            ),
        ).result(timeout=5)
        return plan

    def _freeze_target(self, target_uid: str, reason: str) -> None:
        now_dt = _utc_now()
        now = _iso(now_dt)
        self._writer.execute(
            """UPDATE monitor_plan SET collection_active=0,strategy_eligible=0,write_eligible=0,
               lifecycle_state='WATCHING',sync_state=?,last_status_check_at=?,next_status_check_at=?,
               next_hot_collect_at=NULL,updated_at=? WHERE target_uid=?""",
            (reason, now, _iso(now_dt + timedelta(minutes=10)), now, target_uid),
        ).result(timeout=5)
