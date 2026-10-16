import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from monetizer.catalog import load_catalog, load_playbook
from monetizer.keywords import expand_keywords, resolve_micro_niches, MICRO_NICHES
from monetizer.audit import parse_channel_html
from monetizer.pitches import compose_pitch, evidence_sources, chatgpt_prompt, suggest_scenarios, PLAYBOOK_VERSION
from monetizer.runner import JobRunner
from test_core import preview, NOW


def channel_row():
    html = preview().replace("Полезный пост", "В моём проекте разбираю свет в Blender и объясняю настройку мягких теней.")
    return parse_channel_html(html, "expert_blog", now=NOW)


class CatalogTests(unittest.TestCase):
    def test_imported_counts_and_scoped_semantics(self):
        catalog, playbook = load_catalog(), load_playbook()
        self.assertEqual(len(catalog["niches"]), 11)
        self.assertEqual(len(MICRO_NICHES), 52)
        self.assertEqual(sum(len(m["discovery_queries"]) for m in MICRO_NICHES.values()), 718)
        self.assertEqual(len(playbook["scenarios"]), 11)
        for topic, expected in [("ландшафтный дизайн", "landscape_design"),
                                ("дизайн презентаций", "presentation_design"),
                                ("AI для дизайнеров", "ai_for_designers"),
                                ("AI для фотографов", "ai_for_photographers")]:
            self.assertEqual([m["id"] for m in resolve_micro_niches(topic)], [expected])
        for topic in ("ландшафтный дизайн", "дизайн презентаций"):
            self.assertFalse(any("framer" in q.casefold() or "tilda" in q.casefold() for q in expand_keywords(topic)))

    def test_explicit_micro_and_unknown_topics(self):
        queries = expand_keywords(niche_id="6", micro_niche_id="landscape_design")
        self.assertIn("дендроплан", queries)
        with self.assertRaises(ValueError):
            expand_keywords(niche_id="6", micro_niche_id="websites_nocode")
        self.assertEqual(expand_keywords("таро для начинающих"), ["таро для начинающих"])
        self.assertEqual(len(resolve_micro_niches("Трендовые профессии: монтаж, 3D, моушн, съёмка")), 4)

    def test_reject_invalid_catalog_and_unmarked_examples(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "data.json"
            catalog = copy.deepcopy(load_catalog())
            catalog["niches"][1]["id"] = catalog["niches"][0]["id"]
            path.write_text(json.dumps(catalog), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_catalog(path)
            playbook = copy.deepcopy(load_playbook())
            playbook["scenarios"][0]["examples"][0]["input_evidence"]["fictional"] = False
            path.write_text(json.dumps(playbook), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_playbook(path)


class MessageTests(unittest.TestCase):
    def test_direct_course_announcement_selects_existing_course(self):
        row = channel_row()
        row["content_samples"][0]["text"] = "Друзья, запускаю мини-курс по анимации логотипов в After Effects. Будем разбирать движения и тайминг."
        draft = compose_pitch(row)
        self.assertEqual(draft["scenario_id"], "own_course_explicit")
        self.assertIn("существующему обучению", draft["message"])
        self.assertEqual(draft["evidence_basis"], "explicit_self_report")
        row["content_samples"][0]["forwarded"] = True
        self.assertEqual(compose_pitch(row)["scenario_id"], "limited_evidence")

    def test_real_content_is_stored_and_cited(self):
        row = channel_row()
        self.assertEqual(len(row["content_samples"]), 6)
        source = row["content_samples"][0]
        self.assertTrue(source["url"].startswith("https://t.me/expert_blog/"))
        message = compose_pitch(row)
        self.assertIn("Blender", message["message"])
        self.assertEqual(message["source"]["id"], source["id"])
        self.assertIn("practical_breakdown", [s["scenario_id"] for s in suggest_scenarios(row)])

    def test_personalized_scenario_requires_evidence_confirmation(self):
        row = channel_row()
        source = row["content_samples"][0]
        with self.assertRaises(ValueError):
            compose_pitch(row, scenario_id="practical_breakdown", evidence_id=source["id"])
        draft = compose_pitch(row, scenario_id="practical_breakdown", evidence_id=source["id"],
                              quote="настройку мягких теней", confirmed=True)
        self.assertIn("настройку мягких теней", draft["message"])
        self.assertIn("практикум", draft["message"])
        self.assertEqual(draft["scenario_id"], "practical_breakdown")

    def test_cannot_invent_quote_or_use_forward_as_own_project(self):
        row = channel_row()
        source = row["content_samples"][0]
        with self.assertRaises(ValueError):
            compose_pitch(row, evidence_id=source["id"], quote="Несуществующие результаты продаж")
        source["forwarded"] = True
        with self.assertRaises(ValueError):
            compose_pitch(row, scenario_id="own_projects_cases", evidence_id=source["id"], confirmed=True)

    def test_external_link_is_not_own_video_and_examples_are_not_facts(self):
        row = channel_row()
        draft = compose_pitch(row, scenario_id="external_link_unverified")
        self.assertIn("другого автора", draft["message"])
        self.assertNotIn("ваше видео", draft["message"].casefold())
        prompt = chatgpt_prompt(row, scenario_id="practical_breakdown")
        self.assertIn("Blender", prompt)
        self.assertNotIn('"examples"', prompt)
        self.assertNotIn("убрал паузу перед ответом героя", prompt)

    def test_edited_message_survives_reload_and_new_report(self):
        with tempfile.TemporaryDirectory() as folder:
            checkpoint = Path(folder) / "checkpoint.json"
            row = channel_row()
            state = {"settings": __import__('monetizer.settings', fromlist=['Settings']).Settings().to_dict(),
                     "status": "completed", "rows": [row], "niche": "Моушн", "candidates": {"expert_blog": []}}
            checkpoint.write_text(json.dumps(state), encoding="utf-8")
            runner = JobRunner(checkpoint)
            with patch("monetizer.runner.RESULTS_DIR", Path(folder)), patch("monetizer.runner.save_reports", return_value=(["report.xlsx"], [])):
                runner.save_pitch("@expert_blog", "Проверенный вручную текст", "practical_breakdown", "expert_blog/0")
            restored = JobRunner(checkpoint).snapshot()["rows"][0]
            self.assertEqual(restored["pitch"], "Проверенный вручную текст")
            self.assertTrue(restored["pitch_edited"])
            self.assertEqual(restored["pitch_rule_version"], PLAYBOOK_VERSION)


if __name__ == "__main__":
    unittest.main()
