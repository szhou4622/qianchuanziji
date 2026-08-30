import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from commercial_v1.qianchuan.fair_hot_scheduler import (
    AdvertiserConcurrencyGate,
    FairHotCollectionScheduler,
)
from commercial_v1.runtime.jobs import ClaimedJob
from commercial_v1.storage.database import Database, DatabaseConfig
from commercial_v1.storage.schema import create_schema_v1
from commercial_v1.storage.writer import StorageWriter


def _db(tmp_path: Path):
    db = Database(DatabaseConfig(tmp_path / "runtime.db"))
    with db.connect() as conn:
        create_schema_v1(conn)
    writer = StorageWriter(db)
    writer.start()
    return db, writer


def test_100_plans_are_layered_by_account_before_second_round(tmp_path: Path) -> None:
    db, writer = _db(tmp_path)
    try:
        t = "2026-08-30T07:00:00+00:00"
        def seed(conn):
            for account_index in range(10):
                advertiser_id = str(100000 + account_index)
                account_uid = f"acc-{advertiser_id}"
                conn.execute(
                    "INSERT INTO qianchuan_account(account_uid,advertiser_id,enabled,auth_status,created_at,updated_at) VALUES(?,?,1,'ACTIVE',?,?)",
                    (account_uid, advertiser_id, t, t),
                )
                for plan_index in range(10):
                    ad_id = str(200000 + account_index * 100 + plan_index)
                    target_uid = f"target-{advertiser_id}-{plan_index:02d}"
                    conn.execute(
                        """INSERT INTO monitor_plan(
                           target_uid,account_uid,advertiser_id,ad_id,plan_system,promotion_scene,official_status,
                           monitor_enabled,lifecycle_state,collection_active,strategy_eligible,write_eligible,
                           sync_state,next_hot_collect_at,created_at,updated_at)
                           VALUES(?,?,?,?, 'UNI_PROJECT','VIDEO_PROM_GOODS','DELIVERY_OK',1,
                                  'ACTIVE_COLLECTING',1,1,1,'TRUSTED',?,?,?)""",
                        (target_uid, account_uid, advertiser_id, ad_id, t, t, t),
                    )
        writer.transaction(seed).result(timeout=10)

        scheduler = FairHotCollectionScheduler(db, writer)
        result = scheduler.run_once(now=datetime(2026,8,30,7,0,10,tzinfo=timezone.utc))
        assert result["enqueued"] == 200
        assert result["skipped_overlap"] == 0

        with db.connect(readonly=True) as conn:
            jobs = conn.execute(
                "SELECT priority,payload_json FROM background_job ORDER BY priority,job_uid"
            ).fetchall()
        assert len(jobs) == 200

        # 每个计划有 Material + Control 两条 Job，所以第一层共 10×2=20 条，必须覆盖 10 个账户。
        first_layer = [row for row in jobs if int(row["priority"]) < 60]
        assert len(first_layer) == 20
        first_accounts = {
            json.loads(row["payload_json"])["target_uid"].split("-")[1]
            for row in first_layer
        }
        assert len(first_accounts) == 10

        # 第二层 priority 从 60 开始，证明任一账户的第二计划不会插到其他账户第一计划前面。
        priorities = sorted({int(row["priority"]) for row in jobs})
        assert min(priorities) == 40
        assert any(value >= 60 for value in priorities)
    finally:
        writer.close()


def test_advertiser_gate_never_allows_more_than_two_parallel_handlers(tmp_path: Path) -> None:
    db, writer = _db(tmp_path)
    try:
        t = "2026-08-30T07:00:00+00:00"
        writer.execute(
            "INSERT INTO qianchuan_account(account_uid,advertiser_id,enabled,auth_status,created_at,updated_at) VALUES('acc','111111',1,'ACTIVE',?,?)",
            (t,t),
        ).result(timeout=5)
        writer.execute(
            """INSERT INTO monitor_plan(
               target_uid,account_uid,advertiser_id,ad_id,plan_system,promotion_scene,official_status,
               monitor_enabled,lifecycle_state,collection_active,strategy_eligible,write_eligible,sync_state,
               created_at,updated_at)
               VALUES('target','acc','111111','222222','UNI_PROJECT','VIDEO_PROM_GOODS','DELIVERY_OK',1,
                      'ACTIVE_COLLECTING',1,1,1,'TRUSTED',?,?)""",
            (t,t),
        ).result(timeout=5)

        gate = AdvertiserConcurrencyGate(db, max_per_advertiser=2)
        release = threading.Event()
        two_entered = threading.Event()
        lock = threading.Lock()
        entered = 0

        def handler(_job):
            nonlocal entered
            with lock:
                entered += 1
                if entered >= 2:
                    two_entered.set()
            release.wait(timeout=3)
            return {"ok": True}

        guarded = gate.wrap(handler)
        job = ClaimedJob(
            job_uid="j",job_type="MATERIAL_5M",priority=40,
            payload={"target_uid":"target"},due_at=t,owner_instance_id="w",
            fencing_token=1,lease_expires_at="2026-08-30T07:10:00+00:00",
        )
        threads = [threading.Thread(target=guarded,args=(job,)) for _ in range(6)]
        for thread in threads:
            thread.start()
        assert two_entered.wait(timeout=2)
        snapshot = gate.snapshot()
        assert snapshot["active"]["111111"] == 2
        assert snapshot["peak"]["111111"] == 2
        release.set()
        for thread in threads:
            thread.join(timeout=3)
            assert not thread.is_alive()
        assert gate.snapshot()["peak"]["111111"] == 2
    finally:
        writer.close()
