"""Phase 2 千川账户与计划规范化。

原则：
- 只使用已经确认的字段路径，不递归猜测任意嵌套 ID；
- OAuth 主体不是最终 advertiser，最终账户必须来自可证明的业务账户接口；
- 官方状态原样保存，本地生命周期由上层单独计算；
- 计划四分类允许使用“请求查询条件”作为目录展示证据，但只有平台回显一致时才算 VERIFIED。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .errors import OpenApiContractError


def text_id(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def require_digit_id(value: Any, field: str) -> str:
    result = text_id(value)
    if not result or not result.isdigit():
        raise OpenApiContractError(f"{field} must be a complete numeric id", code="INVALID_ID")
    return result


def _first_direct(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return ""


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _clean_upper(value: Any) -> str:
    return _clean_text(value).upper()


@dataclass(frozen=True)
class OAuthSubject:
    subject_id: str
    subject_name: str
    role: str
    shop_id: str
    subject_kind: str


@dataclass(frozen=True)
class FinalAdvertiser:
    advertiser_id: str
    advertiser_name: str
    account_type: str
    source: str


@dataclass(frozen=True)
class NormalizedPlan:
    advertiser_id: str
    ad_id: str
    plan_name: str
    marketing_goal: str
    adlab_scene: str
    official_status: str
    official_opt_status: str
    modify_time: str
    budget_decimal: str | None
    classification_status: str
    classification_reason: str

    @property
    def is_deleted(self) -> bool:
        return self.official_status == "DELETED"

    @property
    def is_delivering(self) -> bool:
        return self.official_status == "DELIVERY_OK"

    @property
    def classification_verified(self) -> bool:
        return self.classification_status == "VERIFIED"


def normalize_oauth_subject(row: Mapping[str, Any]) -> OAuthSubject:
    """规范化 OAuth 授权主体，但绝不把它直接当作最终千川账户。"""
    subject_id = text_id(
        _first_direct(row, "advertiser_id", "advertiserId", "account_id", "id")
    )
    role = _clean_upper(
        _first_direct(row, "role", "advertiser_role", "account_role", "account_type")
    )
    shop_id = text_id(_first_direct(row, "shop_id", "shopId"))
    subject_name = _clean_text(
        _first_direct(
            row,
            "advertiser_name",
            "advertiserName",
            "account_name",
            "name",
        )
    )

    if shop_id or "SHOP" in role:
        subject_kind = "SHOP"
    elif any(marker in role for marker in ("ENTERPRISE", "BP", "OPERATOR", "AGENT")):
        subject_kind = "ENTERPRISE_OR_OPERATOR"
    elif "ADVERTISER" in role:
        subject_kind = "ADVERTISER_SUBJECT"
    else:
        subject_kind = "UNKNOWN"

    return OAuthSubject(
        subject_id=subject_id,
        subject_name=subject_name,
        role=role,
        shop_id=shop_id,
        subject_kind=subject_kind,
    )


def normalize_final_advertiser(row: Mapping[str, Any], *, source: str) -> FinalAdvertiser:
    """从最终业务账户接口规范化 advertiser_id。

    EBP 接口的已确认最终身份字段是 ``account_id``；若该字段缺失，不允许退回
    OAuth 主体 ID 猜测。Shop/PublicInfo 接口允许各自已知的 advertiser/account 字段。
    """
    source_key = _clean_upper(source)
    if source_key == "EBP":
        advertiser_id = text_id(_first_direct(row, "account_id"))
        if not advertiser_id:
            raise OpenApiContractError(
                "EBP advertiser row missing account_id",
                code="EBP_ACCOUNT_ID_MISSING",
            )
    elif source_key == "SHOP":
        advertiser_id = text_id(_first_direct(row, "advertiser_id", "account_id"))
        if not advertiser_id:
            raise OpenApiContractError(
                "shop advertiser row missing advertiser_id/account_id",
                code="SHOP_ADVERTISER_ID_MISSING",
            )
    elif source_key == "PUBLIC_INFO":
        advertiser_id = text_id(_first_direct(row, "account_id", "advertiser_id"))
        if not advertiser_id:
            raise OpenApiContractError(
                "public info row missing account_id/advertiser_id",
                code="PUBLIC_ACCOUNT_ID_MISSING",
            )
    else:
        raise OpenApiContractError(
            f"unsupported final advertiser source: {source_key}",
            code="INVALID_ACCOUNT_SOURCE",
        )

    require_digit_id(advertiser_id, "advertiser_id")
    name = _clean_text(
        _first_direct(
            row,
            "account_name",
            "advertiser_name",
            "advertiserName",
            "name",
        )
    )
    account_type = _clean_upper(
        _first_direct(row, "account_type", "role", "advertiser_role")
    )
    return FinalAdvertiser(
        advertiser_id=advertiser_id,
        advertiser_name=name,
        account_type=account_type or "QIANCHUAN",
        source=source_key,
    )


def _normalize_adlab_scene(value: Any) -> str:
    raw = _clean_upper(value)
    if raw in {"OVERALL_PROJECT", "1"}:
        return "OVERALL_PROJECT"
    if raw in {"UNI_PROJECT", "0"}:
        return "UNI_PROJECT"
    return ""


def _normalize_marketing_goal(value: Any) -> str:
    raw = _clean_upper(value)
    if raw in {"LIVE_PROM_GOODS", "VIDEO_PROM_GOODS"}:
        return raw
    return ""


def _decimal_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise OpenApiContractError("plan budget is not a valid decimal", code="PLAN_BUDGET_INVALID") from exc
    if not number.is_finite():
        raise OpenApiContractError("plan budget is not finite", code="PLAN_BUDGET_INVALID")
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def normalize_plan(
    row: Mapping[str, Any],
    *,
    advertiser_id: str,
    expected_marketing_goal: str = "",
    expected_adlab_scene: str = "",
) -> NormalizedPlan:
    """规范化计划并校验“四类计划”分类证据。"""
    aid = require_digit_id(advertiser_id, "advertiser_id")
    wrapped = row.get("ad_info")
    plan_row = wrapped if isinstance(wrapped, Mapping) else row

    ad_id = text_id(_first_direct(plan_row, "ad_id", "id"))
    require_digit_id(ad_id, "ad_id")
    plan_name = _clean_text(_first_direct(plan_row, "name", "ad_name"))

    raw_goal = _first_direct(plan_row, "marketing_goal", "marketingGoal")
    raw_scene = _first_direct(plan_row, "adlab_scene", "adlabScene")
    payload_goal = _normalize_marketing_goal(raw_goal)
    payload_scene = _normalize_adlab_scene(raw_scene)
    expected_goal = _normalize_marketing_goal(expected_marketing_goal)
    expected_scene = _normalize_adlab_scene(expected_adlab_scene)

    conflict_reasons: list[str] = []
    if expected_goal and payload_goal and payload_goal != expected_goal:
        conflict_reasons.append(f"marketing_goal:{payload_goal}!={expected_goal}")
    if expected_scene and payload_scene and payload_scene != expected_scene:
        conflict_reasons.append(f"adlab_scene:{payload_scene}!={expected_scene}")

    if conflict_reasons:
        classification_status = "CONFLICT"
        classification_reason = ";".join(conflict_reasons)
    elif payload_goal and payload_scene:
        classification_status = "VERIFIED"
        classification_reason = "platform_payload"
    elif expected_goal and expected_scene:
        classification_status = "QUERY_ONLY"
        classification_reason = "catalog_query_constraint_requires_detail_verification"
    else:
        classification_status = "UNKNOWN"
        classification_reason = "missing_classification_fields"

    marketing_goal = payload_goal or expected_goal
    adlab_scene = payload_scene or expected_scene

    status = _clean_upper(_first_direct(plan_row, "status", "delivery_status", "ad_status"))
    opt_status = _clean_upper(_first_direct(plan_row, "opt_status", "optStatus"))
    modify_time = _clean_text(_first_direct(plan_row, "modify_time", "modifyTime", "update_time"))

    delivery_setting = plan_row.get("delivery_setting")
    if not isinstance(delivery_setting, Mapping):
        delivery_setting = {}
    budget_value = _first_direct(delivery_setting, "budget")
    if budget_value in (None, ""):
        budget_value = _first_direct(plan_row, "budget")

    return NormalizedPlan(
        advertiser_id=aid,
        ad_id=ad_id,
        plan_name=plan_name,
        marketing_goal=marketing_goal,
        adlab_scene=adlab_scene,
        official_status=status,
        official_opt_status=opt_status,
        modify_time=modify_time,
        budget_decimal=_decimal_text(budget_value),
        classification_status=classification_status,
        classification_reason=classification_reason,
    )
