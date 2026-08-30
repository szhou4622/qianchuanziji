from __future__ import annotations

from dataclasses import dataclass

import pytest

from commercial_v1.qianchuan.client import ApiResponse
from commercial_v1.qianchuan.errors import OpenApiResponseError, OpenApiTokenError
from commercial_v1.qianchuan.pagination import get_all_pages


@dataclass
class ScriptedClient:
    script: list[object]

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def get(self, endpoint, *, query=None, access_token, advertiser_id=""):
        page = int((query or {})["page"])
        self.calls.append((str(access_token), page))
        if not self.script:
            raise AssertionError("unexpected request")
        result = self.script.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _response(items, *, total, request_id, page_size=100, has_more=None):
    page_info = {"page_size": page_size, "total_number": total}
    if has_more is not None:
        page_info["has_more"] = bool(has_more)
    return ApiResponse(
        data={"items": list(items), "page_info": page_info},
        raw={},
        request_id=request_id,
        code="0",
        message="",
        local_request_uid=f"local-{request_id}",
    )


def test_material_filter_mismatch_fails_closed() -> None:
    client = ScriptedClient(
        [
            _response(
                [{"material_id": "900001", "material_status": "DELIVERY_NOT"}],
                total=1,
                request_id="rid-1",
            )
        ]
    )

    with pytest.raises(OpenApiResponseError) as captured:
        get_all_pages(
            client,  # type: ignore[arg-type]
            "/open_api/v1.0/qianchuan/uni_promotion/ad/material/get/",
            query={"filtering": {"material_status": "DELIVERY_OK"}},
            access_token="old",
            advertiser_id="111111",
            page_size=100,
            identity_getter=lambda row: row.get("material_id"),
        )

    assert captured.value.code == "MATERIAL_ACTIVE_FILTER_MISMATCH"
    assert captured.value.request_id == "rid-1"


def test_control_status_and_scene_filters_fail_closed() -> None:
    status_client = ScriptedClient(
        [
            _response(
                [{"id": "700001", "task_status": "DISABLE", "scene": "MATERIAL_ADD_BUDGET"}],
                total=1,
                request_id="rid-status",
            )
        ]
    )
    with pytest.raises(OpenApiResponseError) as captured:
        get_all_pages(
            status_client,  # type: ignore[arg-type]
            "/open_api/v1.0/qianchuan/uni_promotion/ad/control_task/list/",
            query={
                "scene": "MATERIAL_ADD_BUDGET",
                "filtering": {"task_status": "PROCESSING"},
            },
            access_token="old",
            advertiser_id="111111",
            page_size=100,
            identity_getter=lambda row: row.get("id"),
        )
    assert captured.value.code == "CONTROL_ACTIVE_FILTER_MISMATCH"

    scene_client = ScriptedClient(
        [
            _response(
                [{"id": "700001", "task_status": "PROCESSING", "scene": "OTHER_SCENE"}],
                total=1,
                request_id="rid-scene",
            )
        ]
    )
    with pytest.raises(OpenApiResponseError) as captured:
        get_all_pages(
            scene_client,  # type: ignore[arg-type]
            "/open_api/v1.0/qianchuan/uni_promotion/ad/control_task/list/",
            query={
                "scene": "MATERIAL_ADD_BUDGET",
                "filtering": {"task_status": "PROCESSING"},
            },
            access_token="old",
            advertiser_id="111111",
            page_size=100,
            identity_getter=lambda row: row.get("id"),
        )
    assert captured.value.code == "CONTROL_SCENE_FILTER_MISMATCH"


def test_token_refresh_restarts_whole_pagination_from_page_one() -> None:
    client = ScriptedClient(
        [
            _response(
                [{"id": "old-1"}],
                total=2,
                request_id="old-rid-1",
                page_size=1,
                has_more=True,
            ),
            OpenApiTokenError("expired", code="TOKEN_EXPIRED"),
            _response(
                [{"id": "fresh-1"}],
                total=2,
                request_id="new-rid-1",
                page_size=1,
                has_more=True,
            ),
            _response(
                [{"id": "fresh-2"}],
                total=2,
                request_id="new-rid-2",
                page_size=1,
                has_more=False,
            ),
        ]
    )
    refresh_calls = 0

    def refresh() -> str:
        nonlocal refresh_calls
        refresh_calls += 1
        return "new"

    rows, request_ids = get_all_pages(
        client,  # type: ignore[arg-type]
        "/open_api/example/list/",
        query={},
        access_token="old",
        advertiser_id="111111",
        page_size=1,
        identity_getter=lambda row: row.get("id"),
        refresh_access_token=refresh,
    )

    assert refresh_calls == 1
    assert client.calls == [("old", 1), ("old", 2), ("new", 1), ("new", 2)]
    assert [row["id"] for row in rows] == ["fresh-1", "fresh-2"]
    assert request_ids == ["new-rid-1", "new-rid-2"]
