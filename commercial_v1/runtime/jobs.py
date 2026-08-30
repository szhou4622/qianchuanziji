"""持久化后台 Job 基础设施。

Phase 1 只提供通用队列/Claim/Heartbeat/Recovery。实时 5 分钟任务的“不补跑”策略将在
对应业务 job type 接入时显式配置，禁止默认把所有过期 RUNNING 任务重新排队。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping

from commercial_v1.storage.database import Database
from commercial_v1.storage.writer import StorageWriter

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


class JobClaimError(RuntimeError):
    pass


class StaleJobFencingToken(RuntimeError):
    pass


@dataclass(frozen=True)
class ClaimedJob:
    job_uid: str
    job_type: str
    priority: int
    payload: dict[str, Any]
    due_at: str
    owner_instance_id: str
    fencing_token: int
    lease_expires_at: str


class PersistentJobStore:
    def __init__(self, database: Database, writer: StorageWriter, *, clock: Clock = utc_now) -> None:
        self._database = database
        self._writer = writer
        self._clock = clock

    def enqueue(self, job_type: str, payload: Mapping[str, Any] | None = None, *, priority: int = 100, due_at: datetime | None = None, job_uid: str | None = None) -> str:
        uid = job_uid or str(uuid.uuid4())
        now_text = _iso(self._clock())
        due_text = _iso(due_at or self._clock())
        payload_json = json.dumps(dict(payload or {}), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        self._writer.execute("INSERT INTO background_job(job_uid,job_type,priority,payload_json,status,due_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (uid, job_type, priority, payload_json, "QUEUED", due_text, now_text, now_text)).result(timeout=5)
        return uid

    def claim_next(self, owner_instance_id: str, *, lease_seconds: int = 45, job_types: Iterable[str] | None = None) -> ClaimedJob | None:
        """Claim 下一个到期 Job。

        Worker 应传入自己明确支持的 job_types，避免通用 Worker 抢到未来业务 Job。
        job_types=None 仅保留给底层测试或显式的全类型 Worker。
        """
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        allowed = tuple(dict.fromkeys(str(item) for item in job_types or ()))
        now = self._clock()
        now_text = _iso(now)
        expires_text = _iso(now + timedelta(seconds=lease_seconds))

        def work(conn):
            if job_types is not None:
                if not allowed:
                    return None
                placeholders = ",".join("?" for _ in allowed)
                sql = f"SELECT * FROM background_job WHERE status='QUEUED' AND due_at<=? AND job_type IN ({placeholders}) ORDER BY priority ASC,due_at ASC,created_at ASC LIMIT 1"
                row = conn.execute(sql, (now_text, *allowed)).fetchone()
            else:
                row = conn.execute("SELECT * FROM background_job WHERE status='QUEUED' AND due_at<=? ORDER BY priority ASC,due_at ASC,created_at ASC LIMIT 1", (now_text,)).fetchone()
            if row is None:
                return None
            token = int(row["fencing_token"] or 0) + 1
            changed = conn.execute("UPDATE background_job SET status='RUNNING',lease_owner=?,lease_expires_at=?,fencing_token=?,started_at=COALESCE(started_at,?),updated_at=? WHERE job_uid=? AND status='QUEUED'", (owner_instance_id, expires_text, token, now_text, now_text, row["job_uid"])).rowcount
            if changed != 1:
                raise JobClaimError(f"job was concurrently claimed: {row['job_uid']}")
            return ClaimedJob(job_uid=str(row["job_uid"]), job_type=str(row["job_type"]), priority=int(row["priority"]), payload=json.loads(str(row["payload_json"])), due_at=str(row["due_at"]), owner_instance_id=owner_instance_id, fencing_token=token, lease_expires_at=expires_text)

        return self._writer.transaction(work).result(timeout=5)

    def heartbeat(self, job: ClaimedJob, *, lease_seconds: int = 45) -> ClaimedJob:
        now = self._clock()
        expires_text = _iso(now + timedelta(seconds=lease_seconds))
        now_text = _iso(now)
        def work(conn):
            self._assert_current(conn, job, now_text)
            conn.execute("UPDATE background_job SET lease_expires_at=?,updated_at=? WHERE job_uid=? AND fencing_token=?", (expires_text, now_text, job.job_uid, job.fencing_token))
        self._writer.transaction(work).result(timeout=5)
        return ClaimedJob(**{**job.__dict__, "lease_expires_at": expires_text})

    def complete(self, job: ClaimedJob, result: Mapping[str, Any] | None = None) -> None:
        now_text = _iso(self._clock())
        result_json = json.dumps(dict(result or {}), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        def work(conn):
            self._assert_current(conn, job, now_text)
            conn.execute("UPDATE background_job SET status='SUCCESS',result_json=?,finished_at=?,updated_at=?,lease_owner=NULL,lease_expires_at=NULL WHERE job_uid=? AND fencing_token=?", (result_json, now_text, now_text, job.job_uid, job.fencing_token))
        self._writer.transaction(work).result(timeout=5)

    def fail(self, job: ClaimedJob, error_message: str) -> None:
        now_text = _iso(self._clock())
        def work(conn):
            self._assert_current(conn, job, now_text)
            conn.execute("UPDATE background_job SET status='FAILED',error_message=?,finished_at=?,updated_at=?,lease_owner=NULL,lease_expires_at=NULL WHERE job_uid=? AND fencing_token=?", (error_message, now_text, now_text, job.job_uid, job.fencing_token))
        self._writer.transaction(work).result(timeout=5)

    def recover_expired(self, recovery_policy: Mapping[str, str]) -> dict[str, int]:
        """未配置 job_type 默认 block，避免误补实时任务。"""
        now_text = _iso(self._clock())
        counts = {"requeue": 0, "abort": 0, "block": 0}
        def work(conn):
            rows = conn.execute("SELECT job_uid,job_type FROM background_job WHERE status='RUNNING' AND lease_expires_at IS NOT NULL AND lease_expires_at<=?", (now_text,)).fetchall()
            for row in rows:
                action = recovery_policy.get(str(row["job_type"]), "block").lower()
                if action == "requeue":
                    new_status, finished_at = "QUEUED", None
                elif action == "abort":
                    new_status, finished_at = "ABORTED_BY_RESTART", now_text
                elif action == "block":
                    new_status, finished_at = "BLOCKED", None
                else:
                    raise ValueError(f"unsupported recovery policy: {action}")
                conn.execute("UPDATE background_job SET status=?,lease_owner=NULL,lease_expires_at=NULL,finished_at=?,updated_at=? WHERE job_uid=?", (new_status, finished_at, now_text, row["job_uid"]))
                counts[action] += 1
            return dict(counts)
        return self._writer.transaction(work).result(timeout=5)

    def get(self, job_uid: str) -> dict[str, Any] | None:
        with self._database.connect(readonly=True) as conn:
            row = conn.execute("SELECT * FROM background_job WHERE job_uid=?", (job_uid,)).fetchone()
            return dict(row) if row is not None else None

    def queue_counts(self) -> dict[str, int]:
        with self._database.connect(readonly=True) as conn:
            rows = conn.execute("SELECT status,COUNT(*) AS n FROM background_job GROUP BY status").fetchall()
        return {str(row["status"]): int(row["n"]) for row in rows}

    @staticmethod
    def _assert_current(conn, job: ClaimedJob, now_text: str) -> None:
        row = conn.execute("SELECT status,lease_owner,lease_expires_at,fencing_token FROM background_job WHERE job_uid=?", (job.job_uid,)).fetchone()
        if row is None:
            raise StaleJobFencingToken(f"job missing: {job.job_uid}")
        if row["status"] != "RUNNING" or row["lease_owner"] != job.owner_instance_id or int(row["fencing_token"] or 0) != job.fencing_token or not row["lease_expires_at"] or str(row["lease_expires_at"]) <= now_text:
            raise StaleJobFencingToken(f"stale job lease: {job.job_uid}")
