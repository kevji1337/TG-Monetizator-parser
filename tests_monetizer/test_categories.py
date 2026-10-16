import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bs4 import BeautifulSoup
from monetizer.categories import load_categories, category_key, category_id_from_key, validate_categories
from monetizer.settings import Settings
from monetizer.search import TGStatSearch, search_tgstat_by_methodology
from monetizer.tgstat_filters import filter_spec
from monetizer.runner import JobRunner
from monetizer.qualification import qualify
from test_qualification import expert
from test_pipeline import FakeSearch


class CategoryFiltersTests(unittest.TestCase):
    def test_actual_tgstat_options(self):
        data = load_categories()
        self.assertEqual(len(data['categories']), 47)
        labels = {c['id']: c['title'] for c in data['categories']}
        self.assertEqual(labels['34'], 'Дизайн')
        self.assertEqual(labels['28'], 'Медицина')
        self.assertEqual(labels['33'], 'Психология')
        self.assertEqual(next(c['title'] for c in data['languages'] if c['id'] == '3'), 'Русский')

    def test_screenshot_preset(self):
        settings = Settings(search_mode='categories', category_ids=['34'])
        spec = filter_spec(settings, '34')
        self.assertEqual(spec['selects'], {'categories': ['34'], 'countries': [], 'languages': ['3'], 'channeltype': ['']})
        self.assertEqual(spec['sliders']['age'], {'from': 12, 'to': 120})
        self.assertEqual(spec['sliders']['err'], {'from': 10, 'to': 100})
        for field in ('er', 'male', 'female'):
            self.assertEqual(spec['sliders'][field]['from'], 0)
        self.assertEqual(spec['text']['q'], '')
        self.assertEqual(spec['text']['participantscountfrom'], '3000')
        self.assertEqual(spec['text']['participantscountto'], '30000')
        self.assertEqual(spec['text']['avgreach24from'], '300')
        for field in ('avgreachfrom', 'avgreachto', 'avgreach24to', 'cifrom', 'cito'):
            self.assertEqual(spec['text'][field], '')

    def test_all_edited_filters_are_serialized(self):
        settings = Settings(country_ids=['1', '3'], language_ids=[], channel_type='public',
                            min_age_months=24, max_age_months=60, min_err=20, max_err=80,
                            min_er=1.5, min_male=25, min_female=50, tgstat_min_avg_reach=400,
                            tgstat_max_avg_reach=2000, max_daily_reach=1500, min_ci=1.5, max_ci=30,
                            exclude_dead=False, exclude_scam=False, exclude_red_label=False)
        spec = filter_spec(settings, '12')
        self.assertEqual(spec['sliders']['age'], {'from': 24, 'to': 60})
        self.assertEqual(spec['sliders']['err'], {'from': 20, 'to': 80})
        self.assertEqual(spec['selects']['countries'], ['1', '3'])
        self.assertEqual(spec['selects']['languages'], [])
        self.assertEqual(spec['text']['avgreach24to'], '1500')
        self.assertEqual(spec['text']['cifrom'], '1.5')
        self.assertFalse(spec['checks']['nodead'])
        self.assertEqual(Settings(**json.loads(json.dumps(settings.to_dict()))), settings)

    def test_invalid_filters_and_category_keys(self):
        for values in ({'min_age_months': 121}, {'min_err': 101}, {'min_er': .3}, {'min_male': 23},
                       {'min_ci': 10, 'max_ci': 5}, {'max_daily_reach': 200}, {'language_ids': '3'},
                       {'search_mode': 'categories'}, {'min_age_months': 20, 'max_age_months': 10}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                Settings(**values)
        for key in ('Дизайн', 'tgstat:design', 'tgstat:../../', 'tgstat:３４'):
            with self.assertRaises(ValueError):
                category_id_from_key(key)
        self.assertEqual(category_id_from_key(category_key('34')), '34')

    def test_legacy_filter_resume_keeps_original_parameters(self):
        saved = {'min_subs': 3000, 'max_subs': 30000}
        restored = Settings.from_saved(saved)
        self.assertEqual((restored.min_age_months, restored.min_err), (0, 0))
        self.assertEqual(restored.language_ids, ())
        self.assertEqual(restored.channel_type, 'public')
        self.assertEqual(Settings.from_saved(Settings().to_dict()), Settings())

    def test_profile_focus_does_not_become_a_site_search(self):
        row = qualify(expert(), category_ids=['34'])
        self.assertEqual(row['status'], 'recommended')
        self.assertEqual(row['analysis_context']['category_ids'], ['34'])
        self.assertEqual(qualify(expert(), category_ids=['28'])['status'], 'rejected')

    def test_form_field_ids_and_slider_boundaries_match_saved_site_markup(self):
        html = (Path(__file__).parent / 'fixtures/tgstat_form.html').read_text(encoding='utf-8')
        soup = BeautifulSoup(html, 'html.parser')
        spec = filter_spec(Settings(), '34')
        for section in ('text', 'selects', 'sliders', 'checks'):
            for field in spec[section]:
                self.assertIsNotNone(soup.find(id=field), field)
        self.assertEqual(soup.find(id='age')['data-max'], '120')
        self.assertEqual(soup.find(id='err')['data-max'], '100')

    def test_compatibility_helper_applies_category_without_words(self):
        with patch('monetizer.search.discover_channels', return_value={}) as discover:
            search_tgstat_by_methodology(category='Дизайн')
            queries, settings = discover.call_args.args
            self.assertEqual(queries, ['tgstat:34'])
            self.assertEqual(settings.category_ids, ('34',))
            self.assertEqual(settings.search_mode, 'categories')
        with self.assertRaises(ValueError):
            search_tgstat_by_methodology(query_word='framer', category='34')


class BrowserFilterTests(unittest.TestCase):
    def search(self):
        search = TGStatSearch(Settings(search_mode='categories', category_ids=['34']))
        search.driver = MagicMock()
        search.driver.execute_script.return_value = []
        search._load_form = MagicMock()
        search._request = MagicMock(return_value=True)
        return search

    def test_reset_uses_empty_query_and_real_category_id(self):
        search = self.search()
        self.assertTrue(search._reset('tgstat:34'))
        spec = search.driver.execute_script.call_args_list[0].args[1]
        self.assertEqual(spec['text']['q'], '')
        self.assertEqual(spec['selects']['categories'], ['34'])
        self.assertEqual(spec['sliders']['age']['from'], 12)
        self.assertIn("data('ionRangeSlider')", search.driver.execute_script.call_args_list[0].args[0])
        search._request.assert_called_once()

    def test_missing_category_option_stops_before_submission(self):
        search = self.search()
        search.driver.execute_script.return_value = ['categories: option missing']
        with self.assertRaises(RuntimeError):
            search._reset('tgstat:34')
        search._request.assert_not_called()

    def test_incorrect_slider_value_stops_before_submission(self):
        search = self.search()
        search.driver.execute_script.side_effect = [[], ['age']]
        with self.assertRaises(RuntimeError):
            search._reset('tgstat:34')
        search._request.assert_not_called()

    def test_wrong_query_is_not_accepted_as_a_category(self):
        search = self.search()
        with self.assertRaises(ValueError):
            search._reset('framer')
        search._request.assert_not_called()

    def test_edited_form_stops_pagination(self):
        search = self.search()
        search.applied_filters = filter_spec(search.settings, '34')
        search.driver.execute_script.return_value = ['q', 'languages']
        with self.assertRaises(RuntimeError):
            TGStatSearch._request(search, MagicMock())
        self.assertEqual(search.driver.execute_script.call_count, 1)

    def test_refresh_reads_options_without_search_submission(self):
        search = self.search()
        search.driver.execute_script.return_value = {
            'categories': [{'id': '34', 'title': 'Дизайн'}],
            'countries': [{'id': '1', 'title': 'Россия'}], 'languages': [{'id': '3', 'title': 'Русский'}]}
        with tempfile.TemporaryDirectory() as folder, patch('monetizer.search.CACHE_PATH', Path(folder) / 'categories.json'):
            result = search.read_categories()
            self.assertEqual(result['categories'][0]['id'], '34')
        search._request.assert_not_called()

    def test_private_only_page_does_not_end_a_mixed_catalog_early(self):
        search = self.search()
        search.current_query, search.current_page = 'tgstat:34', 0
        search._names = MagicMock(return_value=[])
        search._channel_urls = MagicMock(side_effect=[{'private1'}, {'private1'}, {'private1', 'private2'}])
        search._more = MagicMock(return_value=MagicMock())
        response = search.fetch('tgstat:34', 1)
        self.assertFalse(response['exhausted'])
        self.assertEqual(response['usernames'], [])


class CategoryPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runner = JobRunner(self.root / 'checkpoint.json')
        FakeSearch.pages, FakeSearch.calls = {}, []

    def tearDown(self):
        self.temp.cleanup()

    def run_job(self, settings, resume=False):
        with patch('monetizer.runner.TGStatSearch', FakeSearch), patch('monetizer.runner.WebAuditor') as auditor, \
                patch('monetizer.runner.save_reports', return_value=([], [])):
            auditor.return_value.audit.side_effect = lambda name: {**expert(name), **({'status': 'rejected'} if name == 'bad_blog' else {})}
            self.runner.start(settings, niche='Дизайн', resume=resume)
            self.runner.thread.join(10)
        self.assertFalse(self.runner.thread.is_alive())
        return self.runner.snapshot()

    def test_category_scan_continues_after_rejection(self):
        FakeSearch.pages = {('tgstat:34', 0): {'usernames': ['bad_blog', 'good_blog'], 'exhausted': False},
                            ('tgstat:34', 1): {'usernames': ['other_good'], 'exhausted': True}}
        state = self.run_job(Settings(search_mode='categories', category_ids=['34'], target_count=2, workers=1))
        self.assertEqual(state['stop_reason'], 'target_reached')
        self.assertIn(('tgstat:34', 1), FakeSearch.calls)
        self.assertEqual(len(state['rows']), 3)
        self.assertEqual(state['query_stats']['tgstat:34']['qualified'], 2)
        self.assertEqual(state['rows'][0]['source_queries'], '')
        self.assertEqual(state['rows'][0]['source_categories'], 'Дизайн')

    def test_resume_keeps_category_filters_and_unfinished_queue(self):
        FakeSearch.pages = {('tgstat:34', 0): {'usernames': ['good_blog', 'other_good'], 'exhausted': True}}
        state = self.run_job(Settings(search_mode='categories', category_ids=['34'], target_count=2, workers=1, max_audits=1))
        self.assertEqual(state['stop_reason'], 'audit_budget')
        self.runner = JobRunner(self.root / 'checkpoint.json')
        self.assertEqual(self.runner.state['settings']['min_age_months'], 12)
        before = len(FakeSearch.calls)
        state = self.run_job(Settings(search_mode='categories', category_ids=['34'], target_count=2, workers=1, max_audits=3), resume=True)
        self.assertEqual(state['stop_reason'], 'target_reached')
        self.assertEqual(len(FakeSearch.calls), before)

    def test_multiple_categories_deduplicate_channels(self):
        FakeSearch.pages = {('tgstat:34', 0): {'usernames': ['good_blog', 'same_blog'], 'exhausted': True},
                            ('tgstat:11', 0): {'usernames': ['same_blog', 'third_blog'], 'exhausted': True}}
        state = self.run_job(Settings(search_mode='categories', category_ids=['34', '11'], target_count=3, workers=1))
        self.assertEqual(len(state['rows']), 3)
        self.assertEqual(state['query_stats']['tgstat:11']['duplicates'], 1)
        self.assertEqual(state['candidates']['same_blog'], ['tgstat:34', 'tgstat:11'])

    def test_text_cannot_silently_replace_category_search(self):
        with self.assertRaises(ValueError):
            self.runner.start(Settings(search_mode='categories', category_ids=['34']), ['framer'])
        self.assertIsNone(self.runner.thread)
