import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import streamlit as st
from streamlit.testing.v1 import AppTest

from monetizer.audit import error_row, parse_channel_html
from monetizer.runner import JobRunner
from test_core import NOW, preview


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        st.cache_resource.clear()
        self.directory = tempfile.TemporaryDirectory()
        self.runner = JobRunner(Path(self.directory.name) / "checkpoint.json")
        self.factory = patch("monetizer.runner.JobRunner", return_value=self.runner)
        self.factory.start()
        self.app_path = Path(__file__).resolve().parents[1] / "monetizer_app.py"

    def tearDown(self):
        self.factory.stop()
        self.directory.cleanup()
        st.cache_resource.clear()

    def test_initial_page_and_custom_keywords(self):
        app = AppTest.from_file(str(self.app_path)).run(timeout=15)
        self.assertEqual(len(app.exception), 0)
        app.sidebar.selectbox[0].select("custom").run()
        next(t for t in app.sidebar.text_input if t.label == "Ваша тема").set_value("дизайн").run()
        self.assertIn("framer", next(t for t in app.sidebar.text_area if t.label == "Запросы — можно изменить").value.casefold())
        self.assertEqual(len(app.exception), 0)

    def test_custom_landscape_topic_excludes_web_tools(self):
        app = AppTest.from_file(str(self.app_path)).run(timeout=15)
        app.sidebar.selectbox[0].select("custom").run()
        next(t for t in app.sidebar.text_input if t.label == "Ваша тема").set_value("ландшафтный дизайн").run()
        text = next(t for t in app.sidebar.text_area if t.label == "Запросы — можно изменить").value.casefold()
        self.assertIn("дендроплан", text)
        self.assertNotIn("framer", text)
        self.assertNotIn("tilda", text)
        self.assertEqual(len(app.exception), 0)

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
            next(s for s in app.selectbox if s.label == "Рабочий статус").select("contacted").run()
            next(t for t in app.text_area if t.label == "Заметка о кандидате").set_value("Отправлено личное предложение").run()
            next(b for b in app.button if b.label == "Сохранить статус и заметку").click().run()
            stored = self.runner.repo.list_channels()[0]
            self.assertEqual(stored["work_status"], "contacted")
            self.assertIn("личное предложение", stored["notes"])
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
        self.assertEqual(len(app.dataframe[0].value), 2)
        next(c for c in app.checkbox if c.label == "Только зелёные").check().run()
        self.assertEqual(len(app.dataframe[0].value), 1)
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


if __name__ == "__main__":
    unittest.main()
