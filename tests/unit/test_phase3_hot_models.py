import pytest

from commercial_v1.qianchuan.errors import OpenApiContractError
from commercial_v1.qianchuan.hot_models import (
    CONTROL_METRIC_FIELDS,
    MATERIAL_METRIC_FIELDS,
    normalize_control_task_hot,
    normalize_material_hot,
)


def test_material_metrics_keep_null_and_decimal_text() -> None:
    row = {
        "material_id": "900001",
        "video_id": "800001",
        "title": "素材A",
        "material_status": "DELIVERY_OK",
        "audit_status": "PASS",
        "stats_info": {
            "stat_cost_for_roi2": "123.4500",
            "total_order_settle_amount_for_roi2_1h": None,
            "total_prepay_and_pay_settle_roi2_1h": "2.5000",
            "total_order_settle_count_for_roi2_1h": "3",
            "total_pay_order_count_for_roi2": 4,
            "total_pay_order_gmv_include_coupon_for_roi2": "300.00",
            "total_prepay_and_pay_order_roi2": "2.43",
        },
    }
    record = normalize_material_hot(row, advertiser_id="111111", ad_id="222222")
    assert record.overall_cost == "123.45"
    assert record.net_settle_amount is None
    assert record.net_settle_roi == "2.5"
    assert record.net_settle_order_count == 3
    assert record.overall_order_count == 4
    assert record.overall_gmv == "300"
    assert record.overall_pay_roi == "2.43"
    assert len(MATERIAL_METRIC_FIELDS) == 7


def test_control_task_uses_raw_id_and_all_seven_metrics() -> None:
    row = {
        "id": "700001",
        "name": "追投1",
        "scene": "MATERIAL_ADD_BUDGET",
        "task_status": "PROCESSING",
        "budget": "500.00",
        "duration": "2.0",
        "material_list": [{"material_id": "900001"}, {"material_id": "900002"}],
        "metrics": {
            "stat_cost_for_roi2_assist": "10.50",
            "total_pay_order_count_for_roi2_assist": "2",
            "total_pay_order_gmv_include_coupon_for_roi2_assist": "40",
            "total_prepay_and_pay_order_roi2_assist": "3.8095",
            "total_order_settle_amount_for_roi2_1h_assist": "30",
            "total_prepay_and_pay_settle_roi2_1h_assist": "2.8571",
            "total_order_settle_count_for_roi2_1h_assist": "1",
        },
    }
    record = normalize_control_task_hot(row, advertiser_id="111111", ad_id="222222")
    assert record.control_task_id == "700001"
    assert record.budget == "500"
    assert record.duration_decimal == "2"
    assert record.material_ids == ("900001", "900002")
    assert record.assist_cost == "10.5"
    assert record.assist_order_count == 2
    assert record.assist_net_order_count == 1
    assert len(CONTROL_METRIC_FIELDS) == 7


def test_control_task_never_falls_back_to_task_id_alias() -> None:
    with pytest.raises(OpenApiContractError):
        normalize_control_task_hot(
            {"task_id": "700001", "task_status": "PROCESSING"},
            advertiser_id="111111",
            ad_id="222222",
        )


def test_non_integral_count_is_not_silently_rounded() -> None:
    with pytest.raises(OpenApiContractError, match="integer"):
        normalize_material_hot(
            {
                "material_id": "900001",
                "material_status": "DELIVERY_OK",
                "stats_info": {"total_pay_order_count_for_roi2": "1.5"},
            },
            advertiser_id="111111",
            ad_id="222222",
        )
