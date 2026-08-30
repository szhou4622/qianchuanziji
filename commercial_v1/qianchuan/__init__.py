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
from .hot_collection import CollectionResult, HotCollectionService
from .hot_confirmation import (
    CONTROL_CONFIRM,
    MATERIAL_CONFIRM,
    ConfirmationResult,
    HotConfirmationHandler,
    HotConfirmationScheduler,
    HotConfirmationService,
)
from .hot_models import (
    CONTROL_METRIC_FIELDS,
    MATERIAL_METRIC_FIELDS,
    ControlTaskHotRecord,
    MaterialHotRecord,
)
from .hot_scheduler import (
    CONTROL_5M,
    MATERIAL_5M,
    HotCollectionHandler,
    HotCollectionScheduler,
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
    "MATERIAL_METRIC_FIELDS",
    "CONTROL_METRIC_FIELDS",
    "MaterialHotRecord",
    "ControlTaskHotRecord",
    "CollectionResult",
    "HotCollectionService",
    "MATERIAL_5M",
    "CONTROL_5M",
    "HotCollectionHandler",
    "HotCollectionScheduler",
    "MATERIAL_CONFIRM",
    "CONTROL_CONFIRM",
    "ConfirmationResult",
    "HotConfirmationService",
    "HotConfirmationHandler",
    "HotConfirmationScheduler",
    "OAuthTokenProvider",
    "TokenBundle",
    "WindowsDpapiProtector",
]
