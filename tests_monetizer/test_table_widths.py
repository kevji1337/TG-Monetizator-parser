import copy
import unittest

import pandas as pd
import streamlit as st

from monetizer.ui_tables import table_layout, candidate_columns, display_text, text_pixels
from monetizer.export import COLUMNS


class TableWidthTests(unittest.TestCase):
    def test_longer_content_grows_column_up_to_the_cap(self):
        short = pd.DataFrame({'Название': ['Анна']})
        long = pd.DataFrame({'Название': ['Анна — практикующий дизайнер интерфейсов']})
        huge = pd.DataFrame({'Название': ['Ж' * 1000]})
        one, _ = table_layout(short)
        two, _ = table_layout(long)
        three, _ = table_layout(huge)
        self.assertLess(one['Название']['width'], two['Название']['width'])
        self.assertEqual(three['Название']['width'], 320)
        self.assertEqual(table_layout(huge, max_width=200)[0]['Название']['width'], 200)

    def test_header_is_sized_even_for_empty_cells(self):
        config, _ = table_layout(pd.DataFrame({'Длинный заголовок колонки': [None]}))
        self.assertGreater(config['Длинный заголовок колонки']['width'], 80)

    def test_numbers_use_formatted_text_and_have_smaller_caps(self):
        frame = pd.DataFrame({'Процент': [9.999999999999], 'Приоритет': [100]})
        base = {'Процент': st.column_config.NumberColumn('Процент', format='%.1f%%'),
                'Приоритет': st.column_config.NumberColumn('Приоритет', format='%d')}
        config, _ = table_layout(frame, base)
        self.assertEqual(display_text(frame['Процент'][0], base['Процент']), '10.0%')
        self.assertLessEqual(config['Процент']['width'], 160)
        self.assertLessEqual(config['Приоритет']['width'], 128)

    def test_links_measure_display_label_instead_of_url(self):
        config = {'URL': st.column_config.LinkColumn('Пост', display_text='Открыть ↗')}
        short = table_layout(pd.DataFrame({'URL': ['https://t.me/a/1']}), config)[0]
        long = table_layout(pd.DataFrame({'URL': ['https://example.com/' + 'x' * 1000]}), config)[0]
        self.assertEqual(short['URL']['width'], long['URL']['width'])
        self.assertLessEqual(long['URL']['width'], 140)

    def test_visible_columns_only_determine_total_width(self):
        frame = pd.DataFrame({'Первый': ['Коротко'], 'Скрытый': ['О' * 500]})
        config, width = table_layout(frame, column_order=['Первый'])
        self.assertEqual(width, config['Первый']['width'])
        self.assertLess(width, sum(v['width'] for v in config.values()))

    def test_values_and_column_properties_are_preserved(self):
        value = 'Полный длинный текст ' * 50
        frame = pd.DataFrame({COLUMNS['username']: ['@expert_blog'], COLUMNS['reasons']: [value]})
        original = frame.copy(deep=True)
        base = candidate_columns()
        saved = copy.deepcopy(base)
        config, _ = table_layout(frame, base)
        pd.testing.assert_frame_equal(frame, original)
        self.assertEqual(base, saved)
        self.assertTrue(config[COLUMNS['username']]['pinned'])
        self.assertEqual(config[COLUMNS['reasons']]['width'], 320)

    def test_missing_values_and_unicode_are_handled(self):
        for value in (None, float('nan'), pd.NA, pd.NaT):
            self.assertEqual(display_text(value, {}), '')
        self.assertGreater(text_pixels('ШШШШ'), text_pixels('iiii'))
        self.assertGreater(text_pixels('😀😀'), text_pixels('ii'))
        config, _ = table_layout(pd.DataFrame({'Данные': [None, pd.NA, {'текст': 'значение'}, ['тег']]}))
        self.assertLessEqual(config['Данные']['width'], 320)

    def test_invalid_limits_are_rejected_and_hidden_columns_stay_hidden(self):
        frame = pd.DataFrame({'Канал': ['a'], 'Скрыто': ['b']})
        for limit in (119, 601, True, 200.5):
            with self.assertRaises(ValueError):
                table_layout(frame, max_width=limit)
        config, width = table_layout(frame, {'Скрыто': None})
        self.assertIsNone(config['Скрыто'])
        self.assertEqual(width, config['Канал']['width'])
