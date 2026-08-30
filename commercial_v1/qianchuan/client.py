"""Phase 2 千川 Open API HTTP 客户端。

本阶段只允许：
- 六个账户/计划 GET；
- 两个 OAuth Token POST。

任何千川投放业务 POST 都在客户端入口直接拒绝。
"""
from __future__ import annotations

import json
import random
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from commercial_v1.security.redaction import redact, sanitize_text

from .contracts import (
    DEFAULT_OPEN_API_BASE_URL,
    PHASE2_GET_ENDPOINTS,
    PHASE2_OAUTH_POST_ENDPOINTS,
    RATE_LIMIT_CODES,
)
from .errors import (
    OpenApiContractError,
    OpenApiError,
    OpenApiNetworkError,
    OpenApiPermissionError,
    OpenApiRateLimitError,
    OpenApiResponseError,
    OpenApiTokenError,
)


@dataclass(frozen=True)
class TransportResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class ApiResponse:
    data: Any
    raw: Mapping[str, Any]
    request_id: str
    code: str
    message: str
    local_request_uid: str


Transport = Callable[[str, str, Mapping[str, str], bytes | None, float], TransportResponse]
AuditSink = Callable[[Mapping[str, Any]], None]
SleepFn = Callable[[float], None]
MonotonicFn = Callable[[], float]


def default_transport(method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float) -> TransportResponse:
    request = Request(url=url, data=body, headers=dict(headers), method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return TransportResponse(
                status=int(response.status),
                headers={str(k): str(v) for k, v in response.headers.items()},
                body=response.read(),
            )
    except HTTPError as exc:
        return TransportResponse(
            status=int(exc.code),
            headers={str(k): str(v) for k, v in (exc.headers.items() if exc.headers else [])},
            body=exc.read() if hasattr(exc, "read") else b"",
        )
    except (URLError, TimeoutError, socket.timeout, OSError) as exc:
        raise OpenApiNetworkError(sanitize_text(f"{type(exc).__name__}: {exc}"), retryable=True) from exc


class EndpointRateLimiter:
    """本地保守限频，不把这些数值宣称为官方最终额度。"""

    def __init__(
        self,
        *,
        application_qps: float = 12.0,
        advertiser_qps: float = 2.0,
        endpoint_qps: float = 4.0,
        monotonic: MonotonicFn = time.monotonic,
        sleep: SleepFn = time.sleep,
    ) -> None:
        self._app_interval = 1.0 / max(0.1, float(application_qps))
        self._advertiser_interval = 1.0 / max(0.1, float(advertiser_qps))
        self._endpoint_interval = 1.0 / max(0.1, float(endpoint_qps))
        self._monotonic = monotonic
        self._sleep = sleep
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, endpoint: str, advertiser_id: str = "") -> None:
        lanes: list[tuple[str, float]] = [
            ("application", self._app_interval),
            (f"endpoint:{endpoint}", self._endpoint_interval),
        ]
        account = str(advertiser_id or "").strip()
        if account:
            lanes.append((f"advertiser:{account}", self._advertiser_interval))
        with self._lock:
            now = self._monotonic()
            due = max((self._next.get(key, now) for key, _ in lanes), default=now)
            delay = max(0.0, due - now)
            for key, interval in lanes:
                self._next[key] = due + interval
        if delay:
            self._sleep(delay)


class OpenApiClient:
    def __init__(
        self,
        *,
        base_url: str = DEFAULT_OPEN_API_BASE_URL,
        timeout: float = 30.0,
        max_get_attempts: int = 4,
        transport: Transport = default_transport,
        rate_limiter: EndpointRateLimiter | None = None,
        audit_sink: AuditSink | None = None,
        sleep: SleepFn = time.sleep,
        random_fn: Callable[[], float] = random.random,
    ) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.timeout = max(1.0, float(timeout))
        self.max_get_attempts = max(1, int(max_get_attempts))
        self._transport = transport
        self._rate_limiter = rate_limiter or EndpointRateLimiter()
        self._audit_sink = audit_sink
        self._sleep = sleep
        self._random = random_fn

    def get(
        self,
        endpoint: str,
        *,
        query: Mapping[str, Any] | None = None,
        access_token: str,
        advertiser_id: str = "",
    ) -> ApiResponse:
        self._require_phase2_get(endpoint)
        if not str(access_token or "").strip():
            raise OpenApiTokenError("missing access token", code="LOCAL_TOKEN_MISSING")

        last_error: Exception | None = None
        for attempt in range(1, self.max_get_attempts + 1):
            local_request_uid = str(uuid.uuid4())
            self._rate_limiter.wait(endpoint, advertiser_id)
            try:
                response = self._send(
                    "GET",
                    endpoint,
                    query=query,
                    headers={"Access-Token": access_token, "Accept": "application/json"},
                    body=None,
                    local_request_uid=local_request_uid,
                )
                if response.code == "0":
                    return response
                error = self._business_error(response)
                if isinstance(error, OpenApiRateLimitError) and attempt < self.max_get_attempts:
                    last_error = error
                    self._sleep(self._retry_delay(attempt))
                    continue
                raise error
            except OpenApiNetworkError as exc:
                last_error = exc
                if attempt >= self.max_get_attempts:
                    raise
                self._sleep(self._retry_delay(attempt))
            except OpenApiResponseError as exc:
                last_error = exc
                if not exc.retryable or attempt >= self.max_get_attempts:
                    raise
                self._sleep(self._retry_delay(attempt))
        assert last_error is not None
        raise last_error

    def post_oauth(self, endpoint: str, payload: Mapping[str, Any]) -> ApiResponse:
        """发送官方 OAuth token 请求。

        access_token / refresh_token 两个官方端点使用
        ``application/x-www-form-urlencoded``。该入口拒绝任何投放业务 POST。
        OAuth POST 不做客户端自动重试，避免一次性 auth_code 被重复消费。
        """
        if endpoint not in PHASE2_OAUTH_POST_ENDPOINTS:
            raise OpenApiContractError(
                "Phase 2 forbids platform business POST endpoints",
                code="PHASE2_POST_FORBIDDEN",
            )
        local_request_uid = str(uuid.uuid4())
        form_pairs = {
            str(key): str(value)
            for key, value in payload.items()
            if value is not None and value != ""
        }
        body = urlencode(form_pairs).encode("utf-8")
        response = self._send(
            "POST",
            endpoint,
            query=None,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            body=body,
            local_request_uid=local_request_uid,
        )
        if response.code != "0":
            raise self._business_error(response)
        return response

    def _send(
        self,
        method: str,
        endpoint: str,
        *,
        query: Mapping[str, Any] | None,
        headers: Mapping[str, str],
        body: bytes | None,
        local_request_uid: str,
    ) -> ApiResponse:
        url = self._url(endpoint, query)
        started = time.monotonic()
        try:
            transport_response = self._transport(method, url, headers, body, self.timeout)
        except OpenApiNetworkError:
            self._audit(
                {
                    "local_request_uid": local_request_uid,
                    "method": method,
                    "endpoint": endpoint,
                    "outcome": "NETWORK_ERROR",
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                }
            )
            raise

        payload = self._decode(transport_response.body)
        request_id = self._request_id(payload, transport_response.headers)
        code = str(payload.get("code", ""))
        message = str(payload.get("message") or payload.get("msg") or "")

        if transport_response.status == 429:
            error = OpenApiRateLimitError(
                sanitize_text(message or "HTTP 429"),
                code=code or "HTTP_429",
                request_id=request_id,
                retryable=True,
            )
            self._audit_response(local_request_uid, method, endpoint, transport_response.status, code, request_id, "RATE_LIMIT", started)
            raise error
        if transport_response.status >= 500:
            error = OpenApiResponseError(
                sanitize_text(message or f"HTTP {transport_response.status}"),
                code=code or f"HTTP_{transport_response.status}",
                request_id=request_id,
                retryable=True,
            )
            self._audit_response(local_request_uid, method, endpoint, transport_response.status, code, request_id, "SERVER_ERROR", started)
            raise error
        if transport_response.status >= 400:
            error = OpenApiResponseError(
                sanitize_text(message or f"HTTP {transport_response.status}"),
                code=code or f"HTTP_{transport_response.status}",
                request_id=request_id,
                retryable=False,
            )
            self._audit_response(local_request_uid, method, endpoint, transport_response.status, code, request_id, "HTTP_ERROR", started)
            raise error

        response = ApiResponse(
            data=payload.get("data"),
            raw=payload,
            request_id=request_id,
            code=code,
            message=message,
            local_request_uid=local_request_uid,
        )
        self._audit_response(local_request_uid, method, endpoint, transport_response.status, code, request_id, "SUCCESS" if code == "0" else "BUSINESS_ERROR", started)
        return response

    @staticmethod
    def _decode(body: bytes) -> Mapping[str, Any]:
        try:
            value = json.loads(body.decode("utf-8-sig")) if body else {}
        except Exception as exc:
            raise OpenApiResponseError("official API returned invalid JSON", code="INVALID_JSON", retryable=False) from exc
        if not isinstance(value, Mapping):
            raise OpenApiResponseError("official API response is not an object", code="INVALID_RESPONSE", retryable=False)
        return value

    def _url(self, endpoint: str, query: Mapping[str, Any] | None) -> str:
        if not endpoint.startswith("/open_api/"):
            raise OpenApiContractError("endpoint must start with /open_api/", code="INVALID_ENDPOINT")
        pairs: list[tuple[str, str]] = []
        for key, value in (query or {}).items():
            if value is None or value == "":
                continue
            if isinstance(value, (list, tuple, dict)):
                rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            else:
                rendered = str(value)
            pairs.append((str(key), rendered))
        return self.base_url + endpoint + ("?" + urlencode(pairs) if pairs else "")

    @staticmethod
    def _request_id(payload: Mapping[str, Any], headers: Mapping[str, str]) -> str:
        for key in ("request_id", "requestId", "log_id", "logId"):
            if payload.get(key):
                return str(payload[key])
        data = payload.get("data")
        if isinstance(data, Mapping):
            for key in ("request_id", "requestId", "log_id", "logId"):
                if data.get(key):
                    return str(data[key])
        normalized = {str(k).lower(): v for k, v in headers.items()}
        for key in ("x-tt-logid", "x-request-id"):
            if normalized.get(key):
                return str(normalized[key])
        return ""

    @staticmethod
    def _business_error(response: ApiResponse) -> OpenApiError:
        code = str(response.code or "")
        message = sanitize_text(response.message or "official API business error")
        lowered = message.lower()
        if code in RATE_LIMIT_CODES:
            return OpenApiRateLimitError(message, code=code, request_id=response.request_id, retryable=True)
        if any(term in lowered for term in ("access_token", "access token", "token expired", "token过期", "token失效")):
            return OpenApiTokenError(message, code=code, request_id=response.request_id, retryable=False)
        if any(term in lowered for term in ("permission", "无权限", "权限未开通", "not authorized", "unauthorized")):
            return OpenApiPermissionError(message, code=code, request_id=response.request_id, retryable=False)
        return OpenApiResponseError(message, code=code, request_id=response.request_id, retryable=False)

    def _retry_delay(self, attempt: int) -> float:
        return min(8.0, 0.5 * (2 ** max(0, attempt - 1))) + self._random() * 0.25

    @staticmethod
    def _require_phase2_get(endpoint: str) -> None:
        if endpoint not in PHASE2_GET_ENDPOINTS:
            raise OpenApiContractError(
                f"endpoint is outside Phase 2 GET scope: {endpoint}",
                code="PHASE2_GET_FORBIDDEN",
            )

    def _audit_response(self, local_uid: str, method: str, endpoint: str, http_status: int, code: str, request_id: str, outcome: str, started: float) -> None:
        self._audit(
            {
                "local_request_uid": local_uid,
                "method": method,
                "endpoint": endpoint,
                "http_status": http_status,
                "api_code": code,
                "request_id": request_id,
                "outcome": outcome,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            }
        )

    def _audit(self, event: Mapping[str, Any]) -> None:
        if self._audit_sink is not None:
            self._audit_sink(redact(dict(event)))
