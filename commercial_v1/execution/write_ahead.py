"""Phase 6 千川业务 POST 的 Write-Ahead 安全核心。

本模块只解决“如何安全地发一次写请求、如何记录未知结果、何时才允许补偿发送”这一层，
不负责拼装任何具体千川业务接口的正式请求体，也不解除 ``OpenApiClient`` 现有业务 POST
封锁。调用方必须先依据正式 API 契约构造 ``PreparedWriteRequest``，并注入明确实现了
``WriteAdapter`` 的发送器。

硬规则：
- 网络调用之前必须先持久化 execution_attempt，并把 Execution 推进到 SUBMITTING；
- 只有明确证明“请求未发送”时，才可以把 Execution 退回 APPROVED；
- 只要存在“可能已经发送”，立即进入 UNKNOWN_REQUIRES_REVIEW，只允许 GET 对账；
- 已有一次保守计入的发送后，普通 submit 永远不能再发；第二次只能走 compensation=True，
  且必须先有 PROVEN_NOT_EXECUTED 的持久化证据；
- 每个 Execution 最多保守计入两次发送；
- 第二次补偿发送必须与第一次已发送请求的 endpoint + payload 哈希完全一致；
- 未知结果绝不因为 Worker 重启、Job 重跑或调用方再次 submit 而盲重发。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol

from commercial_v1.security.redaction import redact, sanitize_text
from commercial_v1.storage.database import Database
from commercial_v1.storage.writer import StorageWriter

APPROVED = "APPROVED"
SUBMITTING = "SUBMITTING"
SUBMITTED = "SUBMITTED"
VERIFYING = "VERIFYING"
CONFIRMED_SUCCESS = "CONFIRMED_SUCCESS"
CONFIRMED_FAILED = "CONFIRMED_FAILED"
UNKNOWN_REQUIRES_REVIEW = "UNKNOWN_REQUIRES_REVIEW"

ATTEMPT_PENDING = "PENDING"
ATTEMPT_NOT_SENT = "NOT_SENT"
ATTEMPT_ACCEPTED = "ACCEPTED"
ATTEMPT_REJECTED = "REJECTED"
ATTEMPT_UNKNOWN = "UNKNOWN"

RECON_PENDING = "PENDING"
RECON_VERIFYING = "VERIFYING"
RECON_PROVEN_NOT_EXECUTED = "PROVEN_NOT_EXECUTED"
RECON_RESOLVED_SUCCESS = "RESOLVED_SUCCESS"
RECON_RESOLVED_FAILED = "RESOLVED_FAILED"

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


def _hash(endpoint: str, payload: Mapping[str, Any]) -> str:
    raw = f"{endpoint}\n{_json(dict(payload))}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _reconciliation_id(execution_id: str) -> str:
    return "reconciliation_" + hashlib.sha256(execution_id.encode("utf-8")).hexdigest()


class WriteGateBlocked(RuntimeError):
    """本地安全门拒绝发送；该异常本身不代表千川业务失败。"""


class WriteTransportError(RuntimeError):
    """发送器的网络/传输错误。

    ``may_have_been_sent=False`` 只允许在发送器能证明请求没有离开本机时使用；任何超时、
    连接中断、响应读取失败等无法证明服务器未收到的场景都必须传 True。
    """

    def __init__(self, message: str, *, may_have_been_sent: bool) -> None:
        super().__init__(message)
        self.may_have_been_sent = bool(may_have_been_sent)


@dataclass(frozen=True)
class PreparedWriteRequest:
    endpoint: str
    payload: Mapping[str, Any]
    advertiser_id: str
    action_type: str

    def normalized(self) -> "PreparedWriteRequest":
        endpoint = str(self.endpoint or "").strip()
        if not endpoint.startswith("/open_api/"):
            raise ValueError("write endpoint must start with /open_api/")
        advertiser_id = str(self.advertiser_id or "").strip()
        if not advertiser_id:
            raise ValueError("advertiser_id is required")
        action_type = str(self.action_type or "").strip().upper()
        if not action_type:
            raise ValueError("action_type is required")
        if not isinstance(self.payload, Mapping):
            raise ValueError("payload must be an object")
        # 通过 JSON roundtrip 固化键顺序无关、只包含可持久化值的快照。
        payload = json.loads(_json(dict(self.payload)))
        if not isinstance(payload, dict):
            raise ValueError("payload must serialize to an object")
        return PreparedWriteRequest(endpoint, payload, advertiser_id, action_type)

    @property
    def request_hash(self) -> str:
        current = self.normalized()
        return _hash(current.endpoint, current.payload)


@dataclass(frozen=True)
class WriteResponse:
    """动作适配器对一次 POST 响应的确定性解释。

    accepted=True  = 已明确达到“平台受理/接受提交”条件，但尚未代表最终业务成功；
    accepted=False = 已明确被平台拒绝，可直接记为确定失败；
    accepted=None  = HTTP/API 有响应，但业务语义不足以证明接受或拒绝，必须进入对账。
    """

    accepted: bool | None
    http_status: int | None = None
    api_code: str = ""
    request_id: str = ""
    message: str = ""
    response_summary: Mapping[str, Any] | None = None
    external_object_id: str | None = None


class WriteAdapter(Protocol):
    def send(self, request: PreparedWriteRequest) -> WriteResponse: ...


@dataclass(frozen=True)
class WriteSubmissionResult:
    execution_id: str
    attempt_id: str
    attempt_no: int
    execution_status: str
    attempt_outcome: str
    conservative_send_count: int
    request_id: str = ""
    external_object_id: str | None = None


@dataclass(frozen=True)
class ReconcileObservation:
    """只读 GET 对账的确定性结论。"""

    outcome: str
    evidence: Mapping[str, Any]
    request_ids: tuple[str, ...] = ()
    external_object_id: str | None = None
    error_message: str = ""


class ReconcileReader(Protocol):
    def check(self, execution: Mapping[str, Any]) -> ReconcileObservation: ...


@dataclass(frozen=True)
class ReconciliationResult:
    execution_id: str
    execution_status: str
    reconciliation_status: str
    changed: bool
    conservative_send_count: int


class WriteAheadExecutor:
    def __init__(
        self,
        database: Database,
        writer: StorageWriter,
        adapter: WriteAdapter,
        *,
        clock: Clock = utc_now,
    ) -> None:
        self._database = database
        self._writer = writer
        self._adapter = adapter
        self._clock = clock

    def submit(
        self,
        execution_id: str,
        request: PreparedWriteRequest,
        *,
        compensation: bool = False,
    ) -> WriteSubmissionResult:
        eid = str(execution_id or "").strip()
        if not eid:
            raise ValueError("execution_id is required")
        prepared = request.normalized()
        request_hash = prepared.request_hash
        attempt_id, attempt_no, prior_send_count = self._prepare_attempt(
            eid,
            prepared,
            request_hash,
            compensation=compensation,
        )

        try:
            response = self._adapter.send(prepared)
        except WriteTransportError as exc:
            return self._handle_transport_error(
                eid,
                attempt_id,
                attempt_no,
                request_hash,
                exc,
                prior_send_count,
            )
        except BaseException as exc:
            # 一旦进入 Adapter，除非 Adapter 显式证明“未发送”，其余异常都按可能已发送处理。
            unknown = WriteTransportError(
                sanitize_text(f"{type(exc).__name__}: {exc}"),
                may_have_been_sent=True,
            )
            return self._handle_transport_error(
                eid,
                attempt_id,
                attempt_no,
                request_hash,
                unknown,
                prior_send_count,
            )

        return self._handle_response(
            eid,
            attempt_id,
            attempt_no,
            request_hash,
            response,
            prior_send_count,
        )

    def _prepare_attempt(
        self,
        execution_id: str,
        request: PreparedWriteRequest,
        request_hash: str,
        *,
        compensation: bool,
    ) -> tuple[str, int, int]:
        now = _iso(self._clock())

        def work(conn):
            row = conn.execute(
                "SELECT * FROM execution_task WHERE execution_id=?",
                (execution_id,),
            ).fetchone()
            if row is None:
                raise WriteGateBlocked("EXECUTION_NOT_FOUND")
            if str(row["status"] or "") != APPROVED:
                raise WriteGateBlocked(f"EXECUTION_STATUS_{row['status']}")
            if str(row["advertiser_id"] or "") != request.advertiser_id:
                raise WriteGateBlocked("ADVERTISER_CONTEXT_MISMATCH")
            if str(row["action_type"] or "").upper() != request.action_type:
                raise WriteGateBlocked("ACTION_CONTEXT_MISMATCH")

            pending = conn.execute(
                "SELECT 1 FROM execution_attempt WHERE execution_id=? AND outcome=? LIMIT 1",
                (execution_id, ATTEMPT_PENDING),
            ).fetchone()
            if pending is not None:
                raise WriteGateBlocked("WRITE_ATTEMPT_ALREADY_PENDING")

            attempts = conn.execute(
                """SELECT attempt_no,request_hash,request_sent_at,transport_status,outcome
                   FROM execution_attempt WHERE execution_id=? ORDER BY attempt_no""",
                (execution_id,),
            ).fetchall()
            sent = [item for item in attempts if self._counts_as_send(item)]
            send_count = len(sent)
            if send_count >= 2:
                raise WriteGateBlocked("POST_SEND_LIMIT_REACHED")

            if send_count == 0:
                if compensation:
                    raise WriteGateBlocked("COMPENSATION_WITHOUT_PRIOR_SEND")
            else:
                if not compensation:
                    raise WriteGateBlocked("PRIOR_SEND_REQUIRES_RECONCILIATION")
                proof = conn.execute(
                    """SELECT status FROM execution_reconciliation
                       WHERE execution_id=? ORDER BY resolved_at DESC,last_checked_at DESC LIMIT 1""",
                    (execution_id,),
                ).fetchone()
                if proof is None or str(proof["status"] or "") != RECON_PROVEN_NOT_EXECUTED:
                    raise WriteGateBlocked("COMPENSATION_NOT_PROVEN_SAFE")
                first_hash = str(sent[0]["request_hash"] or "")
                if first_hash != request_hash:
                    raise WriteGateBlocked("COMPENSATION_REQUEST_HASH_MISMATCH")

            attempt_no = max((int(item["attempt_no"]) for item in attempts), default=0) + 1
            attempt_id = "attempt_" + uuid.uuid4().hex
            summary = redact(
                {
                    "endpoint": request.endpoint,
                    "advertiser_id": request.advertiser_id,
                    "action_type": request.action_type,
                    "payload": dict(request.payload),
                    "compensation": bool(compensation),
                }
            )
            conn.execute(
                """INSERT INTO execution_attempt(
                   attempt_id,execution_id,attempt_no,endpoint,request_hash,request_summary_json,
                   started_at,transport_status,outcome
                   ) VALUES(?,?,?,?,?,?,?,'PREPARED',?)""",
                (
                    attempt_id,
                    execution_id,
                    attempt_no,
                    request.endpoint,
                    request_hash,
                    _json(summary),
                    now,
                    ATTEMPT_PENDING,
                ),
            )
            changed = conn.execute(
                """UPDATE execution_task SET status=?,last_error_code=NULL,last_error_message=NULL
                   WHERE execution_id=? AND status=?""",
                (SUBMITTING, execution_id, APPROVED),
            ).rowcount
            if changed != 1:
                raise WriteGateBlocked("EXECUTION_CHANGED_DURING_WRITE_AHEAD")
            return attempt_id, attempt_no, send_count

        return self._writer.transaction(work).result(timeout=10)

    @staticmethod
    def _counts_as_send(row: Mapping[str, Any]) -> bool:
        if row["request_sent_at"] not in (None, ""):
            return True
        return str(row["transport_status"] or "") in {
            "SENT",
            "RESPONSE_RECEIVED",
            "OUTCOME_UNKNOWN",
        }

    def _handle_transport_error(
        self,
        execution_id: str,
        attempt_id: str,
        attempt_no: int,
        request_hash: str,
        exc: WriteTransportError,
        prior_send_count: int,
    ) -> WriteSubmissionResult:
        now = _iso(self._clock())
        message = sanitize_text(str(exc))[:2000]
        if not exc.may_have_been_sent:
            def not_sent(conn):
                conn.execute(
                    """UPDATE execution_attempt SET transport_status='NOT_SENT',outcome=?,error_message=?
                       WHERE attempt_id=? AND outcome=?""",
                    (ATTEMPT_NOT_SENT, message, attempt_id, ATTEMPT_PENDING),
                )
                conn.execute(
                    """UPDATE execution_task SET status=?,last_error_code='POST_NOT_SENT',last_error_message=?
                       WHERE execution_id=? AND status=?""",
                    (APPROVED, message, execution_id, SUBMITTING),
                )
            self._writer.transaction(not_sent).result(timeout=10)
            return WriteSubmissionResult(
                execution_id,
                attempt_id,
                attempt_no,
                APPROVED,
                ATTEMPT_NOT_SENT,
                prior_send_count,
            )

        def unknown(conn):
            conn.execute(
                """UPDATE execution_attempt SET request_sent_at=COALESCE(request_sent_at,?),
                   transport_status='OUTCOME_UNKNOWN',outcome=?,error_message=?
                   WHERE attempt_id=? AND outcome=?""",
                (now, ATTEMPT_UNKNOWN, message, attempt_id, ATTEMPT_PENDING),
            )
            conn.execute(
                """UPDATE execution_task SET status=?,submitted_at=COALESCE(submitted_at,?),
                   last_error_code='POST_OUTCOME_UNKNOWN',last_error_message=?
                   WHERE execution_id=? AND status=?""",
                (UNKNOWN_REQUIRES_REVIEW, now, message, execution_id, SUBMITTING),
            )
            self._ensure_reconciliation(conn, execution_id, now)
        self._writer.transaction(unknown).result(timeout=10)
        return WriteSubmissionResult(
            execution_id,
            attempt_id,
            attempt_no,
            UNKNOWN_REQUIRES_REVIEW,
            ATTEMPT_UNKNOWN,
            prior_send_count + 1,
        )

    def _handle_response(
        self,
        execution_id: str,
        attempt_id: str,
        attempt_no: int,
        request_hash: str,
        response: WriteResponse,
        prior_send_count: int,
    ) -> WriteSubmissionResult:
        now = _iso(self._clock())
        api_code = str(response.api_code or "")
        request_id = str(response.request_id or "")
        message = sanitize_text(str(response.message or ""))[:2000]
        response_summary = redact(dict(response.response_summary or {}))
        external_id = str(response.external_object_id or "").strip() or None

        if response.accepted is False:
            status = CONFIRMED_FAILED
            outcome = ATTEMPT_REJECTED
        elif response.accepted is True:
            status = SUBMITTED
            outcome = ATTEMPT_ACCEPTED
        else:
            status = UNKNOWN_REQUIRES_REVIEW
            outcome = ATTEMPT_UNKNOWN

        def work(conn):
            conn.execute(
                """UPDATE execution_attempt SET request_sent_at=COALESCE(request_sent_at,?),
                   response_received_at=?,transport_status='RESPONSE_RECEIVED',http_status=?,api_code=?,
                   request_id=?,response_summary_json=?,outcome=?,error_message=?
                   WHERE attempt_id=? AND outcome=?""",
                (
                    now,
                    now,
                    response.http_status,
                    api_code,
                    request_id,
                    _json(response_summary),
                    outcome,
                    message or None,
                    attempt_id,
                    ATTEMPT_PENDING,
                ),
            )
            if status == CONFIRMED_FAILED:
                conn.execute(
                    """UPDATE execution_task SET status=?,submitted_at=COALESCE(submitted_at,?),
                       confirmed_at=?,last_error_code=?,last_error_message=?
                       WHERE execution_id=? AND status=?""",
                    (
                        status,
                        now,
                        now,
                        api_code or "POST_REJECTED",
                        message,
                        execution_id,
                        SUBMITTING,
                    ),
                )
            else:
                conn.execute(
                    """UPDATE execution_task SET status=?,submitted_at=COALESCE(submitted_at,?),
                       external_object_id=COALESCE(?,external_object_id),last_error_code=?,last_error_message=?
                       WHERE execution_id=? AND status=?""",
                    (
                        status,
                        now,
                        external_id,
                        None if status == SUBMITTED else "POST_RESPONSE_AMBIGUOUS",
                        None if status == SUBMITTED else message,
                        execution_id,
                        SUBMITTING,
                    ),
                )
                self._ensure_reconciliation(conn, execution_id, now)

        self._writer.transaction(work).result(timeout=10)
        return WriteSubmissionResult(
            execution_id,
            attempt_id,
            attempt_no,
            status,
            outcome,
            prior_send_count + 1,
            request_id=request_id,
            external_object_id=external_id,
        )

    def _ensure_reconciliation(self, conn, execution_id: str, now: str) -> None:
        execution = conn.execute(
            """SELECT action_type,control_task_id,expected_after_json
               FROM execution_task WHERE execution_id=?""",
            (execution_id,),
        ).fetchone()
        if execution is None:
            raise WriteGateBlocked("EXECUTION_DISAPPEARED")
        reconciliation_id = _reconciliation_id(execution_id)
        conn.execute(
            """INSERT INTO execution_reconciliation(
               reconciliation_id,execution_id,action_type,control_task_id,expected_state_json,
               status,attempt_count,last_checked_at,next_check_at
               ) VALUES(?,?,?,?,?,'PENDING',0,NULL,?)
               ON CONFLICT(reconciliation_id) DO UPDATE SET
                 status=CASE
                   WHEN execution_reconciliation.status IN('RESOLVED_SUCCESS','RESOLVED_FAILED')
                   THEN execution_reconciliation.status ELSE 'PENDING' END,
                 next_check_at=CASE
                   WHEN execution_reconciliation.status IN('RESOLVED_SUCCESS','RESOLVED_FAILED')
                   THEN execution_reconciliation.next_check_at ELSE excluded.next_check_at END""",
            (
                reconciliation_id,
                execution_id,
                execution["action_type"],
                execution["control_task_id"],
                execution["expected_after_json"] or "{}",
                now,
            ),
        )


class ExecutionReconciler:
    """只读对账器。Reader 必须只做 GET/本地证据读取，不允许发业务 POST。"""

    def __init__(
        self,
        database: Database,
        writer: StorageWriter,
        reader: ReconcileReader,
        *,
        clock: Clock = utc_now,
    ) -> None:
        self._database = database
        self._writer = writer
        self._reader = reader
        self._clock = clock

    def reconcile(self, execution_id: str) -> ReconciliationResult:
        eid = str(execution_id or "").strip()
        if not eid:
            raise ValueError("execution_id is required")
        with self._database.connect(readonly=True) as conn:
            row = conn.execute("SELECT * FROM execution_task WHERE execution_id=?", (eid,)).fetchone()
            if row is None:
                raise WriteGateBlocked("EXECUTION_NOT_FOUND")
            execution = dict(row)
        if str(execution["status"] or "") not in {
            SUBMITTED,
            VERIFYING,
            UNKNOWN_REQUIRES_REVIEW,
        }:
            raise WriteGateBlocked(f"RECONCILE_STATUS_{execution['status']}")

        now = _iso(self._clock())

        def mark_verifying(conn):
            reconciliation = conn.execute(
                "SELECT * FROM execution_reconciliation WHERE execution_id=?",
                (eid,),
            ).fetchone()
            if reconciliation is None:
                raise WriteGateBlocked("RECONCILIATION_NOT_FOUND")
            conn.execute(
                """UPDATE execution_reconciliation SET status=?,attempt_count=attempt_count+1,
                   last_checked_at=?,error_message=NULL WHERE reconciliation_id=?""",
                (RECON_VERIFYING, now, reconciliation["reconciliation_id"]),
            )
            conn.execute(
                "UPDATE execution_task SET status=? WHERE execution_id=? AND status IN(?,?,?)",
                (VERIFYING, eid, SUBMITTED, VERIFYING, UNKNOWN_REQUIRES_REVIEW),
            )
        self._writer.transaction(mark_verifying).result(timeout=10)

        try:
            observation = self._reader.check(execution)
        except BaseException as exc:
            message = sanitize_text(f"{type(exc).__name__}: {exc}")[:2000]
            def failed_read(conn):
                conn.execute(
                    """UPDATE execution_reconciliation SET status=?,error_message=?
                       WHERE execution_id=?""",
                    (RECON_PENDING, message, eid),
                )
                conn.execute(
                    """UPDATE execution_task SET status=?,last_error_code='RECONCILIATION_READ_FAILED',
                       last_error_message=? WHERE execution_id=?""",
                    (UNKNOWN_REQUIRES_REVIEW, message, eid),
                )
            self._writer.transaction(failed_read).result(timeout=10)
            return ReconciliationResult(
                eid,
                UNKNOWN_REQUIRES_REVIEW,
                RECON_PENDING,
                True,
                self._conservative_send_count(eid),
            )

        outcome = str(observation.outcome or "").strip().upper()
        if outcome not in {
            CONFIRMED_SUCCESS,
            CONFIRMED_FAILED,
            RECON_PROVEN_NOT_EXECUTED,
            "UNKNOWN",
        }:
            raise ValueError(f"unsupported reconcile outcome: {outcome}")
        request_ids = tuple(str(item) for item in observation.request_ids if str(item))
        evidence_json = _json(redact(dict(observation.evidence or {})))
        external_id = str(observation.external_object_id or "").strip() or None
        message = sanitize_text(str(observation.error_message or ""))[:2000]
        changed = True

        def apply(conn):
            if outcome == CONFIRMED_SUCCESS:
                conn.execute(
                    """UPDATE execution_reconciliation SET status=?,evidence_json=?,request_ids_json=?,
                       resolved_at=?,next_check_at=NULL,error_message=NULL WHERE execution_id=?""",
                    (RECON_RESOLVED_SUCCESS, evidence_json, _json(request_ids), now, eid),
                )
                conn.execute(
                    """UPDATE execution_task SET status=?,confirmed_at=?,
                       external_object_id=COALESCE(?,external_object_id),last_error_code=NULL,
                       last_error_message=NULL WHERE execution_id=?""",
                    (CONFIRMED_SUCCESS, now, external_id, eid),
                )
                return CONFIRMED_SUCCESS, RECON_RESOLVED_SUCCESS
            if outcome == CONFIRMED_FAILED:
                conn.execute(
                    """UPDATE execution_reconciliation SET status=?,evidence_json=?,request_ids_json=?,
                       resolved_at=?,next_check_at=NULL,error_message=? WHERE execution_id=?""",
                    (RECON_RESOLVED_FAILED, evidence_json, _json(request_ids), now, message or None, eid),
                )
                conn.execute(
                    """UPDATE execution_task SET status=?,confirmed_at=?,last_error_code='RECONCILED_FAILED',
                       last_error_message=? WHERE execution_id=?""",
                    (CONFIRMED_FAILED, now, message or None, eid),
                )
                return CONFIRMED_FAILED, RECON_RESOLVED_FAILED
            if outcome == RECON_PROVEN_NOT_EXECUTED:
                send_count = self._conservative_send_count_conn(conn, eid)
                if send_count >= 2:
                    conn.execute(
                        """UPDATE execution_reconciliation SET status=?,evidence_json=?,request_ids_json=?,
                           resolved_at=?,next_check_at=NULL,error_message='POST send limit already reached'
                           WHERE execution_id=?""",
                        (RECON_RESOLVED_FAILED, evidence_json, _json(request_ids), now, eid),
                    )
                    conn.execute(
                        """UPDATE execution_task SET status=?,confirmed_at=?,
                           last_error_code='POST_SEND_LIMIT_REACHED',last_error_message='POST send limit already reached'
                           WHERE execution_id=?""",
                        (CONFIRMED_FAILED, now, eid),
                    )
                    return CONFIRMED_FAILED, RECON_RESOLVED_FAILED
                conn.execute(
                    """UPDATE execution_reconciliation SET status=?,evidence_json=?,request_ids_json=?,
                       resolved_at=?,next_check_at=NULL,error_message=NULL WHERE execution_id=?""",
                    (RECON_PROVEN_NOT_EXECUTED, evidence_json, _json(request_ids), now, eid),
                )
                # 这里只开放“补偿资格”，不会自动发第二次。submit(compensation=True) 仍会再次
                # 校验持久化证明、请求哈希和两次发送上限。
                conn.execute(
                    """UPDATE execution_task SET status=?,last_error_code='COMPENSATION_ALLOWED',
                       last_error_message=NULL WHERE execution_id=?""",
                    (APPROVED, eid),
                )
                return APPROVED, RECON_PROVEN_NOT_EXECUTED

            conn.execute(
                """UPDATE execution_reconciliation SET status=?,evidence_json=?,request_ids_json=?,
                   next_check_at=NULL,error_message=? WHERE execution_id=?""",
                (RECON_PENDING, evidence_json, _json(request_ids), message or None, eid),
            )
            conn.execute(
                """UPDATE execution_task SET status=?,last_error_code='RECONCILIATION_UNKNOWN',
                   last_error_message=? WHERE execution_id=?""",
                (UNKNOWN_REQUIRES_REVIEW, message or None, eid),
            )
            return UNKNOWN_REQUIRES_REVIEW, RECON_PENDING

        execution_status, reconciliation_status = self._writer.transaction(apply).result(timeout=10)
        return ReconciliationResult(
            eid,
            execution_status,
            reconciliation_status,
            changed,
            self._conservative_send_count(eid),
        )

    def _conservative_send_count(self, execution_id: str) -> int:
        with self._database.connect(readonly=True) as conn:
            return self._conservative_send_count_conn(conn, execution_id)

    @staticmethod
    def _conservative_send_count_conn(conn, execution_id: str) -> int:
        return int(
            conn.execute(
                """SELECT COUNT(*) FROM execution_attempt
                   WHERE execution_id=? AND (
                     request_sent_at IS NOT NULL OR transport_status IN('SENT','RESPONSE_RECEIVED','OUTCOME_UNKNOWN')
                   )""",
                (execution_id,),
            ).fetchone()[0]
        )
