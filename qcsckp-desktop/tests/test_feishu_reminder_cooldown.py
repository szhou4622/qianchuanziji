"""Persisted reminder cooldown, isolated SQLite/clock and no live messages."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest.mock import Mock, patch
import unittest

from services import local_feishu_bridge as bridge
from services.qianchuan_accounts import ensure_qianchuan_account
from tests import test_r19_feishu_delivery as delivery_fixtures
from tests import test_local_feishu_bridge as task_fixtures


class ReminderCooldownTests(unittest.TestCase):
    _stop_payload = delivery_fixtures.FrozenDeliveryTests._stop_payload

    def setUp(self):
        delivery_fixtures.FrozenDeliveryTests.setUp(self)
        self.now = datetime(2026, 10, 22, 12)
        clock = patch.object(bridge, '_now', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        ensure_qianchuan_account('10001', owner_username=self.owner, enabled=True, seen=True, db=self.store)
        self.manager.restart = Mock()

    def payload(self, kind='retarget'):
        if kind == 'stop':
            result = self._stop_payload()
            result['control_cycle_key'] = 'cycle-1'
            return result
        result = task_fixtures.task_payload()
        if kind == 'budget':
            result.update(task_operation='increase_budget', assist_task_id='assist-1',
                          calculation_snapshot={'current_budget_yuan': 100, 'new_budget_yuan': 200},
                          calculation_fingerprint='a' * 64)
        return result

    def create(self, payload):
        result = (bridge.create_local_stop_task(payload) if 'control_cycle_key' in payload
                  else bridge.create_local_retarget_task(payload))
        self.assertTrue(result['success'], result)
        return result

    def reject(self, uid):
        task = bridge._task_row(uid, self.owner)
        result = bridge.handle_local_card_action(self.owner, task_uid=uid, nonce=task['action_nonce'],
                                                action='reject', operator_open_id='personal')
        self.assertTrue(result['success'], result)

    def counts(self):
        return tuple(self.store.execute(f'SELECT COUNT(*) AS n FROM {name}', fetch=True)[0]['n']
                     for name in ('local_retarget_task', 'feishu_outbox'))

    def test_rejection_cools_retarget_stop_and_budget_then_allows_one_new_card(self):
        for kind in ('retarget', 'stop', 'budget'):
            with self.subTest(kind=kind):
                payload = self.payload(kind)
                original = self.create(payload)['data']['task_uid']
                self.reject(original)
                before = self.counts()
                terminal = bridge._task_row(original)
                self.now += timedelta(minutes=59)
                suppressed = self.create(payload)
                self.assertTrue(suppressed['suppressed'])
                self.assertTrue(suppressed['duplicate'])
                self.assertEqual('cooldown', suppressed['data']['delivery_state'])
                self.assertEqual(original, suppressed['data']['task_uid'])
                self.assertEqual(60, suppressed['data']['cooldown_remaining_seconds'])
                self.assertEqual(before, self.counts())
                self.assertEqual(terminal, bridge._task_row(original))
                self.now += timedelta(minutes=1)
                renewed = self.create(payload)
                self.assertFalse(renewed['duplicate'])
                self.assertNotEqual(original, renewed['data']['task_uid'])
                self.assertEqual(before[0] + 1, self.counts()[0])

    def test_expiry_uses_original_deadline_not_cleanup_time(self):
        for kind in ('retarget', 'stop', 'budget'):
            with self.subTest(kind=kind):
                payload = self.payload(kind)
                uid = self.create(payload)['data']['task_uid']
                expires = bridge._parse_dt(bridge._task_row(uid)['expires_at'])
                self.now = expires + timedelta(minutes=20)
                result = self.create(payload)
                self.assertTrue(result['suppressed'])
                self.assertEqual(bridge._dt(expires + timedelta(minutes=60)), result['data']['cooldown_until'])
                self.assertEqual('expired', bridge._task_row(uid)['status'])
                self.now = expires + timedelta(minutes=60)
                self.assertFalse(self.create(payload)['duplicate'])

    def test_offline_past_expiry_and_cooldown_does_not_start_another_wait(self):
        payload = self.payload()
        uid = self.create(payload)['data']['task_uid']
        self.now += timedelta(hours=3)
        result = self.create(payload)
        self.assertFalse(result['duplicate'])
        self.assertNotEqual(uid, result['data']['task_uid'])

    def test_restart_and_repeated_checks_do_not_extend_cooldown(self):
        payload = self.payload()
        uid = self.create(payload)['data']['task_uid']
        self.reject(uid)
        expected = bridge._dt(self.now + timedelta(minutes=60))
        before = self.counts()
        for _ in range(3):
            self.now += timedelta(minutes=10)
            self.actual = bridge.LocalFeishuBridge(self.owner)
            self.actual.status = Mock(return_value={'connected': True})
            result = self.create(payload)
            self.assertEqual(expected, result['data']['cooldown_until'])
            self.assertEqual(before, self.counts())

    def test_config_can_shorten_extend_and_disable_without_rewriting_history(self):
        payload = self.payload()
        uid = self.create(payload)['data']['task_uid']
        self.reject(uid)
        original = bridge._task_row(uid)
        for minutes in (5, 120):
            bridge._update_profile(self.owner, {'reminder_cooldown_minutes': minutes})
            result = self.create(payload)
            self.assertEqual(bridge._dt(self.now + timedelta(minutes=minutes)), result['data']['cooldown_until'])
        bridge._update_profile(self.owner, {'reminder_cooldown_minutes': 0})
        self.assertFalse(self.create(payload)['duplicate'])
        self.assertEqual(original, bridge._task_row(uid))

    def test_active_card_wins_over_older_rejection_after_config_change(self):
        payload = self.payload()
        self.reject(self.create(payload)['data']['task_uid'])
        bridge._update_profile(self.owner, {'reminder_cooldown_minutes': 0})
        current = self.create(payload)['data']['task_uid']
        bridge._update_profile(self.owner, {'reminder_cooldown_minutes': 60})
        result = self.create(payload)
        self.assertTrue(result['duplicate'])
        self.assertFalse(result.get('suppressed', False))
        self.assertEqual(current, result['data']['task_uid'])
        self.assertEqual('pending', result['data']['status'])

    def test_retarget_scope_separates_plan_strategy_operation_and_budget_task(self):
        payload = self.payload()
        self.reject(self.create(payload)['data']['task_uid'])
        for changes in ({'strategy_id': 'other-strategy'}, {'target_uid': 'other-plan', 'ad_id': '20003'}):
            self.assertFalse(self.create({**payload, **changes})['duplicate'])
        budget = self.payload('budget')
        self.reject(self.create(budget)['data']['task_uid'])
        self.assertTrue(self.create(budget)['suppressed'])
        self.assertFalse(self.create({**budget, 'assist_task_id': 'assist-2'})['duplicate'])

    def test_stop_shares_rejection_across_strategies_but_not_control_cycles(self):
        payload = self.payload('stop')
        self.reject(self.create(payload)['data']['task_uid'])
        self.assertTrue(self.create({**payload, 'strategy_id': 'another-stop-rule'})['suppressed'])
        for changes in ({'control_cycle_key': 'cycle-2'}, {'assist_task_id': 'another-task'},
                        {'regulation_stop_action': 'delete'}):
            self.assertFalse(self.create({**payload, **changes})['duplicate'])

    def test_other_account_history_and_unrelated_terminal_states_do_not_suppress(self):
        payload = self.payload()
        for status in ('succeeded', 'failed', 'invalidated', 'cancelled', 'unknown_requires_review'):
            uid = self.create(payload)['data']['task_uid']
            self.store.update('local_retarget_task', {'status': status, 'active_dedupe_key': None,
                              'finished_at': bridge._dt(self.now)}, where={'task_uid': uid})
        uid = self.create(payload)['data']['task_uid']
        self.reject(uid)
        self.store.update('local_retarget_task', {'account_username': 'another-owner'}, where={'task_uid': uid})
        self.assertFalse(self.create(payload)['duplicate'])

    def test_concurrent_creation_after_cooldown_expiry_queues_exactly_one_card(self):
        payload = self.payload()
        self.reject(self.create(payload)['data']['task_uid'])
        before = self.counts()
        self.now += timedelta(minutes=60)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.create(payload), range(4)))
        self.assertEqual(1, sum(not r['duplicate'] for r in results))
        self.assertEqual(1, len({r['data']['task_uid'] for r in results}))
        self.assertEqual((before[0] + 1, before[1] + 1), self.counts())

    def test_configuration_validates_preserves_omitted_values_and_is_account_scoped(self):
        bridge._update_profile(self.owner, {'app_id': 'fixture-app', 'app_secret_protected': 'fixture-cipher'})
        self.assertEqual(60, bridge._profile_for(self.owner)['reminder_cooldown_minutes'])
        config = {'app_id': 'fixture-app', 'reminder_cooldown_minutes': '120'}
        self.assertTrue(bridge.save_local_feishu_config(config)['success'])
        self.assertEqual(120, bridge._profile_for(self.owner)['reminder_cooldown_minutes'])
        self.assertEqual(120, bridge.LocalFeishuBridge.status(self.actual)['profile']['reminder_cooldown_minutes'])
        self.assertTrue(bridge.save_local_feishu_config({'app_id': 'fixture-app'})['success'])
        self.assertEqual(120, bridge._profile_for(self.owner)['reminder_cooldown_minutes'])
        before = copy.deepcopy(bridge._load_profiles())
        for invalid in (-1, 10081, True, None, 1.5, '1.5', 'nan', '--1', {}, ''):
            with self.subTest(invalid=invalid):
                self.assertFalse(bridge.save_local_feishu_config({**config, 'reminder_cooldown_minutes': invalid})['success'])
                self.assertEqual(before, bridge._load_profiles())
        self.assertEqual(60, bridge._profile_for('other-owner')['reminder_cooldown_minutes'])
        bridge._update_profile(self.owner, {'reminder_cooldown_minutes': 'bad-hand-edit'})
        self.assertEqual(60, bridge._profile_for(self.owner)['reminder_cooldown_minutes'])

    def test_legacy_history_without_new_metadata_still_cools_and_preview_is_independent(self):
        payload = self.payload()
        uid = self.create(payload)['data']['task_uid']
        self.reject(uid)
        self.assertNotIn('reminder_cooldown_minutes', json.loads(bridge._task_row(uid)['payload_json']))
        self.assertTrue(self.create(payload)['suppressed'])
        self.assertFalse(self.create({**payload, 'preview_only': True})['duplicate'])
