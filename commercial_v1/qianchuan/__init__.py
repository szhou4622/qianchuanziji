"""千川官方 Open API 适配层。"""

from .accounts import AccountDiscoveryResult, AccountDiscoveryService, AccountStore
from .client import ApiResponse, EndpointRateLimiter, OpenApiClient, TransportResponse
from .errors import (
    OpenApiContractError,
    OpenApiError,
    OpenApiNetworkError,
    OpenApiPermissionError,
    OpenApiRateLimitError,
    OpenApiResponseError,
    OpenApiTokenError,
)
from .normalizers import FinalAdvertiser, NormalizedPlan, OAuthSubject
from .plans import (
    FOUR_PLAN_CLASSES,
    MonitorPlanStore,
    PlanCatalogResult,
    PlanCatalogService,
    PlanMonitorService,
)
from .scheduler import PLAN_STATUS_CHECK, PlanStateCheckHandler, PlanStateScheduler
from .token_provider import OAuthTokenProvider, TokenBundle, WindowsDpapiProtector

__all__ = [
    "AccountDiscoveryResult",
    "AccountDiscoveryService",
    "AccountStore",
    "ApiResponse",
    "EndpointRateLimiter",
    "OpenApiClient",
    "TransportResponse",
    "OpenApiContractError",
    "OpenApiError",
    "OpenApiNetworkError",
    "OpenApiPermissionError",
    "OpenApiRateLimitError",
    "OpenApiResponseError",
    "OpenApiTokenError",
    "OAuthSubject",
    "FinalAdvertiser",
    "NormalizedPlan",
    "FOUR_PLAN_CLASSES",
    "MonitorPlanStore",
    "PlanCatalogResult",
    "PlanCatalogService",
    "PlanMonitorService",
    "PLAN_STATUS_CHECK",
    "PlanStateCheckHandler",
    "PlanStateScheduler",
    "OAuthTokenProvider",
    "TokenBundle",
    "WindowsDpapiProtector",
]
