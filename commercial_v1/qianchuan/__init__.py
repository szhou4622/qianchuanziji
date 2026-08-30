"""千川官方 Open API 适配层。"""

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
from .token_provider import OAuthTokenProvider, TokenBundle, WindowsDpapiProtector

__all__ = [
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
    "OAuthTokenProvider",
    "TokenBundle",
    "WindowsDpapiProtector",
]
