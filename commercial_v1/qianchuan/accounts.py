"""千川最终 advertiser 发现、落库与启用管理。

OAuth 授权主体、店铺主体、企业/BP 主体都不是自动执行时可以直接使用的最终账户。
本模块只把经过 EBP / Shop / PublicInfo 证据确认的 advertiser_id 写入业务账户表。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from commercial_v1.security.redaction import sanitize_text
from commercial_v1.storage.database import Database
from commercial_v1.storage.writer import StorageWriter

from .client import OpenApiClient
from .contracts import (
    ADVERTISER_PUBLIC_INFO,
    EBP_ADVERTISER_LIST,
    OAUTH_ADVERTISER_GET,
    SHOP_ADVERTISER_LIST,
)
from .normalizers import (
    FinalAdvertiser,
    OAuthSubject,
    normalize_final_advertiser,
    normalize_oauth_subject,
    require_digit_id,
)
from .pagination import extract_items, get_all_pages
from .token_provider import OAuthTokenProvider


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class AccountDiscoveryResult:
    accounts: tuple[FinalAdvertiser, ...]
    subjects: tuple[OAuthSubject, ...]
    complete: bool
    evidence: Mapping[str, Any]


class AccountStore:
    MAX_ENABLED_ACCOUNTS = 10

    def __init__(self, database: Database, writer: StorageWriter) -> None:
        self._database = database
        self._writer = writer

    @staticmethod
    def account_uid(advertiser_id: str) -> str:
        return f"qc:{require_digit_id(advertiser_id, 'advertiser_id')}"

    def upsert_discovered(
        self,
        accounts: tuple[FinalAdvertiser, ...],
        *,
        auth_profile_id: str,
        discovery_sources: Mapping[str, list[str]],
    ) -> None:
        now = _now()

        def work(conn):
            for account in accounts:
                uid = self.account_uid(account.advertiser_id)
                existing = conn.execute(
                    "SELECT enabled,created_at FROM qianchuan_account WHERE account_uid=?",
                    (uid,),
                ).fetchone()
                enabled = int(existing["enabled"]) if existing is not None else 0
                created_at = str(existing["created_at"]) if existing is not None else now
                sources = discovery_sources.get(account.advertiser_id) or [account.source]
                capability_json = json.dumps(
                    {
                        "discovery_sources": sorted(set(str(v) for v in sources)),
                        "final_advertiser_verified": True,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                conn.execute(
                    """INSERT INTO qianchuan_account(
                       account_uid,advertiser_id,account_name,account_type,enabled,auth_status,
                       capability_json,last_auth_ok_at,last_seen_at,created_at,updated_at
                       ) VALUES(?,?,?,?,?,'ACTIVE',?,?,?,?,?)
                       ON CONFLICT(account_uid) DO UPDATE SET
                         advertiser_id=excluded.advertiser_id,
                         account_name=CASE WHEN excluded.account_name<>'' THEN excluded.account_name ELSE qianchuan_account.account_name END,
                         account_type=excluded.account_type,
                         auth_status='ACTIVE',
                         capability_json=excluded.capability_json,
                         last_auth_ok_at=excluded.last_auth_ok_at,
                         last_seen_at=excluded.last_seen_at,
                         updated_at=excluded.updated_at""",
                    (
                        uid,
                        account.advertiser_id,
                        account.advertiser_name,
                        account.account_type or "QIANCHUAN",
                        enabled,
                        capability_json,
                        now,
                        now,
                        created_at,
                        now,
                    ),
                )
                conn.execute(
                    "UPDATE qianchuan_account_auth SET is_primary=0 WHERE account_uid=?",
                    (uid,),
                )
                conn.execute(
                    """INSERT INTO qianchuan_account_auth(
                       account_uid,auth_profile_id,is_primary,bound_at,last_verified_at,created_at
                       ) VALUES(?,?,1,?,?,?)
                       ON CONFLICT(account_uid,auth_profile_id) DO UPDATE SET
                         is_primary=1,last_verified_at=excluded.last_verified_at""",
                    (uid, auth_profile_id, now, now, now),
                )

        self._writer.transaction(work).result(timeout=10)

    def set_enabled(self, advertiser_id: str, enabled: bool) -> None:
        aid = require_digit_id(advertiser_id, "advertiser_id")
        uid = self.account_uid(aid)
        now = _now()

        def work(conn):
            row = conn.execute(
                "SELECT enabled FROM qianchuan_account WHERE account_uid=?",
                (uid,),
            ).fetchone()
            if row is None:
                raise ValueError("advertiser account has not been discovered")
            if enabled and not int(row["enabled"]):
                current = conn.execute(
                    "SELECT COUNT(*) FROM qianchuan_account WHERE enabled=1"
                ).fetchone()[0]
                if int(current) >= self.MAX_ENABLED_ACCOUNTS:
                    raise ValueError("最多只能启用10个千川账户")
            conn.execute(
                "UPDATE qianchuan_account SET enabled=?,updated_at=? WHERE account_uid=?",
                (1 if enabled else 0, now, uid),
            )

        self._writer.transaction(work).result(timeout=5)

    def list_accounts(self) -> list[dict[str, Any]]:
        with self._database.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT * FROM qianchuan_account ORDER BY enabled DESC,account_name,advertiser_id"
            ).fetchall()
        return [dict(row) for row in rows]


class AccountDiscoveryService:
    def __init__(
        self,
        client: OpenApiClient,
        token_provider: OAuthTokenProvider,
        store: AccountStore,
    ) -> None:
        self._client = client
        self._tokens = token_provider
        self._store = store

    def discover(self, auth_profile_id: str) -> AccountDiscoveryResult:
        access_token = self._tokens.get_access_token(auth_profile_id)
        evidence: dict[str, Any] = {
            "complete": True,
            "oauth_subjects": [],
            "lanes": {},
        }

        subject_response = self._client.get(
            OAUTH_ADVERTISER_GET,
            access_token=access_token,
        )
        subjects = tuple(
            normalize_oauth_subject(row) for row in extract_items(subject_response.data)
        )
        evidence["oauth_request_id"] = subject_response.request_id
        evidence["oauth_subjects"] = [asdict(subject) for subject in subjects]

        resolved: dict[str, FinalAdvertiser] = {}
        sources: dict[str, list[str]] = {}

        def add(account: FinalAdvertiser) -> None:
            existing = resolved.get(account.advertiser_id)
            if existing is None or (not existing.advertiser_name and account.advertiser_name):
                resolved[account.advertiser_id] = account
            sources.setdefault(account.advertiser_id, []).append(account.source)

        # 1) 最终 EBP/Qianchuan 账户。正式契约以 account_id 为最终 advertiser_id。
        try:
            rows, request_ids = get_all_pages(
                self._client,
                EBP_ADVERTISER_LIST,
                query={"account_type": "QIANCHUAN"},
                access_token=access_token,
                page_size=100,
                identity_getter=lambda row: row.get("account_id"),
            )
            for row in rows:
                add(normalize_final_advertiser(row, source="EBP"))
            evidence["lanes"]["ebp"] = {
                "complete": True,
                "count": len(rows),
                "request_ids": request_ids,
            }
        except Exception as exc:
            evidence["complete"] = False
            evidence["lanes"]["ebp"] = {
                "complete": False,
                "error": sanitize_text(exc),
            }

        # 2) 店铺主体必须展开为最终广告账户。
        shop_subjects = [subject for subject in subjects if subject.subject_kind == "SHOP"]
        shop_count = 0
        for subject in shop_subjects:
            shop_id = subject.shop_id or subject.subject_id
            if not shop_id:
                evidence["complete"] = False
                evidence["lanes"].setdefault("shop", {"subjects": []})["subjects"].append(
                    {"subject_id": subject.subject_id, "complete": False, "error": "shop id missing"}
                )
                continue
            try:
                rows, request_ids = get_all_pages(
                    self._client,
                    SHOP_ADVERTISER_LIST,
                    query={"shop_id": shop_id, "permission": ["QC_AWEME"]},
                    access_token=access_token,
                    page_size=100,
                    identity_getter=lambda row: row.get("advertiser_id") or row.get("account_id"),
                )
                for row in rows:
                    add(normalize_final_advertiser(row, source="SHOP"))
                shop_count += len(rows)
                evidence["lanes"].setdefault("shop", {"subjects": []})["subjects"].append(
                    {
                        "subject_id": subject.subject_id,
                        "shop_id": shop_id,
                        "complete": True,
                        "count": len(rows),
                        "request_ids": request_ids,
                    }
                )
            except Exception as exc:
                evidence["complete"] = False
                evidence["lanes"].setdefault("shop", {"subjects": []})["subjects"].append(
                    {
                        "subject_id": subject.subject_id,
                        "shop_id": shop_id,
                        "complete": False,
                        "error": sanitize_text(exc),
                    }
                )
        evidence["lanes"].setdefault("shop", {"subjects": []})["count"] = shop_count

        # 3) 若 OAuth 明确回显 ADVERTISER 主体，只能通过 PublicInfo 再确认，不能直接采用主体 ID。
        direct_subject_ids = [
            subject.subject_id
            for subject in subjects
            if subject.subject_kind == "ADVERTISER_SUBJECT" and subject.subject_id.isdigit()
        ]
        if direct_subject_ids:
            self._enrich_or_confirm_public_info(
                access_token,
                direct_subject_ids,
                resolved,
                sources,
                evidence,
                confirm_missing=True,
            )

        # 4) 对所有已确认 advertiser 再查 PublicInfo 补全名称。失败不否定账户 ID 身份。
        if resolved:
            self._enrich_or_confirm_public_info(
                access_token,
                sorted(resolved),
                resolved,
                sources,
                evidence,
                confirm_missing=False,
            )

        accounts = tuple(sorted(resolved.values(), key=lambda item: item.advertiser_id))
        evidence["complete"] = bool(evidence["complete"])
        evidence["account_count"] = len(accounts)
        self._store.upsert_discovered(
            accounts,
            auth_profile_id=auth_profile_id,
            discovery_sources=sources,
        )
        return AccountDiscoveryResult(
            accounts=accounts,
            subjects=subjects,
            complete=bool(evidence["complete"]),
            evidence=evidence,
        )

    def _enrich_or_confirm_public_info(
        self,
        access_token: str,
        advertiser_ids: list[str],
        resolved: dict[str, FinalAdvertiser],
        sources: dict[str, list[str]],
        evidence: dict[str, Any],
        *,
        confirm_missing: bool,
    ) -> None:
        unique_ids = list(dict.fromkeys(require_digit_id(value, "advertiser_id") for value in advertiser_ids))
        public_rows: list[FinalAdvertiser] = []
        request_ids: list[str] = []
        try:
            for offset in range(0, len(unique_ids), 100):
                batch = unique_ids[offset : offset + 100]
                response = self._client.get(
                    ADVERTISER_PUBLIC_INFO,
                    query={"advertiser_ids": batch},
                    access_token=access_token,
                )
                request_ids.append(response.request_id)
                for row in extract_items(response.data):
                    public_rows.append(normalize_final_advertiser(row, source="PUBLIC_INFO"))
        except Exception as exc:
            lane = evidence["lanes"].setdefault("public_info", {})
            lane["complete"] = False
            lane["error"] = sanitize_text(exc)
            if confirm_missing:
                evidence["complete"] = False
            return

        public_by_id = {item.advertiser_id: item for item in public_rows}
        for aid, public in public_by_id.items():
            current = resolved.get(aid)
            if current is None and confirm_missing:
                resolved[aid] = public
                sources.setdefault(aid, []).append("PUBLIC_INFO")
            elif current is not None and public.advertiser_name:
                resolved[aid] = FinalAdvertiser(
                    advertiser_id=current.advertiser_id,
                    advertiser_name=public.advertiser_name,
                    account_type=current.account_type or public.account_type,
                    source=current.source,
                )
                sources.setdefault(aid, []).append("PUBLIC_INFO")

        missing = [aid for aid in unique_ids if aid not in public_by_id]
        if confirm_missing and missing:
            evidence["complete"] = False
        lane = evidence["lanes"].setdefault("public_info", {})
        lane.update(
            {
                "complete": not (confirm_missing and missing),
                "requested": len(unique_ids),
                "returned": len(public_rows),
                "missing": missing,
                "request_ids": [value for value in request_ids if value],
            }
        )
