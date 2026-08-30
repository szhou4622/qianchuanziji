import json
from datetime import datetime, timezone
from pathlib import Path

from commercial_v1.qianchuan.hot_scheduler import CONTROL_5M, MATERIAL_5M, HotCollectionScheduler
from commercial_v1.storage.database import Database, DatabaseConfig
from commercial_v1.storage.schema import create_schema_v1
from commercial_v1.storage.writer import StorageWriter


def _setup(tmp_path: Path):
    db = Database(DatabaseConfig(tmp_path / "runtime.db"))
    with db.connect() as conn:
        create_schema_v1(conn)
    writer = StorageWriter(db)
    writer.start()
    now = "2026-08-30T00:00:00+00:00"
    writer.execute(
        "INSERT INTO qianchuan_account(account_uid,advertiser_id,enabled,auth_status,created_at,updated_at) VALUES('acc','111111',1,'ACTIVE',?,?)",
        (now, now),
    ).result(timeout=5)
    writer.execute(
        """INSERT INTO monitor_plan(
           target_uid,account_uid,advertiser_id,ad_id,plan_system,promotion_scene,official_status,
           monitor_enabled,lifecycle_state,collection_active,strategy_eligible,write_eligible,sync_state,
           next_hot_collect_at,created_at,updated_at
           ) VALUES('target','acc','111111','222222','UNI_PROJECT','VIDEO_PROM_GOODS','DELIVERY_OK',
                    1,'ACTIVE_COLLECTING',1,1,1,'TRUSTED','2026-08-30T07:00:00+00:00',?,?)""",
        (now, now),
    ).result(timeout=5)
    return db, writer


def test_scheduler_enqueues_two_independent_current_jobs_once(tmp_path: Path) -> None:
    db, writer = _setup(tmp_path)
    try:
        scheduler = HotCollectionScheduler(db, writer)
        result = scheduler.run_once(now=datetime(2026, 8, 30, 7, 0, 10, tzinfo=timezone.utc))
        assert result == {"enqueued": 2, "skipped_overlap": 0, "skipped_stale": 0}
        with db.connect(readonly=True) as conn:
            rows = conn.execute("SELECT job_type,payload_json FROM background_job ORDER BY job_type").fetchall()
            assert {row["job_type"] for row in rows} == {MATERIAL_5M, CONTROL_5M}
            payloads = [json.loads(row["payload_json"]) for row in rows]
            assert all(payload["target_uid"] == "target" for payload in payloads)
            plan = conn.execute("SELECT next_hot_collect_at FROM monitor_plan WHERE target_uid='target'").fetchone()
            assert plan["next_hot_collect_at"] == "2026-08-30T07:05:00+00:00"

        again = scheduler.run_once(now=datetime(2026, 8, 30, 7, 0, 20, tzinfo=timezone.utc))
        assert again["enqueued"] == 0
    finally:
        writer.close()


def test_scheduler_skips_stale_realtime_slot_instead_of_backfill(tmp_path: Path) -> None:
    db, writer = _setup(tmp_path)
    try:
        writer.execute(
            "UPDATE monitor_plan SET last_hot_collect_at='2026-08-30T06:55:00+00:00' WHERE target_uid='target'"
        ).result(timeout=5)
        scheduler = HotCollectionScheduler(db, writer, max_lateness_seconds=90)
        result = scheduler.run_once(now=datetime(2026, 8, 30, 7, 3, 0, tzinfo=timezone.utc))
        assert result["enqueued"] == 0
        assert result["skipped_stale"] == 1
        with db.connect(readonly=True) as conn:
            assert conn.execute("SELECT COUNT(*) FROM background_job").fetchone()[0] == 0
            next_due = conn.execute("SELECT next_hot_collect_at FROM monitor_plan").fetchone()[0]
            assert next_due == "2026-08-30T07:05:00+00:00"
    finally:
        writer.close()


def test_overlap_only_skips_affected_pipeline(tmp_path: Path) -> None:
    db, writer = _setup(tmp_path)
    try:
        now = "2026-08-30T07:00:00+00:00"
        payload = json.dumps({"target_uid": "target", "scheduled_at": now}, separators=(",", ":"), sort_keys=True)
        writer.execute(
            """INSERT INTO background_job(job_uid,job_type,priority,payload_json,status,due_at,created_at,updated_at)
               VALUES('old-material',?,40,?,'RUNNING',?,?,?)""",
            (MATERIAL_5M, payload, now, now, now),
        ).result(timeout=5)
        scheduler = HotCollectionScheduler(db, writer)
        result = scheduler.run_once(now=datetime(2026, 8, 30, 7, 0, 10, tzinfo=timezone.utc))
        assert result["skipped_overlap"] == 1
        assert result["enqueued"] == 1
        with db.connect(readonly=True) as conn:
            skipped = conn.execute(
                "SELECT status,pipeline_type FROM collection_batch WHERE status='SKIPPED_OVERLAP'"
            ).fetchone()
            assert skipped["pipeline_type"] == MATERIAL_5M
            queued = conn.execute("SELECT job_type FROM background_job WHERE status='QUEUED'").fetchall()
            assert [row["job_type"] for row in queued] == [CONTROL_5M]
    finally:
        writer.close()


def test_license_gate_prevents_new_hot_jobs(tmp_path: Path) -> None:
    db, writer = _setup(tmp_path)
    try:
        scheduler = HotCollectionScheduler(db, writer, business_allowed=lambda: False)
        result = scheduler.run_once(now=datetime(2026, 8, 30, 7, 0, 10, tzinfo=timezone.utc))
        assert result["enqueued"] == 0
        with db.connect(readonly=True) as conn:
            assert conn.execute("SELECT COUNT(*) FROM background_job").fetchone()[0] == 0
    finally:
        writer.close()
