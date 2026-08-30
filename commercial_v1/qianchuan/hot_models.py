"""Phase 3 素材/调控任务热采集规范化模型。

只读取已经确认的官方字段路径；不递归猜 ID，不把缺失值补成 0，不做未经证明的单位换算。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .errors import OpenApiContractError
from .normalizers import require_digit_id, text_id

MATERIAL_METRIC_FIELDS = (
    "stat_cost_for_roi2",
    "total_order_settle_amount_for_roi2_1h",
    "total_prepay_and_pay_settle_roi2_1h",
    "total_order_settle_count_for_roi2_1h",
    "total_pay_order_count_for_roi2",
    "total_pay_order_gmv_include_coupon_for_roi2",
    "total_prepay_and_pay_order_roi2",
)

CONTROL_METRIC_FIELDS = (
    "stat_cost_for_roi2_assist",
    "total_pay_order_count_for_roi2_assist",
    "total_pay_order_gmv_include_coupon_for_roi2_assist",
    "total_prepay_and_pay_order_roi2_assist",
    "total_order_settle_amount_for_roi2_1h_assist",
    "total_prepay_and_pay_settle_roi2_1h_assist",
    "total_order_settle_count_for_roi2_1h_assist",
)


def decimal_text(value: Any, *, field: str) -> str | None:
    """保留官方原单位 Decimal；缺失保持 None。"""
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise OpenApiContractError(
            f"{field} is not a valid decimal",
            code="INVALID_DECIMAL_METRIC",
        ) from exc
    if not number.is_finite():
        raise OpenApiContractError(
            f"{field} is not finite",
            code="INVALID_DECIMAL_METRIC",
        )
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def integer_value(value: Any, *, field: str) -> int | None:
    """数量缺失保持 None；只接受数学意义上的整数。"""
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise OpenApiContractError(
            f"{field} is not a valid integer",
            code="INVALID_INTEGER_METRIC",
        ) from exc
    if not number.is_finite() or number != number.to_integral_value():
        raise OpenApiContractError(
            f"{field} is not an integer",
            code="INVALID_INTEGER_METRIC",
        )
    return int(number)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def material_identity(row: Mapping[str, Any]) -> str:
    """只从已确认的素材字段路径取 material_id。"""
    direct = text_id(row.get("material_id"))
    if direct:
        return direct
    info = _mapping(row.get("material_info"))
    direct = text_id(info.get("material_id"))
    if direct:
        return direct
    video = _mapping(info.get("video_material"))
    return text_id(video.get("material_id"))


def control_task_identity(row: Mapping[str, Any]) -> str:
    """官方调控任务原始 ID 字段已确认是 id。"""
    return text_id(row.get("id"))


@dataclass(frozen=True)
class MaterialHotRecord:
    advertiser_id: str
    ad_id: str
    material_id: str
    video_id: str
    title: str
    official_material_status: str
    official_audit_status: str
    overall_cost: str | None
    net_settle_amount: str | None
    net_settle_roi: str | None
    net_settle_order_count: int | None
    overall_order_count: int | None
    overall_gmv: str | None
    overall_pay_roi: str | None


@dataclass(frozen=True)
class ControlTaskHotRecord:
    advertiser_id: str
    ad_id: str
    control_task_id: str
    scene: str
    task_name: str
    official_task_status: str
    budget: str | None
    duration_decimal: str | None
    bid: str | None
    roi_goal: str | None
    create_time: str
    material_ids: tuple[str, ...]
    assist_cost: str | None
    assist_order_count: int | None
    assist_gmv: str | None
    assist_pay_roi: str | None
    assist_net_amount: str | None
    assist_net_roi: str | None
    assist_net_order_count: int | None


def normalize_material_hot(
    row: Mapping[str, Any],
    *,
    advertiser_id: str,
    ad_id: str,
) -> MaterialHotRecord:
    aid = require_digit_id(advertiser_id, "advertiser_id")
    pid = require_digit_id(ad_id, "ad_id")
    material_id = require_digit_id(material_identity(row), "material_id")

    info = _mapping(row.get("material_info"))
    video = _mapping(info.get("video_material"))
    if not video:
        video = _mapping(row.get("video_info"))

    title = str(
        row.get("title")
        or video.get("title")
        or info.get("title")
        or ""
    ).strip()
    video_id = text_id(row.get("video_id") or video.get("video_id"))
    stats = _mapping(row.get("stats_info"))

    return MaterialHotRecord(
        advertiser_id=aid,
        ad_id=pid,
        material_id=material_id,
        video_id=video_id,
        title=title,
        official_material_status=str(row.get("material_status") or "").strip().upper(),
        official_audit_status=str(row.get("audit_status") or "").strip().upper(),
        overall_cost=decimal_text(stats.get("stat_cost_for_roi2"), field="stat_cost_for_roi2"),
        net_settle_amount=decimal_text(
            stats.get("total_order_settle_amount_for_roi2_1h"),
            field="total_order_settle_amount_for_roi2_1h",
        ),
        net_settle_roi=decimal_text(
            stats.get("total_prepay_and_pay_settle_roi2_1h"),
            field="total_prepay_and_pay_settle_roi2_1h",
        ),
        net_settle_order_count=integer_value(
            stats.get("total_order_settle_count_for_roi2_1h"),
            field="total_order_settle_count_for_roi2_1h",
        ),
        overall_order_count=integer_value(
            stats.get("total_pay_order_count_for_roi2"),
            field="total_pay_order_count_for_roi2",
        ),
        overall_gmv=decimal_text(
            stats.get("total_pay_order_gmv_include_coupon_for_roi2"),
            field="total_pay_order_gmv_include_coupon_for_roi2",
        ),
        overall_pay_roi=decimal_text(
            stats.get("total_prepay_and_pay_order_roi2"),
            field="total_prepay_and_pay_order_roi2",
        ),
    )


def _control_material_ids(row: Mapping[str, Any]) -> tuple[str, ...]:
    values = row.get("material_list")
    if not isinstance(values, list):
        return ()
    result: list[str] = []
    for item in values:
        if not isinstance(item, Mapping):
            raise OpenApiContractError(
                "control task material_list contains a non-object",
                code="CONTROL_MATERIAL_LIST_INVALID",
            )
        material_id = require_digit_id(item.get("material_id"), "material_id")
        if material_id not in result:
            result.append(material_id)
    return tuple(sorted(result))


def normalize_control_task_hot(
    row: Mapping[str, Any],
    *,
    advertiser_id: str,
    ad_id: str,
) -> ControlTaskHotRecord:
    aid = require_digit_id(advertiser_id, "advertiser_id")
    pid = require_digit_id(ad_id, "ad_id")
    task_id = require_digit_id(control_task_identity(row), "control_task_id")
    metrics = _mapping(row.get("metrics"))
    if not metrics:
        metrics = _mapping(row.get("stats_info"))

    return ControlTaskHotRecord(
        advertiser_id=aid,
        ad_id=pid,
        control_task_id=task_id,
        scene=str(row.get("scene") or "").strip().upper(),
        task_name=str(row.get("name") or "").strip(),
        official_task_status=str(row.get("task_status") or "").strip().upper(),
        budget=decimal_text(row.get("budget"), field="control_task.budget"),
        duration_decimal=decimal_text(row.get("duration"), field="control_task.duration"),
        bid=decimal_text(row.get("bid"), field="control_task.bid"),
        roi_goal=decimal_text(row.get("roi2_goal"), field="control_task.roi2_goal"),
        create_time=str(row.get("create_time") or "").strip(),
        material_ids=_control_material_ids(row),
        assist_cost=decimal_text(
            metrics.get("stat_cost_for_roi2_assist"),
            field="stat_cost_for_roi2_assist",
        ),
        assist_order_count=integer_value(
            metrics.get("total_pay_order_count_for_roi2_assist"),
            field="total_pay_order_count_for_roi2_assist",
        ),
        assist_gmv=decimal_text(
            metrics.get("total_pay_order_gmv_include_coupon_for_roi2_assist"),
            field="total_pay_order_gmv_include_coupon_for_roi2_assist",
        ),
        assist_pay_roi=decimal_text(
            metrics.get("total_prepay_and_pay_order_roi2_assist"),
            field="total_prepay_and_pay_order_roi2_assist",
        ),
        assist_net_amount=decimal_text(
            metrics.get("total_order_settle_amount_for_roi2_1h_assist"),
            field="total_order_settle_amount_for_roi2_1h_assist",
        ),
        assist_net_roi=decimal_text(
            metrics.get("total_prepay_and_pay_settle_roi2_1h_assist"),
            field="total_prepay_and_pay_settle_roi2_1h_assist",
        ),
        assist_net_order_count=integer_value(
            metrics.get("total_order_settle_count_for_roi2_1h_assist"),
            field="total_order_settle_count_for_roi2_1h_assist",
        ),
    )
