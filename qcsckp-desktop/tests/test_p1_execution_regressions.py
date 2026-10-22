"""Rule boundary regressions; no real API, messages or application profile."""
import asyncio
from contextlib import ExitStack, asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from services import regulation_rule_runner as runner
from services.product_rule_engine import aggregate_product_rows, evaluate_product_strategy


def trigger(metric, op, value):
    return {"group_combine": "or", "groups": [{"join": "and", "conditions": [
        {"metric": metric, "op": op, "value": value}]}]}


class RuleExecutionRegressions(unittest.TestCase):
    def run_cycle(self, mode):
        target = {"target_uid": "t", "account_uid": "a", "aadvid": "1001", "ad_id": "2001",
                  "promotion_scene": "product", "plan_system": "global", "enabled": True,
                  "monitor_eligible": True, "stop_eligible": True}
        row = {"target_uid": "t", "aadvid": "1001", "ad_id": "2001", "assist_task_id": "3001",
               "promotion_scene": "product", "plan_system": "global", "stat_cost_for_roi2_assist": 200}
        strategy = {"id": "s", "target_uid": "t", "action_mode": mode,
                    "trigger": trigger("stat_cost_for_roi2_assist", "gt", 100)}
        db = Mock(config={})
        db.select_one.side_effect = lambda table, **kw: target if table == "promotion_target" else {"enabled": True}
        svc = Mock(run=AsyncMock(return_value=SimpleNamespace(step="submitted")), close=AsyncMock())

        @asynccontextmanager
        async def lock(*args, **kwargs):
            yield

        def cycle_state(db, target_uid, assist_task_id, *, assist_row=None):
            return {"blocked": False, "cycle_key": "cycle-1"}

        with ExitStack() as stack:
            mocks = {}
            for name, options in {
                "_process_approved_stop_tasks": {"new": AsyncMock()},
                "load_rule_regulation_config": {"return_value": {"enabled": True, "strategies": [strategy]}},
                "DashboardApi": {"return_value": Mock(get_roi2_assist_table_data=Mock(return_value={"success": True, "data": [row]}))},
                "schedulable_promotion_targets": {"return_value": [target]},
                "_shadow_mode_enabled": {"return_value": False},
                "_product_fields_from_assist_row": {"return_value": ("", "")},
                "stop_cycle_state": {"side_effect": cycle_state},
                "_target_assist_sync_ready": {"return_value": (True, "")},
                "check_target_capability": {"return_value": (True, "")},
                "exclusive_browser_operation": {"side_effect": lock},
                "_revalidate_stop_candidate": {"return_value": (target, row, "global", "")},
                "_insert_regulation_run": {},
            }.items():
                mocks[name] = stack.enter_context(patch.object(runner, name, **options))
            stack.enter_context(patch.object(runner.QianChuanRegulationStopService, "from_rule_file_dict", return_value=svc))
            stack.enter_context(patch("services.qianchuan_session.current_session_owner", return_value="test-owner"))
            stack.enter_context(patch("services.qianchuan_session.automation_session_ready", return_value={"ready": True, "session_epoch": 1}))
            stack.enter_context(patch("services.operation_diagnostics.record"))
            card = stack.enter_context(patch("services.local_feishu_bridge.create_local_stop_task", return_value={"success": True, "data": {"delivery_state": "queued"}}))
            asyncio.run(runner.run_one_cycle(db))
        return svc, card, mocks

    def test_auto_stop_reaches_executor_after_final_revalidation(self):
        svc, card, mocks = self.run_cycle("auto_execute")
        svc.run.assert_awaited_once()
        self.assertEqual("3001", svc.run.call_args.kwargs["assist_task_id"])
        self.assertEqual("cycle-1", svc.run.call_args.kwargs["control_cycle_key"])
        self.assertEqual(2, mocks["stop_cycle_state"].call_count)
        mocks["_insert_regulation_run"].assert_not_called()
        card.assert_not_called()

    def test_card_confirmation_does_not_submit_before_approval(self):
        svc, card, _ = self.run_cycle("card_confirm")
        card.assert_called_once()
        svc.run.assert_not_called()

    def test_product_roi_uses_same_cumulative_window(self):
        row = {"id": "m", "productIds": ["p"], "product_ids": ["p"], "currentCost": 1000,
               "costDiff": 10, "overallAmount": 1000, "netAmount": 800, "overallShowCount": 2000}
        product = aggregate_product_rows([row])[0]
        self.assertEqual(1, product["overallPayRoi"])
        self.assertEqual(.8, product["netRoi"])
        self.assertEqual(500, product["estimatedEcpm"])
        self.assertEqual(10, product["costDiff"])
        self.assertEqual([], evaluate_product_strategy([row], {"trigger": trigger("overallPayRoi", "gt", 2)}))

    def test_partial_or_invalid_product_metrics_stay_unknown(self):
        for missing in (None, "", "bad", float("nan"), float("inf")):
            with self.subTest(missing=missing):
                rows = [{"id": "m1", "product_ids": ["p"], "currentCost": 100, "costDiff": 10, "netAmount": 200},
                        {"id": "m2", "product_ids": ["p"], "currentCost": 100, "costDiff": 10, "netAmount": missing}]
                product = aggregate_product_rows(rows)[0]
                self.assertIsNone(product["netAmount"])
                self.assertIsNone(product["netRoi"])
                self.assertEqual([], evaluate_product_strategy(rows, {"trigger": trigger("netRoi", "lte", 2)}))
        zero = aggregate_product_rows([{"id": "m", "product_ids": ["p"], "currentCost": 100, "netAmount": 0}])[0]
        self.assertEqual(0, zero["netRoi"])
