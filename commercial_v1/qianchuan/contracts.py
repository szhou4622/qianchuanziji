"""千川官方 Open API 已封板端点常量。

新增/修改端点前必须先更新正式 API 契约文档，不能由实现层猜测。
"""
from __future__ import annotations

DEFAULT_OPEN_API_BASE_URL = "https://api.oceanengine.com"

OAUTH_ACCESS_TOKEN = "/open_api/oauth2/access_token/"
OAUTH_REFRESH_TOKEN = "/open_api/oauth2/refresh_token/"

OAUTH_ADVERTISER_GET = "/open_api/oauth2/advertiser/get/"
SHOP_ADVERTISER_LIST = "/open_api/v1.0/qianchuan/shop/advertiser/list/"
EBP_ADVERTISER_LIST = "/open_api/2/ebp/advertiser/list/"
ADVERTISER_PUBLIC_INFO = "/open_api/2/advertiser/public_info/"

PLAN_LIST = "/open_api/v1.0/qianchuan/uni_promotion/list/"
PLAN_DETAIL = "/open_api/v1.0/qianchuan/uni_promotion/ad/detail/"

MATERIAL_GET = "/open_api/v1.0/qianchuan/uni_promotion/ad/material/get/"
PRODUCT_GET = "/open_api/v1.0/qianchuan/uni_promotion/ad/product/get/"
REPORT_CONFIG_GET = "/open_api/v1.0/qianchuan/report/uni_promotion/config/get/"
REPORT_DATA_GET = "/open_api/v1.0/qianchuan/report/uni_promotion/data/get/"
CONTROL_TASK_LIST = "/open_api/v1.0/qianchuan/uni_promotion/ad/control_task/list/"
LOG_SEARCH = "/open_api/v1.0/qianchuan/tools/log_search/"

CONTROL_TASK_CREATE = "/open_api/v1.0/qianchuan/uni_promotion/ad/control_task/create/"
CONTROL_TASK_STATUS_UPDATE = "/open_api/v1.0/qianchuan/uni_promotion/ad/control_task/status/update/"
CONTROL_TASK_BUDGET_UPDATE = "/open_api/v1.0/qianchuan/uni_promotion/ad/control_task/budget/update/"
CONTROL_TASK_DURATION_UPDATE = "/open_api/v1.0/qianchuan/uni_promotion/ad/control_task/duration/update/"

PHASE2_OAUTH_POST_ENDPOINTS = frozenset({OAUTH_ACCESS_TOKEN, OAUTH_REFRESH_TOKEN})

# Kept under the historical name for compatibility with Phase 2 tests/imports.
# Phase 3 extends the same read-only client gate with material/control GET only.
PHASE2_GET_ENDPOINTS = frozenset(
    {
        OAUTH_ADVERTISER_GET,
        SHOP_ADVERTISER_LIST,
        EBP_ADVERTISER_LIST,
        ADVERTISER_PUBLIC_INFO,
        PLAN_LIST,
        PLAN_DETAIL,
        MATERIAL_GET,
        CONTROL_TASK_LIST,
    }
)
READ_ONLY_GET_ENDPOINTS = PHASE2_GET_ENDPOINTS

ALL_OFFICIAL_ENDPOINTS = frozenset(
    {
        OAUTH_ACCESS_TOKEN,
        OAUTH_REFRESH_TOKEN,
        OAUTH_ADVERTISER_GET,
        SHOP_ADVERTISER_LIST,
        EBP_ADVERTISER_LIST,
        ADVERTISER_PUBLIC_INFO,
        PLAN_LIST,
        PLAN_DETAIL,
        MATERIAL_GET,
        PRODUCT_GET,
        REPORT_CONFIG_GET,
        REPORT_DATA_GET,
        CONTROL_TASK_LIST,
        LOG_SEARCH,
        CONTROL_TASK_CREATE,
        CONTROL_TASK_STATUS_UPDATE,
        CONTROL_TASK_BUDGET_UPDATE,
        CONTROL_TASK_DURATION_UPDATE,
    }
)

RATE_LIMIT_CODES = frozenset({"40100", "40110", "40130"})
