import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import streamlit as st
from streamlit.testing.v1 import AppTest

from monetizer.audit import error_row, parse_channel_html
from monetizer.runner import JobRunner
from monetizer.export import COLUMNS
from test_core import NOW, preview


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        self.code_reload = patch('monetizer.live_reload.refresh_code')
        self.code_reload.start()
        st.cache_resource.clear()
        self.directory = tempfile.TemporaryDirectory()
        self.runner = JobRunner(Path(self.directory.name) / "checkpoint.json")
        self.profile_root = patch('monetizer.tgstat_profiles.ROOT', Path(self.directory.name) / 'profiles')
        self.profile_root.start()
        self.factory = patch("monetizer.runner.JobRunner", return_value=self.runner)
        self.factory.start()
        self.runtime = patch('monetizer.runtime.acquire_runner', side_effect=lambda factory: factory())
        self.runtime.start()
        self.app_path = Path(__file__).resolve().parents[1] / "monetizer_app.py"

    def tearDown(self):
        self.code_reload.stop()
        self.factory.stop()
        self.runtime.stop()
        self.profile_root.stop()
        self.directory.cleanup()
        st.cache_resource.clear()

    def test_presets_apply_without_starting_a_search(self):
        with patch.object(self.runner, 'start') as start:
            app = AppTest.from_file(str(self.app_path)).run(timeout=15)
            next(n for n in app.number_input if n.label == 'Подписчики от').set_value(5000).run()
            next(t for t in app.text_input if t.label == 'Название пресета').set_value('Мой пресет').run()
            next(b for b in app.button if b.label == 'Сохранить текущие настройки как пресет').click().run()
            next(s for s in app.selectbox if s.label == 'Сохранённый пресет').set_value('Мой пресет').run()
            next(n for n in app.number_input if n.label == 'Подписчики от').set_value(7000).run()
            next(b for b in app.button if b.label == 'Применить пресет').click().run()
            self.assertEqual(next(n for n in app.number_input if n.label == 'Подписчики от').value, 5000)
            start.assert_not_called()
            self.assertEqual(len(app.exception), 0)

    def test_new_tools_and_business_pages_render_without_network(self):
        self._seed_database()
        with patch('monetizer.runner.TGStatSearch') as browser, patch('monetizer.runner.WebAuditor') as network, \
                patch('monetizer.ai_audit.chat_completion') as ai:
            app = AppTest.from_file(str(self.app_path)).run(timeout=15)
            next(r for r in app.radio if r.label == 'Раздел').set_value('Инструменты').run()
            for name in ['Поиск и очередь', 'Анализ контента', 'Калибровка порога', 'AI-лаборатория', 'Теги и подборки']:
                next(r for r in app.radio if r.label == 'Инструмент').set_value(name).run()
                self.assertEqual(len(app.exception), 0, name)
                self.assertEqual(len(app.error), 0, name)
            for name in ['Канбан', 'Аналитика партнёрств']:
                next(r for r in app.radio if r.label == 'Раздел').set_value(name).run()
                self.assertEqual(len(app.exception), 0, name)
                self.assertEqual(len(app.error), 0, name)
            browser.assert_not_called()
            network.assert_not_called()
            ai.assert_not_called()

    def test_tags_economics_and_kanban_status_are_persisted(self):
        from monetizer.workspace import Workspace
        self._seed_database()
        app = AppTest.from_file(str(self.app_path)).run(timeout=15)
        next(t for t in app.text_input if t.label == 'Теги через запятую').set_value('дизайнер, личный блог')
        next(t for t in app.text_input if t.label == 'Подборки через запятую').set_value('Первый контакт')
        next(s for s in app.selectbox if s.label == 'Этап партнёрства').set_value('talks')
        next(n for n in app.number_input if n.label == 'Предполагаемый средний чек').set_value(1000.)
        next(b for b in app.button if b.label == 'Сохранить теги и экономику').click().run()
        record = Workspace(self.runner.repo).get('candidate', 'expert_blog')
        self.assertEqual(record['tags'], ['дизайнер', 'личный блог'])
        self.assertEqual(record['economics']['price'], 1000)
        next(r for r in app.radio if r.label == 'Раздел').set_value('Канбан').run()
        next(s for s in app.selectbox if s.label == 'Переместить').set_value('contacted').run()
        next(b for b in app.button if b.label == 'Переместить').click().run()
        self.assertEqual(self.runner.repo.list_channels()[0]['work_status'], 'contacted')
        self.assertEqual(len(app.exception), 0)

    def test_draft_can_be_prepared_during_search_without_mutating_active_categories(self):
        from monetizer.workspace import Workspace
        from monetizer.settings import Settings
        self.runner.state.update(status='running', stage='Поиск', settings=Settings(search_mode='categories', category_ids=('34',)).to_dict())
        with patch.object(self.runner, 'start') as start:
            app = AppTest.from_file(str(self.app_path)).run(timeout=15)
            next(r for r in app.radio if r.label == 'Раздел').set_value('Инструменты').run()
            next(m for m in app.multiselect if m.label == 'Следующие категории').set_value(['53']).run()
            next(b for b in app.button if b.label == 'Сохранить следующий поиск').click().run()
            self.assertEqual(Workspace(self.runner.repo).get('draft', 'next_search')['categories'], ['53'])
            self.assertEqual(self.runner.state['settings']['category_ids'], ('34',))
            self.assertTrue(next(b for b in app.button if b.label == 'Подставить подготовленный поиск в панель').disabled)
            start.assert_not_called()
            self.assertEqual(len(app.exception), 0)

    def test_table_view_roundtrip_and_candidate_navigation_buttons(self):
        self._seed_database()
        app = AppTest.from_file(str(self.app_path)).run(timeout=15)
        next(m for m in app.multiselect if m.label == 'Видимые столбцы').set_value(['username', 'title']).run()
        next(t for t in app.text_input if t.label == 'Название вида').set_value('Коротко').run()
        next(b for b in app.button if b.label == 'Сохранить вид таблицы').click().run()
        next(s for s in app.selectbox if s.label == 'Сохранённый вид').set_value('Коротко').run()
        next(m for m in app.multiselect if m.label == 'Видимые столбцы').set_value(['username', 'subs']).run()
        next(b for b in app.button if b.label == 'Применить вид').click().run()
        self.assertEqual(next(m for m in app.multiselect if m.label == 'Видимые столбцы').value, ['username', 'title'])
        next(b for b in app.button if b.label == 'Следующий →').click().run()
        self.assertEqual(next(s for s in app.selectbox if s.label == 'Канал для черновика').value, '@expert_blog')
        self.assertEqual(len(app.exception), 0)

    def test_retry_errors_selects_only_failed_channels_and_forces_fetch(self):
        self._seed_database()
        self.runner.state['rows'].append(error_row('failed_blog', 'network', 'Timeout'))
        with patch.object(self.runner, 'start') as start:
            app = AppTest.from_file(str(self.app_path)).run(timeout=15)
            next(r for r in app.radio if r.label == 'Раздел').set_value('Инструменты').run()
            next(b for b in app.button if b.label == 'Повторить только ошибки доступа (1)').click().run()
            self.assertEqual(start.call_args.kwargs['usernames'], ['@failed_blog'])
            self.assertTrue(start.call_args.args[0].force_reaudit)
            self.assertFalse(start.call_args.args[0].ai_enabled)
            self.assertEqual(len(app.exception), 0)

    def test_initial_page_uses_real_tgstat_categories(self):
        app = AppTest.from_file(str(self.app_path)).run(timeout=15)
        self.assertEqual(len(app.exception), 0)
        choices = next(m for m in app.multiselect if m.label == 'Тематики TGStat')
        self.assertEqual(len(choices.options), 47)
        self.assertIn('Дизайн', choices.options)
        self.assertIn('Медицина', choices.options)
        self.assertFalse(any(t.label == 'Запросы — можно изменить' for t in app.text_area))
        self.assertEqual(len(app.exception), 0)

    def test_sidebar_navigation_has_home_and_primary_controls_before_options(self):
        with patch.object(self.runner, 'start') as start:
            app = AppTest.from_file(str(self.app_path)).run(timeout=15)
            self.assertEqual(len([r for r in app.sidebar.radio if r.label == 'Раздел']), 1)
            self.assertFalse(any(r.label == 'Раздел' for r in app.main.radio))
            for label in ('AI-аудит Gemini', 'Фильтры TGStat', 'Разделы приложения'):
                self.assertFalse(next(e for e in app.sidebar.expander if e.label == label).proto.expanded)
            next(r for r in app.radio if r.label == 'Раздел').set_value('Профили TGStat').run()
            next(b for b in app.sidebar.button if b.label == 'На главную — поиск').click().run()
            self.assertEqual(next(r for r in app.radio if r.label == 'Раздел').value, 'Поиск')
            self.assertTrue(any(m.label == 'Подходящие / цель' for m in app.metric))
            self.assertEqual(len(app.exception), 0)
            start.assert_not_called()

    def test_category_scope_limits_optional_audit_focus(self):
        app = AppTest.from_file(str(self.app_path)).run(timeout=15)
        next(m for m in app.multiselect if m.label == 'Тематики TGStat').set_value(['28']).run()
        focus = next(s for s in app.selectbox if s.label == 'Проверять конкретную специализацию')
        self.assertTrue(any('Стоматолог' in option or 'стоматолог' in option for option in focus.options))
        self.assertFalse(any('UX' in option or 'ландшафт' in option.casefold() for option in focus.options))
        self.assertEqual(len(app.exception), 0)

    def test_running_search_locks_conditions_but_keeps_navigation_and_results_usable(self):
        from monetizer.settings import Settings
        from monetizer.preferences import Preferences
        row = parse_channel_html(preview(), 'expert_blog', now=NOW)
        self.runner.state.update(status='running', stage='Поиск', settings=Settings(search_mode='categories', category_ids=('34',)).to_dict(),
                                 candidates={'expert_blog': []}, rows=[row])
        Preferences(self.runner.repo).save('tgstat_category_ids', ['53'])
        with patch.object(self.runner, 'start') as start, patch.object(self.runner, 'stop') as stop:
            app = AppTest.from_file(str(self.app_path)).run(timeout=15)
            self.assertEqual(len(app.exception), 0)
            self.assertEqual(next(m for m in app.multiselect if m.label == 'Тематики TGStat').value, ['34'])
            for kind in ('checkbox', 'number_input', 'multiselect', 'text_input', 'text_area', 'slider', 'selectbox'):
                for widget in getattr(app.sidebar, kind):
                    if widget.label == 'Лимит ширины текстовых столбцов, px':
                        self.assertFalse(widget.disabled)
                        continue
                    self.assertTrue(widget.disabled, widget.label)
            self.assertTrue(next(b for b in app.button if b.label == 'Начать поиск и аудит').disabled)
            self.assertFalse(next(b for b in app.sidebar.button if b.label == 'На главную — поиск').disabled)
            self.assertFalse(next(c for c in app.checkbox if c.label == 'Только зелёные').disabled)
            next(b for b in app.button if b.label == 'Остановить и сохранить прогресс').click().run()
            stop.assert_called_once()
            start.assert_not_called()
            self.runner.state['status'] = 'completed'
            app.run()
            self.assertFalse(next(m for m in app.multiselect if m.label == 'Тематики TGStat').disabled)
            self.assertEqual(len(app.exception), 0)

    def test_compact_results_use_short_headers_narrow_priority_and_formatted_numbers(self):
        from monetizer.settings import Settings
        row = parse_channel_html(preview(), 'expert_blog', now=NOW)
        row.update(ai_priority_score=60, ai_audit_status='queued')
        self.runner.state.update(status='completed', stage='Готово', settings=Settings(ai_enabled=True).to_dict(),
                                 candidates={'expert_blog': []}, rows=[row])
        app = AppTest.from_file(str(self.app_path)).run(timeout=15)
        def table():
            return next(t for t in app.dataframe if COLUMNS['activity_ratio'] in t.value.columns)
        self.assertEqual(len(app.exception), 0)
        config = json.loads(table().proto.columns)
        self.assertLessEqual(config[COLUMNS['ai_priority_score']]['width'], 128)
        self.assertGreaterEqual(config[COLUMNS['ai_priority_score']]['width'], 64)
        self.assertEqual(config[COLUMNS['ai_priority_score']]['label'], 'Приоритет')
        self.assertEqual(config[COLUMNS['activity_ratio']]['type_config']['format'], '%.1f%%')
        self.assertNotIn(COLUMNS['median_views'], table().proto.column_order)
        next(r for r in app.radio if r.label == 'Вид таблицы').set_value('Подробный').run()
        self.assertIn(COLUMNS['median_views'], table().proto.column_order)
        self.assertEqual(len(app.exception), 0)

    def test_table_width_limit_applies_and_survives_new_session(self):
        from monetizer.preferences import Preferences
        from monetizer.ui_tables import WIDTH_LABEL, WIDTH_KEY
        self._seed_database()
        title = 'Практикующий дизайнер интерфейсов ' * 20
        self.runner.state['rows'][0]['title'] = title
        app = AppTest.from_file(str(self.app_path)).run(timeout=15)
        self.assertFalse(next(e for e in app.sidebar.expander if e.label == 'Вид таблиц').proto.expanded)
        next(n for n in app.number_input if n.label == WIDTH_LABEL).set_value(200).run()
        table = next(t for t in app.dataframe if COLUMNS['title'] in t.value.columns)
        self.assertEqual(json.loads(table.proto.columns)[COLUMNS['title']]['width'], 200)
        self.assertEqual(table.value[COLUMNS['title']].iloc[0], title)
        self.assertEqual(Preferences(self.runner.repo).values[WIDTH_KEY], 200)
        fresh = AppTest.from_file(str(self.app_path)).run(timeout=15)
        self.assertEqual(next(n for n in fresh.number_input if n.label == WIDTH_LABEL).value, 200)
        fresh_table = next(t for t in fresh.dataframe if COLUMNS['title'] in t.value.columns)
        self.assertEqual(json.loads(fresh_table.proto.columns)[COLUMNS['title']]['width'], 200)
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(fresh.exception), 0)

    def test_invalid_search_bounds_disable_start_until_corrected(self):
        with patch.object(self.runner, 'start') as start:
            app = AppTest.from_file(str(self.app_path)).run(timeout=15)
            next(n for n in app.number_input if n.label == 'Подписчики от').set_value(40000).run()
            self.assertTrue(next(b for b in app.button if b.label == 'Начать поиск и аудит').disabled)
            self.assertTrue(any('Минимум больше максимума' in message.value for message in app.warning))
            next(n for n in app.number_input if n.label == 'Подписчики до').set_value(50000).run()
            self.assertFalse(next(b for b in app.button if b.label == 'Начать поиск и аудит').disabled)
            start.assert_not_called()
            self.assertEqual(len(app.exception), 0)

    def test_screenshot_defaults_and_edits_reach_runner(self):
        with patch.object(self.runner, 'start') as start:
            app = AppTest.from_file(str(self.app_path)).run(timeout=15)
            self.assertEqual(next(m for m in app.multiselect if m.label == 'Страна канала').value, [])
            self.assertEqual(next(m for m in app.multiselect if m.label == 'Язык канала').value, ['3'])
            self.assertEqual(next(s for s in app.selectbox if s.label == 'Тип канала').value, '')
            self.assertEqual(tuple(next(s for s in app.slider if s.label == 'Возраст канала, месяцев').value), (12, 120))
            self.assertEqual(tuple(next(s for s in app.slider if s.label == 'Уровень вовлечённости ERR, %').value), (10, 100))
            self.assertEqual(next(n for n in app.number_input if n.label == 'Возраст поста для оценки активности, часов').value, 24)
            self.assertEqual(next(n for n in app.number_input if n.label == 'Допустимое отклонение возраста, часов').value, 6)
            self.assertEqual(next(n for n in app.number_input if n.label == 'Просмотры поста от подписчиков, %').value, 10)
            self.assertEqual(next(n for n in app.number_input if n.label == 'Средние просмотры от подписчиков, %').value, 10)
            self.assertFalse(any(n.label == 'Средние просмотры поста от' for n in app.number_input))
            next(s for s in app.slider if s.label == 'Возраст канала, месяцев').set_value((24, 60)).run()
            next(s for s in app.slider if s.label == 'Уровень вовлечённости ERR, %').set_value((15, 80)).run()
            next(n for n in app.number_input if n.label == 'Подписчики от').set_value(5000).run()
            next(n for n in app.number_input if n.label == 'Индекс цитирования до').set_value(25.0).run()
            next(n for n in app.number_input if n.label == 'Возраст поста для оценки активности, часов').set_value(30).run()
            next(n for n in app.number_input if n.label == 'Допустимое отклонение возраста, часов').set_value(4).run()
            next(n for n in app.number_input if n.label == 'Просмотры поста от подписчиков, %').set_value(12.0).run()
            next(n for n in app.number_input if n.label == 'Средние просмотры от подписчиков, %').set_value(15.0).run()
            next(b for b in app.button if b.label == 'Начать поиск и аудит').click().run()
            settings = start.call_args.args[0]
            self.assertEqual(settings.category_ids, ('34',))
            self.assertEqual(settings.search_mode, 'categories')
            self.assertEqual((settings.min_age_months, settings.max_age_months), (24, 60))
            self.assertEqual((settings.min_err, settings.max_err), (15, 80))
            self.assertEqual(settings.min_subs, 5000)
            self.assertEqual(settings.max_ci, 25.0)
            self.assertEqual(settings.target_post_age_hours, 30)
            self.assertEqual(settings.post_age_tolerance_hours, 4)
            self.assertEqual(settings.min_post_view_ratio, 12)
            self.assertEqual(settings.min_avg_view_ratio, 15)
            self.assertEqual(settings.min_avg_views, 0)
            self.assertNotIn('queries', start.call_args.kwargs)
            self.assertEqual(len(app.exception), 0)

    def test_empty_category_selection_does_not_start_search(self):
        with patch.object(self.runner, 'start') as start:
            app = AppTest.from_file(str(self.app_path)).run(timeout=15)
            next(m for m in app.multiselect if m.label == 'Тематики TGStat').set_value([]).run()
            next(b for b in app.button if b.label == 'Начать поиск и аудит').click().run()
            start.assert_not_called()
            self.assertTrue(any('тематику' in error.value for error in app.error))

    def test_build_source_based_pitch_requires_confirmation(self):
        from test_editorial import channel_row
        self.runner.state.update(status="completed", stage="Готово", niche="Моушн", run_id="test_run",
                                 candidates={"expert_blog": []}, rows=[channel_row()])
        app = AppTest.from_file(str(self.app_path)).run(timeout=15)
        next(s for s in app.selectbox if s.label == "Сценарий первого сообщения").select("practical_breakdown").run()
        next(b for b in app.button if b.label == "Собрать черновик по выбранному источнику").click().run()
        self.assertTrue(any("Подтвердите" in error.value for error in app.error))
        next(c for c in app.checkbox if c.label.startswith("Проверил требования")).check().run()
        next(b for b in app.button if b.label == "Собрать черновик по выбранному источнику").click().run()
        message = next(t for t in app.text_area if t.label.startswith("Черновик")).value
        self.assertIn("Blender", message)
        self.assertIn("практикум", message)
        self.assertEqual(len(app.exception), 0)

    def _seed_database(self):
        from test_qualification import expert
        from monetizer.qualification import qualify
        from monetizer.settings import Settings
        row = qualify(expert(), micro_id="motion_design")
        self.runner.state.update(status="completed", stage="Готово", niche="Моушн", run_id="ui_run",
                                 settings=Settings(micro_id="motion_design").to_dict(),
                                 candidates={"expert_blog": []}, rows=[row], started_at="2026-10-15T10:00:00+00:00")
        self.runner.repo.save_state(self.runner.state)

    def test_database_crm_and_batch_without_network(self):
        self._seed_database()
        with patch("monetizer.runner.TGStatSearch") as browser, patch("monetizer.runner.WebAuditor") as network:
            app = AppTest.from_file(str(self.app_path)).run(timeout=15)
            self.assertEqual(len(app.exception), 0)
            next(r for r in app.radio if r.label == 'Раздел').set_value('База кандидатов').run()
            next(s for s in app.selectbox if s.label == "Рабочий статус").select("contacted").run()
            next(t for t in app.text_area if t.label == "Заметка о кандидате").set_value("Отправлено личное предложение").run()
            next(b for b in app.button if b.label == "Сохранить статус и заметку").click().run()
            stored = self.runner.repo.list_channels()[0]
            self.assertEqual(stored["work_status"], "contacted")
            self.assertIn("личное предложение", stored["notes"])
            next(r for r in app.radio if r.label == 'Раздел').set_value('Пакет ChatGPT').run()
            next(b for b in app.button if b.label == "Создать пакет ChatGPT").click().run()
            self.assertEqual(len(app.session_state["chat_batch"]["channels"]), 1)
            self.assertEqual(len(app.exception), 0)
            browser.assert_not_called()
            network.assert_not_called()

    def test_filters_and_editable_pitch(self):
        row = parse_channel_html(preview(), "expert_blog", now=NOW)
        self.runner.state.update(status="completed", stage="Готово", niche="Дизайн", run_id="test_run",
                                 candidates={"expert_blog": [], "broken_blog": []}, rows=[row, error_row("broken_blog", "http_403", "HTTP 403")])
        app = AppTest.from_file(str(self.app_path)).run(timeout=15)
        self.assertEqual(len(app.exception), 0)
        def results():
            # Summary tables precede results. Match the channel table by its
            # columns so adding another summary cannot change what we assert.
            expected = {COLUMNS[key] for key in ('username', 'verdict', 'link')}
            tables = [table.value for table in app.dataframe if expected.issubset(table.value.columns)]
            self.assertEqual(len(tables), 1, 'Expected exactly one channel results table')
            return tables[0]

        self.assertEqual(len(results()), 2)
        self.assertEqual(set(results()[COLUMNS['username']]), {row['username'], '@broken_blog'})
        next(c for c in app.checkbox if c.label == "Только зелёные").check().run()
        self.assertEqual(len(results()), 1)
        self.assertEqual(list(results()[COLUMNS['username']]), [row['username']])
        self.assertEqual(len(app.exception), 0)
        pitch = next(t for t in app.text_area if t.label.startswith("Черновик"))
        pitch.set_value("Проверенный текст предложения").run()
        self.assertEqual(next(t for t in app.text_area if t.label.startswith("Черновик")).value, "Проверенный текст предложения")

    def test_sender_profile_persists_and_enters_batch(self):
        from monetizer.profile import load_profile
        self._seed_database()
        with patch('monetizer.runner.TGStatSearch') as browser, patch('monetizer.runner.WebAuditor') as network:
            app = AppTest.from_file(str(self.app_path)).run(timeout=15)
            next(t for t in app.text_input if t.label == 'Имя отправителя').set_value('Алексей')
            next(t for t in app.text_input if t.label == 'Ваша роль').set_value('маркетолог')
            next(t for t in app.text_area if t.label == 'Какие задачи берёте').set_value('привлечение заявок')
            next(b for b in app.button if b.label == 'Сохранить профиль отправителя').click().run()
            self.assertEqual(load_profile(self.runner.repo)['name'], 'Алексей')
            next(r for r in app.radio if r.label == 'Раздел').set_value('Пакет ChatGPT').run()
            next(b for b in app.button if b.label == 'Создать пакет ChatGPT').click().run()
            self.assertEqual(app.session_state['chat_batch']['sender_profile']['services'], 'привлечение заявок')
            self.assertEqual(len(app.exception), 0)
            browser.assert_not_called()
            network.assert_not_called()

    def test_manual_quality_form_without_network(self):
        from monetizer.evaluation import evaluate
        self._seed_database()
        with patch('monetizer.runner.TGStatSearch') as browser, patch('monetizer.runner.WebAuditor') as network:
            app = AppTest.from_file(str(self.app_path)).run(timeout=15)
            next(r for r in app.radio if r.label == 'Раздел').set_value('Качество отбора').run()
            next(s for s in app.selectbox if s.label == 'Тип канала по вашей оценке').select('expert')
            next(s for s in app.selectbox if s.label == 'Соответствие теме по вашей оценке').select('matched')
            next(s for s in app.selectbox if s.label == 'Проходит предварительный отбор по вашей оценке').select(True)
            next(t for t in app.text_input if t.label == 'Кто выполнил ручную оценку').set_value('Проверяющий')
            next(t for t in app.text_area if t.label == 'Основание ручной оценки').set_value('Описание и собственные проекты')
            next(b for b in app.button if b.label == 'Сохранить оценку канала').click().run()
            self.assertEqual(evaluate(self.runner.repo)['labelled'], 1)
            self.assertEqual(self.runner.repo.list_channels()[0]['work_status'], 'new')
            self.assertEqual(len(app.exception), 0)
            browser.assert_not_called()
            network.assert_not_called()

    def test_start_page_does_not_render_hidden_workspace(self):
        with patch('monetizer.ui_workspace.database_panel') as database, patch('monetizer.ui_workspace.history_panel') as history, \
                patch('monetizer.ui_workspace.batch_panel') as batch, patch('monetizer.ui_workspace.quality_panel') as quality:
            app = AppTest.from_file(str(self.app_path)).run(timeout=15)
            self.assertEqual(len(app.exception), 0)
            for panel in [database, history, batch, quality]:
                panel.assert_not_called()

    def test_manual_login_continue_button_signals_worker(self):
        self.runner.state.update(status='running', stage='Ожидание входа', settings=__import__('monetizer.settings', fromlist=['Settings']).Settings().to_dict(),
                                 browser={'waiting': True, 'message': 'Войдите в TGStat'})
        app = AppTest.from_file(str(self.app_path)).run(timeout=15)
        next(b for b in app.button if b.label == 'Продолжить после входа').click().run()
        self.assertTrue(self.runner.browser_control.resume.is_set())
        self.assertEqual(len(app.exception), 0)

    def test_dev_diagnostics_page_does_not_start_browser(self):
        with patch('monetizer.runner.TGStatSearch') as browser, patch('monetizer.runner.WebAuditor') as network:
            app = AppTest.from_file(str(self.app_path)).run(timeout=15)
            next(r for r in app.radio if r.label == 'Раздел').set_value('Dev-диагностика').run()
            self.assertEqual(len(app.exception), 0)
            self.assertTrue(any(b.label == 'Скачать лог диагностики' for b in app.get('download_button')))
            browser.assert_not_called()
            network.assert_not_called()

    def test_profile_manager_add_delete_restore_and_select_without_browser(self):
        from monetizer.tgstat_profiles import TGStatProfiles
        store = TGStatProfiles(self.runner.repo)
        with patch('monetizer.ui_tgstat_profiles.open_for_login') as browser, \
                patch.object(self.runner, 'start') as start:
            app = AppTest.from_file(str(self.app_path)).run(timeout=15)
            next(b for b in app.button if b.label == 'Управлять профилями').click().run()
            self.assertEqual(next(r for r in app.radio if r.label == 'Раздел').value, 'Профили TGStat')
            self.assertEqual(len(app.sidebar.dataframe), 0)
            next(t for t in app.text_input if t.label == 'Название нового профиля').set_value('Тестовый аккаунт')
            next(b for b in app.button if b.label == 'Добавить профиль').click().run()
            created = next(row['id'] for row in store.list() if row['name'] == 'Тестовый аккаунт')
            self.assertEqual(next(r for r in app.radio if r.label == 'Аккаунты TGStat').value, created)
            next(b for b in app.button if b.label == 'Удалить профиль').click().run()
            self.assertNotIn(created, [row['id'] for row in store.list()])
            self.assertEqual(store.archived()[0]['id'], created)
            next(b for b in app.button if b.key == 'restore_tgstat_' + created).click().run()
            self.assertIn(created, [row['id'] for row in store.list()])
            next(b for b in app.button if b.label == 'Использовать для поиска').click().run()
            self.assertEqual(next(r for r in app.radio if r.label == 'Раздел').value, 'Поиск')
            self.assertEqual(next(s for s in app.selectbox if s.label == 'Профиль TGStat').value, created)
            self.assertEqual(len(app.exception), 0)
            browser.assert_not_called()
            start.assert_not_called()

    def test_no_profiles_disables_search_and_allows_default_restore(self):
        from monetizer.tgstat_profiles import TGStatProfiles
        store = TGStatProfiles(self.runner.repo)
        store.delete('default')
        with patch('monetizer.ui_tgstat_profiles.open_for_login') as browser:
            app = AppTest.from_file(str(self.app_path)).run(timeout=15)
            self.assertTrue(next(b for b in app.button if b.label == 'Начать поиск и аудит').disabled)
            next(b for b in app.button if b.label == 'Управлять профилями').click().run()
            next(b for b in app.button if b.key == 'restore_tgstat_default').click().run()
            self.assertEqual([row['id'] for row in store.list()], ['default'])
            self.assertEqual(len(app.exception), 0)
            browser.assert_not_called()

    def test_close_chrome_runs_before_card_and_renders_workspace_once(self):
        import monetizer.ui_tgstat_profiles as profiles_ui
        sessions = [{'session_id': 'fake-session', 'profile_id': 'default'}]
        order = []
        original = profiles_ui._profile_card

        def render_card(*args):
            order.append('card')
            return original(*args)

        def close(session_id):
            self.assertEqual(session_id, 'fake-session')
            self.assertEqual(order, [])  # No partially rendered new card while closing.
            order.append('close')
            sessions.clear()
            return {'closed': True, 'close_status': 'closed', 'auth_status': 'authenticated'}

        with patch.object(profiles_ui, 'retained_sessions', side_effect=lambda **kwargs: list(sessions)), \
                patch.object(profiles_ui, 'close_retained', side_effect=close) as closing, \
                patch.object(profiles_ui, '_profile_card', side_effect=render_card), \
                patch.object(profiles_ui, 'open_for_login') as browser:
            app = AppTest.from_file(str(self.app_path)).run(timeout=15)
            next(b for b in app.button if b.label == 'Управлять профилями').click().run()
            order.clear()
            next(b for b in app.button if b.label == 'Закрыть Chrome').click().run()
            self.assertEqual(order, ['close', 'card'])
            self.assertEqual(len([r for r in app.radio if r.label == 'Аккаунты TGStat']), 1)
            self.assertEqual(len([b for b in app.button if b.label == 'Закрыть Chrome']), 1)
            self.assertTrue(next(b for b in app.button if b.label == 'Закрыть Chrome').disabled)
            self.assertFalse(next(b for b in app.button if b.label == 'Открыть Chrome').disabled)
            self.assertTrue(any('Chrome закрыт штатно' in item.value for item in app.info))
            with self.assertRaises(KeyError):
                app.session_state['_tgstat_browser_action']
            self.assertEqual(len(app.exception), 0)
            closing.assert_called_once()
            browser.assert_not_called()

    def test_close_error_keeps_one_card_and_allows_retry(self):
        import monetizer.ui_tgstat_profiles as profiles_ui
        sessions = [{'session_id': 'fake-session', 'profile_id': 'default'}]
        with patch.object(profiles_ui, 'retained_sessions', side_effect=lambda **kwargs: list(sessions)), \
                patch.object(profiles_ui, 'close_retained', side_effect=RuntimeError('Chrome ещё работает')) as closing:
            app = AppTest.from_file(str(self.app_path)).run(timeout=15)
            next(b for b in app.button if b.label == 'Управлять профилями').click().run()
            next(b for b in app.button if b.label == 'Закрыть Chrome').click().run()
            self.assertTrue(any('Chrome ещё работает' in item.value for item in app.error))
            self.assertEqual(len([r for r in app.radio if r.label == 'Аккаунты TGStat']), 1)
            self.assertFalse(next(b for b in app.button if b.label == 'Закрыть Chrome').disabled)
            self.assertTrue(next(b for b in app.button if b.label == 'Открыть Chrome').disabled)
            self.assertEqual(len(app.exception), 0)
            closing.assert_called_once()


if __name__ == "__main__":
    unittest.main()
