import unittest

from monetizer.audit import parse_channel_html, reassess_metrics
from monetizer.settings import Settings
from monetizer.view_metrics import assess_average_views, assess_post_views
from test_core import NOW, preview


class AveragePercentTests(unittest.TestCase):
    def test_percentage_uses_subscriber_count_and_exact_boundary(self):
        for subs, mean, rejected in [(4000, 400, False), (20000, 400, True),
                                      (4999, 499, True), (3000, 300, False)]:
            row = {'subs': subs, 'avg_views_exact': mean, 'eligible_posts': 6, 'view_ratio_exact': 100}
            failures, uncertain = assess_average_views(row, Settings())
            self.assertEqual(bool(failures), rejected)
            self.assertFalse(uncertain)
            self.assertAlmostEqual(row['view_ratio_exact'], mean / subs * 100)

    def test_legacy_settings_preserve_units_without_converting_300_to_percent(self):
        legacy = Settings.from_saved({'min_avg_views': 300})
        self.assertIsNone(legacy.min_avg_view_ratio)
        self.assertEqual(legacy.min_avg_views, 300)
        self.assertEqual(Settings().min_avg_view_ratio, 10)
        self.assertEqual(Settings().min_avg_views, 0)
        row = {'subs': 20000, 'avg_views_exact': 400, 'eligible_posts': 6}
        self.assertFalse(assess_average_views(row, legacy)[0])
        self.assertTrue(assess_average_views(row, Settings())[0])

    def test_average_and_day_old_post_checks_are_independent(self):
        html = preview(subs='5K', views=100)
        html = html.replace('>100</span>', '>500</span>', 1)
        row = parse_channel_html(html, 'expert_blog', now=NOW)
        self.assertEqual(row['activity_status'], 'pass')
        self.assertEqual(row['status'], 'rejected')
        self.assertIn('Средние просмотры ниже 10%', row['reasons'])
        result = reassess_metrics(row, Settings(min_avg_view_ratio=3), now=NOW)
        self.assertEqual(result['metric_status'], 'top')

    def test_missing_denominator_and_small_sample_need_review(self):
        for subs in (None, 0):
            failures, uncertain = assess_average_views({'subs': subs, 'avg_views_exact': 500, 'eligible_posts': 6}, Settings())
            self.assertFalse(failures)
            self.assertTrue(uncertain)
        self.assertTrue(assess_average_views({'subs': 5000, 'avg_views_exact': 500, 'eligible_posts': 1}, Settings())[1])
        self.assertEqual(assess_average_views({'subs': 5000, 'avg_views_exact': 1, 'eligible_posts': 6}, Settings(min_avg_view_ratio=0)), ([], []))

    def test_invalid_percent_and_saved_preferences(self):
        from monetizer.preferences import last_run_preferences, widget_key
        for value in (-1, 101, float('nan'), float('inf'), True, '10'):
            with self.assertRaises(ValueError):
                Settings(min_avg_view_ratio=value)
        key = widget_key('number_input', 'Средние просмотры от подписчиков, %')
        self.assertNotIn(key, last_run_preferences({'settings': {'min_avg_views': 300}}))
        self.assertEqual(last_run_preferences({'settings': Settings(min_avg_view_ratio=12.5).to_dict()})[key], 12.5)


class PostActivityTests(unittest.TestCase):
    def test_ten_percent_is_uniform_for_small_and_larger_channels(self):
        for subs, views in [('4K', 400), ('5K', 500), ('20K', 2000)]:
            row = parse_channel_html(preview(subs=subs, views=views), 'expert_blog', now=NOW)
            self.assertEqual(row['norm'], 10)
            self.assertEqual(row['activity_status'], 'pass')
            self.assertEqual(row['activity_ratio'], 10)
            self.assertEqual(row['status'], 'top')

    def test_default_window_is_18_to_30_hours_inclusive(self):
        for hours in [18, 23, 24, 30]:
            row = parse_channel_html(preview(subs='5K', views=500, days=[hours / 24, 2, 3, 4, 5, 6]), 'expert_blog', now=NOW)
            self.assertEqual(row['activity_status'], 'pass')
            self.assertEqual(row['activity_post_age_hours'], hours)
        for hours in [0, 1, 17.99, 30.01, 48]:
            row = parse_channel_html(preview(subs='5K', views=5000, days=[hours / 24, 3, 4, 5, 6, 7]), 'expert_blog', now=NOW)
            self.assertEqual(row['activity_status'], 'unknown')
            self.assertNotIn(row['status'], ('top', 'recommended'))

    def test_choose_closest_age_instead_of_highest_views(self):
        html = preview(subs='5K', views=5000, days=[23 / 24, 28 / 24, 2, 3, 4, 5])
        html = html.replace('>5000</span>', '>100</span>', 1)
        row = parse_channel_html(html, 'expert_blog', now=NOW)
        self.assertGreater(row['view_ratio'], 10)
        self.assertEqual(row['activity_post_age_hours'], 23)
        self.assertEqual(row['activity_ratio'], 2)
        self.assertEqual(row['activity_status'], 'below')
        self.assertEqual(row['status'], 'rejected')

    def test_threshold_is_not_rounded_up(self):
        for subs, views in [('4,999', 499), ('29,999', 2999)]:
            row = parse_channel_html(preview(subs=subs, views=views), 'expert_blog', now=NOW)
            self.assertEqual(row['activity_status'], 'below')
            self.assertLess(row['activity_ratio'], 10)
            self.assertEqual(row['status'], 'rejected')
            self.assertIn('ниже 10%', row['reasons'])

    def test_tie_uses_older_post_consistently(self):
        row = parse_channel_html(preview(days=[22 / 24, 26 / 24, 2, 3, 4, 5]), 'expert_blog', now=NOW)
        self.assertEqual(row['activity_post_age_hours'], 26)
        self.assertTrue(row['activity_post_url'].endswith('/1'))

    def test_cached_observation_recalculates_age_and_ratio_without_network(self):
        row = parse_channel_html(preview(subs='5K', views=500), 'expert_blog', now=NOW)
        assess_post_views(row, Settings(target_post_age_hours=12, post_age_tolerance_hours=2))
        self.assertEqual(row['activity_status'], 'unknown')
        assess_post_views(row, Settings(min_post_view_ratio=11))
        self.assertEqual(row['activity_status'], 'below')
        assess_post_views(row, Settings())
        self.assertEqual(row['activity_status'], 'pass')
        self.assertEqual(row['activity_observed_at'], NOW.isoformat())
        self.assertEqual(row['activity_views'], 500)
        self.assertEqual(row['activity_subscribers'], 5000)

    def test_legacy_average_does_not_invent_a_post_measurement(self):
        row = parse_channel_html(preview(), 'expert_blog', now=NOW)
        row.pop('post_observations')
        recalculated = reassess_metrics(row, Settings(), now=NOW)
        self.assertEqual(recalculated['activity_status'], 'unknown')
        self.assertEqual(recalculated['metric_status'], 'review')
        self.assertIsNone(recalculated['activity_ratio'])

    def test_missing_counter_on_closest_post_is_not_replaced_with_a_better_post(self):
        html = preview(subs='5K', views=5000, days=[23 / 24, 28 / 24, 2, 3, 4, 5])
        html = html.replace('class="tgme_widget_message_views"', 'class="missing"', 1)
        row = parse_channel_html(html, 'expert_blog', now=NOW)
        self.assertEqual(row['activity_post_age_hours'], 23)
        self.assertEqual(row['activity_status'], 'unknown')
        self.assertIsNone(row['activity_ratio'])

    def test_invalid_age_settings(self):
        for kwargs in [{'target_post_age_hours': 0}, {'post_age_tolerance_hours': -1},
                       {'post_age_tolerance_hours': 25}, {'min_post_view_ratio': 0}]:
            with self.assertRaises(ValueError):
                Settings(**kwargs)

    def test_missing_legacy_counter_cannot_bypass_activity_check(self):
        row = parse_channel_html(preview(), 'expert_blog', now=NOW)
        row.pop('post_observations')
        row.pop('eligible_posts')
        recalculated = reassess_metrics(row, Settings(), now=NOW)
        self.assertEqual(recalculated['activity_status'], 'unknown')
        self.assertEqual(recalculated['metric_status'], 'review')
