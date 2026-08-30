import json
from urllib.parse import parse_qs

import pytest

from commercial_v1.qianchuan.client import OpenApiClient, TransportResponse
from commercial_v1.qianchuan.contracts import (
    CONTROL_TASK_CREATE,
    EBP_ADVERTISER_LIST,
    OAUTH_ACCESS_TOKEN,
    PLAN_LIST,
)
from commercial_v1.qianchuan.errors import (
    OpenApiContractError,
    OpenApiRateLimitError,
    OpenApiTokenError,
)


class NoWaitLimiter:
    def wait(self, endpoint: str, advertiser_id: str = "") -> None:
        return None


def _response(payload: dict, *, status: int = 200, headers: dict[str, str] | None = None) -> TransportResponse:
    return TransportResponse(status, headers or {}, json.dumps(payload).encode("utf-8"))


def test_get_accepts_phase2_endpoint_and_extracts_request_id() -> None:
    seen = []

    def transport(method, url, headers, body, timeout):
        seen.append((method, url, dict(headers), body))
        return _response({"code": 0, "data": {"list": []}, "request_id": "rid-1"})

    client = OpenApiClient(transport=transport, rate_limiter=NoWaitLimiter())  # type: ignore[arg-type]
    result = client.get(
        EBP_ADVERTISER_LIST,
        query={"account_type": "QIANCHUAN"},
        access_token="token-value",
    )
    assert result.code == "0"
    assert result.request_id == "rid-1"
    assert seen[0][0] == "GET"
    assert "account_type=QIANCHUAN" in seen[0][1]
    assert seen[0][2]["Access-Token"] == "token-value"


def test_get_rejects_endpoint_outside_phase2_scope() -> None:
    client = OpenApiClient(transport=lambda *_args: _response({"code": 0}), rate_limiter=NoWaitLimiter())  # type: ignore[arg-type]
    with pytest.raises(OpenApiContractError, match="outside Phase 2 GET scope"):
        client.get(CONTROL_TASK_CREATE, access_token="token")


def test_phase2_rejects_business_post_before_transport() -> None:
    called = 0

    def transport(*_args):
        nonlocal called
        called += 1
        return _response({"code": 0})

    client = OpenApiClient(transport=transport, rate_limiter=NoWaitLimiter())  # type: ignore[arg-type]
    with pytest.raises(OpenApiContractError, match="forbids platform business POST"):
        client.post_oauth(CONTROL_TASK_CREATE, {"anything": 1})
    assert called == 0


def test_oauth_post_uses_form_encoding_and_no_access_token_header() -> None:
    seen = []

    def transport(method, url, headers, body, timeout):
        seen.append((method, url, dict(headers), parse_qs(body.decode("utf-8"))))
        return _response({"code": 0, "data": {"access_token": "new-token"}})

    client = OpenApiClient(transport=transport, rate_limiter=NoWaitLimiter())  # type: ignore[arg-type]
    result = client.post_oauth(OAUTH_ACCESS_TOKEN, {"app_id": 1, "secret": "x", "auth_code": "y"})
    assert result.data["access_token"] == "new-token"
    assert seen[0][0] == "POST"
    assert seen[0][2]["Content-Type"] == "application/x-www-form-urlencoded"
    assert "Access-Token" not in seen[0][2]
    assert seen[0][3] == {"app_id": ["1"], "secret": ["x"], "auth_code": ["y"]}


def test_rate_limit_business_code_retries_get_boundedly() -> None:
    attempts = 0
    sleeps = []

    def transport(*_args):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return _response({"code": 40130, "message": "rate limited", "request_id": f"r{attempts}"})
        return _response({"code": 0, "data": {"ok": True}, "request_id": "r3"})

    client = OpenApiClient(
        transport=transport,
        rate_limiter=NoWaitLimiter(),  # type: ignore[arg-type]
        max_get_attempts=4,
        sleep=sleeps.append,
        random_fn=lambda: 0,
    )
    result = client.get(PLAN_LIST, access_token="token", advertiser_id="123")
    assert result.data == {"ok": True}
    assert attempts == 3
    assert sleeps == [0.5, 1.0]


def test_rate_limit_stops_after_max_get_attempts() -> None:
    attempts = 0

    def transport(*_args):
        nonlocal attempts
        attempts += 1
        return _response({"code": 40100, "message": "rate limited"})

    client = OpenApiClient(
        transport=transport,
        rate_limiter=NoWaitLimiter(),  # type: ignore[arg-type]
        max_get_attempts=2,
        sleep=lambda _seconds: None,
        random_fn=lambda: 0,
    )
    with pytest.raises(OpenApiRateLimitError):
        client.get(PLAN_LIST, access_token="token")
    assert attempts == 2


def test_token_business_error_is_not_retried() -> None:
    attempts = 0

    def transport(*_args):
        nonlocal attempts
        attempts += 1
        return _response({"code": 40001, "message": "access_token 已过期"})

    client = OpenApiClient(
        transport=transport,
        rate_limiter=NoWaitLimiter(),  # type: ignore[arg-type]
        max_get_attempts=4,
        sleep=lambda _seconds: None,
    )
    with pytest.raises(OpenApiTokenError):
        client.get(PLAN_LIST, access_token="old-token")
    assert attempts == 1


def test_audit_does_not_contain_access_token() -> None:
    audit = []

    def transport(method, url, headers, body, timeout):
        return _response({"code": 0, "data": {}}, headers={"X-Request-Id": "rid"})

    client = OpenApiClient(
        transport=transport,
        rate_limiter=NoWaitLimiter(),  # type: ignore[arg-type]
        audit_sink=audit.append,
    )
    client.get(PLAN_LIST, access_token="super-secret-token")
    rendered = json.dumps(audit, ensure_ascii=False)
    assert "super-secret-token" not in rendered
    assert audit[0]["request_id"] == "rid"
