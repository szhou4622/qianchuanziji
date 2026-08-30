"""千川官方 Open API 错误模型。"""
from __future__ import annotations


class OpenApiError(RuntimeError):
    def __init__(self, message: str, *, code: str = "", request_id: str = "", retryable: bool = False) -> None:
        super().__init__(message)
        self.code = str(code or "")
        self.request_id = str(request_id or "")
        self.retryable = bool(retryable)


class OpenApiNetworkError(OpenApiError):
    pass


class OpenApiRateLimitError(OpenApiError):
    pass


class OpenApiTokenError(OpenApiError):
    pass


class OpenApiPermissionError(OpenApiError):
    pass


class OpenApiContractError(OpenApiError):
    pass


class OpenApiResponseError(OpenApiError):
    pass
