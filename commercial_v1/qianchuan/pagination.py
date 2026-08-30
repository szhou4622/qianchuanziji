"""千川 GET 分页完整性与过滤契约校验。

分页是“数据可信度边界”，不是便利函数。只要无法证明页数/总数完整，或官方响应违反
本次请求已经明确声明的活动状态过滤条件，就返回错误；上层必须保留已有可信数据，不能
把半页、错状态或语义不一致的结果覆盖到本地状态。

若平台明确返回 Token 失效，整次分页最多强制刷新一次，并从第一页重新读取。禁止把
刷新前已经读取的页与刷新后的页拼接成一个批次。
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from .client import ApiResponse, OpenApiClient
from .errors import OpenApiResponseError, OpenApiTokenError


PRIMARY_LIST_KEYS = (
    "adv_id_list",
    "account_list",
    "advertiser_list",
    "ad_list",
    "ad_material_infos",
    "material_list",
    "product_list",
    "task_list",
    "log_list",
    "logs",
    "advertisers",
    "data_list",
    "items",
    "rows",
    "list",
)


def extract_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [dict(item) for item in data if isinstance(item, Mapping)]
    if not isinstance(data, Mapping):
        return []
    for key in PRIMARY_LIST_KEYS:
        value = data.get(key)
        if isinstance(value, list):
            rows = [dict(item) for item in value if isinstance(item, Mapping)]
            if rows or not value:
                return rows
    return []


def _pagination_info(data: Any) -> Mapping[str, Any] | None:
    if not isinstance(data, Mapping):
        return None
    for key in ("page_info", "pageInfo", "pagination"):
        value = data.get(key)
        if isinstance(value, Mapping):
            return value
    return None


def _int_value(info: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = info.get(key)
        if value not in (None, ""):
            try:
                return int(value)
            except (TypeError, ValueError) as exc:
                raise OpenApiResponseError(
                    f"invalid pagination integer: {key}",
                    code="PAGINATION_INVALID_METADATA",
                    retryable=False,
                ) from exc
    return None


def _has_more(data: Any, *, page: int, page_size: int) -> tuple[bool, int | None]:
    info = _pagination_info(data)
    if info is None:
        raise OpenApiResponseError(
            "official API did not return verifiable pagination metadata",
            code="PAGINATION_METADATA_MISSING",
            retryable=False,
        )

    echoed_size = _int_value(info, "page_size", "pageSize")
    if echoed_size is not None and echoed_size != page_size:
        raise OpenApiResponseError(
            "official API echoed a different page_size",
            code="PAGINATION_PAGE_SIZE_MISMATCH",
            retryable=False,
        )

    total = _int_value(info, "total_number", "total_num", "total", "count")
    if "has_more" in info:
        return bool(info.get("has_more")), total

    total_pages = _int_value(info, "total_page", "total_pages")
    if total_pages is not None:
        return page < max(0, total_pages), total

    if total is not None:
        return page * page_size < total, total

    raise OpenApiResponseError(
        "official API pagination metadata cannot prove completion",
        code="PAGINATION_COMPLETION_UNKNOWN",
        retryable=False,
    )


def _upper(value: Any) -> str:
    return str(value or "").strip().upper()


def _validate_exact_filter_contract(
    items: list[dict[str, Any]],
    query: Mapping[str, Any],
    *,
    request_id: str,
) -> None:
    """校验当前已确认的“服务端应精确执行”过滤条件。

    Phase 3 热采集明确请求：
    - material_status=DELIVERY_OK
    - task_status=PROCESSING
    - control scene=MATERIAL_ADD_BUDGET

    如果官方成功响应却返回不符合过滤条件的对象，不能在上层静默丢弃后继续把批次标记
    SUCCESS。那样会把“接口语义异常”伪装成“对象自然消失”。这里直接让整批失败并保留
    上一轮可信 Latest。确认任务没有这些活动状态过滤，因此不会被本校验误伤。
    """
    filtering = query.get("filtering")
    if not isinstance(filtering, Mapping) or not items:
        return

    expected_material = _upper(filtering.get("material_status"))
    if expected_material:
        mismatched = [
            str(item.get("material_id") or "?")
            for item in items
            if _upper(item.get("material_status")) != expected_material
        ]
        if mismatched:
            raise OpenApiResponseError(
                "official material response violated requested material_status filter",
                code="MATERIAL_ACTIVE_FILTER_MISMATCH",
                request_id=request_id,
                retryable=False,
            )

    expected_task = _upper(filtering.get("task_status"))
    if expected_task:
        mismatched = [
            str(item.get("id") or "?")
            for item in items
            if _upper(item.get("task_status")) != expected_task
        ]
        if mismatched:
            raise OpenApiResponseError(
                "official control-task response violated requested task_status filter",
                code="CONTROL_ACTIVE_FILTER_MISMATCH",
                request_id=request_id,
                retryable=False,
            )

        expected_scene = _upper(query.get("scene"))
        if expected_scene:
            scene_mismatched = [
                str(item.get("id") or "?")
                for item in items
                if _upper(item.get("scene")) != expected_scene
            ]
            if scene_mismatched:
                raise OpenApiResponseError(
                    "official control-task response violated requested scene filter",
                    code="CONTROL_SCENE_FILTER_MISMATCH",
                    request_id=request_id,
                    retryable=False,
                )


def get_all_pages(
    client: OpenApiClient,
    endpoint: str,
    *,
    query: Mapping[str, Any],
    access_token: str,
    advertiser_id: str = "",
    page_size: int = 100,
    max_pages: int = 1000,
    identity_getter: Callable[[Mapping[str, Any]], Any] | None = None,
    refresh_access_token: Callable[[], str] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """串行读取完整分页，验证总数、重复页、对象唯一性和已声明过滤契约。"""
    size = max(1, min(1000, int(page_size)))
    page_limit = max(1, int(max_pages))
    current_token = str(access_token or "")
    token_refreshed = False

    def collect_once(token: str) -> tuple[list[dict[str, Any]], list[str]]:
        rows: list[dict[str, Any]] = []
        request_ids: list[str] = []
        fingerprints: set[str] = set()
        expected_total: int | None = None

        for page in range(1, page_limit + 1):
            current = dict(query)
            current["page"] = page
            current["page_size"] = size
            response: ApiResponse = client.get(
                endpoint,
                query=current,
                access_token=token,
                advertiser_id=advertiser_id,
            )
            items = extract_items(response.data)
            _validate_exact_filter_contract(
                items,
                query,
                request_id=response.request_id,
            )

            fingerprint = json.dumps(
                items,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            if page > 1 and fingerprint in fingerprints:
                raise OpenApiResponseError(
                    "official API returned a duplicate page",
                    code="PAGINATION_DUPLICATE_PAGE",
                    request_id=response.request_id,
                    retryable=False,
                )
            fingerprints.add(fingerprint)
            rows.extend(items)
            if response.request_id:
                request_ids.append(response.request_id)

            has_more, page_total = _has_more(
                response.data,
                page=page,
                page_size=size,
            )
            if page_total is not None:
                if expected_total is None:
                    expected_total = page_total
                elif expected_total != page_total:
                    raise OpenApiResponseError(
                        "official API total changed during pagination",
                        code="PAGINATION_TOTAL_CHANGED",
                        request_id=response.request_id,
                        retryable=False,
                    )

            if has_more and not items:
                raise OpenApiResponseError(
                    "official API declared another page after an empty page",
                    code="PAGINATION_EMPTY_MIDDLE_PAGE",
                    request_id=response.request_id,
                    retryable=False,
                )
            if not has_more:
                break
        else:
            raise OpenApiResponseError(
                "official API pagination exceeded safety limit",
                code="PAGINATION_LIMIT_EXCEEDED",
                retryable=False,
            )

        if expected_total is not None and len(rows) != expected_total:
            raise OpenApiResponseError(
                "official API row count differs from pagination total",
                code="PAGINATION_TOTAL_MISMATCH",
                retryable=False,
            )

        if identity_getter is not None:
            identities = [str(identity_getter(item) or "").strip() for item in rows]
            if any(not value for value in identities):
                raise OpenApiResponseError(
                    "official API page contains a row without identity",
                    code="PAGINATION_IDENTITY_MISSING",
                    retryable=False,
                )
            if len(identities) != len(set(identities)):
                raise OpenApiResponseError(
                    "official API pagination contains duplicate objects",
                    code="PAGINATION_DUPLICATE_OBJECT",
                    retryable=False,
                )

        return rows, request_ids

    while True:
        try:
            return collect_once(current_token)
        except OpenApiTokenError:
            if token_refreshed or refresh_access_token is None:
                raise
            refreshed = str(refresh_access_token() or "")
            token_refreshed = True
            if not refreshed:
                raise OpenApiTokenError(
                    "token refresh returned an empty access token",
                    code="LOCAL_REFRESH_EMPTY",
                )
            current_token = refreshed
            # 关键：丢弃刷新前所有已读取页，从 page=1 重新开始。
            continue
