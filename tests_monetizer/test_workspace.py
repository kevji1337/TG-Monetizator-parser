import copy
import importlib
import json
import tempfile
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from monetizer.repository import Repository
from monetizer.settings import Settings
from monetizer.workspace import Workspace, economics, distribution, topic_stability, reminder_calendar, search_eta, funnel
from monetizer.ai_audit import effective_settings, cache_key, request_data
from monetizer.experiments import preview_request
from monetizer.calibration import scores, propose_threshold
from monetizer.qualification import qualify
from test_qualification import expert


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Repository(Path(self.temp.name) / 'test.sqlite3')
        self.store = Workspace(self.repo)

    def seed(self):
        row = qualify(expert(), micro_id='motion_design')
        row['metric_status'] = 'recommended'
        self.repo.save_state({'run_id': 'test', 'settings': Settings().to_dict(), 'rows': [row]})
        return row

    def test_candidate_data_survives_public_snapshot_and_restart(self):
        row = self.seed()
        self.store.save('candidate', 'expert_blog', {'tags': ['Личный эксперт'], 'collections': ['Сентябрь'],
                        'deal_stage': 'agreement', 'economics': {'price': 100, 'leads': 20, 'conversion': 10, 'share': 50, 'costs': 20}})
        self.repo.save_state({'run_id': 'test', 'rows': [{**row, 'title': 'Новое название'}]})
        saved = Repository(self.repo.path).list_channels()[0]
        self.assertEqual(saved['custom_tags'], ['Личный эксперт'])
        self.assertEqual(saved['deal_stage'], 'agreement')
        self.assertEqual(saved['partnership_economics']['partner_forecast'], 80)

    def test_workspace_preset_view_draft_roundtrip_and_secret_rejection(self):
        for kind in ('preset', 'view', 'draft'):
            self.store.save(kind, 'name', {'tgstat_category_ids': ['34'], 'target': 10})
            self.assertEqual(Workspace(self.repo).get(kind, 'name')['target'], 10)
            self.store.delete(kind, 'name')
            self.assertNotIn('name', self.store.list(kind))
        with self.assertRaises(ValueError):
            self.store.save('preset', 'bad', {'proxy_api_key': 'secret'})

    def test_invalid_business_numbers_are_atomic(self):
        self.store.save('candidate', 'expert_blog', {'tags': ['old']})
        for values in ({'share': 101}, {'price': -1}, {'costs': float('nan')}):
            with self.assertRaises(ValueError):
                self.store.save('candidate', 'expert_blog', {'tags': ['new'], 'economics': values})
        self.assertEqual(self.store.get('candidate', 'expert_blog')['tags'], ['old'])

    def test_view_outlier_has_link_without_fraud_verdict(self):
        row = expert()
        base = row['post_observations'][0]
        row['post_observations'] = [{**base, 'id': f'expert_blog/{i}', 'views': 500} for i in range(6)]
        row['post_observations'].append({**base, 'id': 'expert_blog/99', 'views': 10000})
        report = distribution(row)
        self.assertEqual(len(report['outliers']), 1)
        self.assertTrue(report['outliers'][0]['url'].endswith('/99'))
        self.assertNotIn('status', row.get('view_distribution', {}))

    def test_topic_stability_uses_own_posts_and_separates_time_halves(self):
        row = qualify(expert(), micro_id='motion_design')
        row['content_samples'] *= 3
        report = topic_stability(row)
        self.assertEqual(report['coverage'], 100)
        row['content_samples'] = [{**p, 'forwarded': True} for p in row['content_samples']]
        self.assertEqual(topic_stability(row)['posts'], [])

    def test_eta_does_not_extrapolate_without_enough_successes(self):
        self.assertIsNone(search_eta({'rows': [], 'elapsed_seconds': 100})['minutes'])
        result = search_eta({'rows': [{'status': 'recommended'}] * 10, 'elapsed_seconds': 600, 'settings': {'target_count': 20}})
        self.assertEqual(result['minutes'], 10)

    def test_calendar_alarm_escapes_text_and_folds_utf8_lines(self):
        result = reminder_calendar([{'username': '@expert_blog', 'next_contact_date': '2026-11-01',
                         'next_action_label': 'Позвонить', 'contact_plan_note': 'Тема\n' + 'Длинное пояснение; ' * 20}], 30)
        self.assertIn('BEGIN:VALARM', result)
        self.assertIn('TRIGGER:-PT30M', result)
        self.assertIn('DTSTART:20261101T090000', result)
        self.assertTrue(all(len(line.encode('utf-8')) <= 75 for line in result.split('\r\n')))
        with self.assertRaises(ValueError):
            reminder_calendar([], -1)

    def test_funnel_counts_history_even_when_status_changes_again(self):
        self.seed()
        self.repo.save_crm('expert_blog', 'contacted', '')
        self.repo.save_crm('expert_blog', 'replied', '')
        self.repo.save_crm('expert_blog', 'declined', '')
        report = funnel(self.repo, self.store)
        self.assertEqual(report['Ответили, когда-либо'], 1)
        self.assertEqual(report['Доля ответов, %'], 100)

    def test_models_are_selected_by_explicit_stage_and_cache_keys_differ(self):
        row = self.seed()
        one = Settings(ai_screen_model='screen-model', ai_deep_model='deep-model', ai_analysis_stage='screen')
        two = Settings(ai_screen_model='screen-model', ai_deep_model='deep-model', ai_analysis_stage='deep')
        data = request_data(row, one, 'Дизайн', {})
        self.assertEqual(effective_settings(one).ai_model, 'screen-model')
        self.assertNotEqual(cache_key(data, one), cache_key(data, two))
        self.assertEqual(preview_request(row, one, 'Дизайн', {})['model'], 'screen-model')

    def test_calibration_does_not_invent_training_data(self):
        self.assertFalse(propose_threshold(self.repo)['ready'])
        self.assertEqual(scores([(5, False), (15, True)], 10)['f1'], 1)

    def test_calibration_uses_human_labels_and_a_separate_holdout(self):
        from monetizer.evaluation import review_set, save_labels
        rows = []
        for index in range(40):
            row = qualify(expert('expert_' + str(index)), micro_id='motion_design')
            row['activity_ratio'] = 20. if index % 2 else 5.
            rows.append(row)
        self.repo.save_state({'run_id': 'train', 'settings': Settings().to_dict(), 'rows': rows})
        packet = review_set(self.repo)
        for item in packet['channels']:
            positive = bool(int(item['channel_id'].split('_')[-1]) % 2)
            item['label'] = {'qualified': positive, 'channel_type': 'expert', 'relevance': 'matched',
                             'annotator': 'operator', 'note': 'Controlled example'}
        save_labels(self.repo, packet)
        report = propose_threshold(self.repo)
        self.assertTrue(report['ready'])
        self.assertEqual(report['train_count'] + report['holdout_count'], 40)
        self.assertEqual(report['holdout_count'], 8)
        self.assertEqual(report['holdout']['f1'], 1)

    def test_repair_cannot_change_unselected_fields(self):
        from monetizer.ai_audit import ProxyAuditor
        from monetizer.web2api import ChatReply
        row = self.seed()
        settings = Settings()
        base = {'channel_id': 'expert_blog', 'input_hash': '', 'channel_type': 'unknown', 'relevance': 'unknown',
                'reason': 'Original', 'evidence': [], 'offers': [], 'message': ''}
        experiment = {'kind': 'repair', 'base': base, 'fields': ['offers']}
        data = request_data(row, settings, '', {})
        data['experiment'] = experiment
        decoded = {**base, 'reason': 'Changed', 'input_hash': cache_key(data, settings)}
        info = {'models_http': 200, 'models_checked': False, 'models': [], 'version': 'local', 'notes': [],
                'health_ok': True, 'auth_checked': True}
        auditor = ProxyAuditor(self.repo, settings, 'dummy', threading.Event(), lambda line: None)
        with patch('monetizer.ai_audit.inspect_proxy', return_value=info), \
                patch('monetizer.ai_audit.chat_completion', return_value=ChatReply(json.dumps(decoded))):
            result = auditor.audit(row, 'repair', '', {}, experiment=experiment)
        self.assertEqual(result['ai_audit_status'], 'error')
        self.assertIn('вне выбранного списка', result['ai_detail'])
        self.assertNotIn('api_ai_review', result)

    def test_ai_experiments_share_quota_and_never_overwrite_channel(self):
        from monetizer.runner import JobRunner
        from monetizer.experiments import run_experiment
        from monetizer.web2api import ChatReply
        runner = JobRunner(Path(self.temp.name) / 'checkpoint.json', self.repo.path)
        row = self.seed()
        runner.state.update(run_id='test', rows=[row], status='completed', settings=Settings(ai_max_audits=1).to_dict())
        info = {'models_http': 200, 'models_checked': True, 'models': ['one', 'two'], 'version': 'local', 'notes': [],
                'health_ok': True, 'auth_checked': True}
        with patch('monetizer.ai_audit.inspect_proxy', return_value=info), patch('monetizer.ai_audit.chat_completion', return_value=ChatReply('invalid')) as api:
            result = run_experiment(runner, row, 'dummy', 'models', ['one', 'two'])
        self.assertEqual(api.call_count, 1)
        self.assertEqual(result['results'][1]['status'], 'budget')
        self.assertEqual(runner.state['rows'], [row])
        self.assertEqual(len(self.store.list('experiment')), 1)

    def test_old_ui_signature_is_repaired_without_restart(self):
        from monetizer import preferences, live_reload
        class OldUI:
            def __init__(self, preferences, target=None):
                pass
        with patch.object(preferences, 'PersistentUI', OldUI):
            module, _ = live_reload.ui_api()
            self.assertIn('disabled', __import__('inspect').signature(module.PersistentUI).parameters)
        importlib.reload(preferences)

    def test_code_generation_reimports_classes_without_mutating_old_objects(self):
        script = '''
from monetizer import preferences, runtime
from monetizer.live_reload import refresh_code
old_class = preferences.PersistentUI
registry = runtime.shared_state()
refresh_code()
from monetizer.preferences import PersistentUI
from monetizer.runtime import shared_state
assert PersistentUI is not old_class
assert preferences.PersistentUI is old_class
assert shared_state() is registry
'''
        result = subprocess.run([sys.executable, '-X', 'utf8', '-c', script],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_background_experiment_can_be_cancelled_without_ui_calls(self):
        from monetizer.experiments import start_experiment, experiment_job, experiment_active
        entered = threading.Event()
        def fake(*args):
            entered.set()
            args[-1].wait(5)
            return {'stopped': args[-1].is_set()}
        runner = SimpleNamespace(repo=self.repo)
        with patch('monetizer.experiments.run_experiment', side_effect=fake):
            future = start_experiment(runner, {'username': '@expert_blog'}, 'dummy', 'models', ['one', 'two'])
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(experiment_active(self.repo))
            finally:
                experiment_job(self.repo)['stop'].set()
                self.assertEqual(future.result(timeout=3), {'stopped': True})
        self.assertFalse(experiment_active(self.repo))

    def test_blocklist_is_applied_to_new_manual_search(self):
        from monetizer.runner import JobRunner
        runner = JobRunner(Path(self.temp.name) / 'checkpoint.json', self.repo.path)
        self.store.save('blocklist', 'channels', ['blocked_blog'])
        with patch('threading.Thread.start'):
            runner.start(Settings(search_mode='manual'), usernames=['blocked_blog', 'allowed_blog'])
        try:
            self.assertEqual(runner.state['queue'], ['allowed_blog'])
        finally:
            runner.guard.__exit__()

    def test_query_weights_apply_after_every_category_has_an_initial_portion(self):
        from monetizer.runner import JobRunner
        runner = JobRunner(Path(self.temp.name) / 'checkpoint.json', self.repo.path)
        queries = ['tgstat:34', 'tgstat:53']
        runner.state.update(queries=queries, settings=Settings(category_weights=(('34', 10), ('53', 1))).to_dict(),
                            query_state={q: {'buffer': [], 'exhausted': False, 'served': 1} for q in queries},
                            query_stats={q: {'qualified': 0, 'audited': 10} for q in queries})
        self.assertEqual(runner._pick_query({}), 'tgstat:34')
        runner.state['query_state']['tgstat:53']['served'] = 0
        self.assertEqual(runner._pick_query({}), 'tgstat:53')
