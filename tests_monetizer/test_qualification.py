import copy
import json
import tempfile
from pathlib import Path
import unittest
from datetime import datetime, timedelta, timezone

from monetizer.qualification import qualify, extract_offers
from monetizer.repository import Repository
from monetizer.audit import reassess_metrics
from monetizer.settings import Settings


def expert(username="expert_blog"):
    observed = datetime.now(timezone.utc)
    published = observed - timedelta(days=1)
    return {"username": "@" + username, "title": "Анна — моушн-дизайнер", "status": "recommended",
            "reasons": "Метрики соответствуют", "subs": 4000, "avg_views": 800,
            'avg_views_exact': 800, 'view_ratio_exact': 20, 'eligible_posts': 6, 'post_count': 6,
            'last_post': published.isoformat(), 'avg_gap_days': 1,
            'post_observations': [{'id': f'{username}/1', 'date': published.isoformat(),
                                   'observed_at': observed.isoformat(), 'views': 800, 'subs_at_observation': 4000}],
            "audited_at": datetime.now(timezone.utc).isoformat(), "link": f"https://t.me/{username}",
            "about_text": "Я дизайнер. Работаю с After Effects и Lottie.",
            "content_samples": [{"id": f"{username}/1", "url": f"https://t.me/{username}/1", "date": "2026-10-15T10:00:00+00:00",
                                 "text": "В моём проекте разбираю анимацию After Effects и показываю Lottie для интерфейса.", "forwarded": False, "links": []},
                                {"id": f"{username}/2", "url": f"https://t.me/{username}/2", "date": "2026-10-14T10:00:00+00:00",
                                 "text": "Показываю мой проект: настройка шейп-анимации в After Effects.", "forwarded": False, "links": []}]}


class QualificationTests(unittest.TestCase):
    def test_expert_with_single_vacancy(self):
        row = expert()
        row["content_samples"].append({"id": "expert_blog/3", "text": "Вакансия: ищем монтажёра", "forwarded": False})
        result = qualify(row, micro_id="motion_design")
        self.assertEqual(result["channel_type"], "expert")
        self.assertEqual(result["status"], "recommended")
        self.assertGreaterEqual(len(result["classification"]["positive_evidence"]), 2)

    def test_plugin_store_never_green(self):
        row = expert()
        row.update(title="Склад плагинов", about_text="Склад плагинов After Effects")
        for post in row["content_samples"]:
            post["text"] = "Скачать плагины After Effects и Lottie, подборка файлов"
        result = qualify(row, micro_id="motion_design")
        self.assertEqual(result["channel_type"], "aggregator")
        self.assertEqual(result["status"], "rejected")

    def test_forwarded_not_expertise_and_one_tool_not_relevance(self):
        row = expert()
        row["about_text"] = "After Effects"
        for post in row["content_samples"]:
            post["forwarded"] = True
        result = qualify(row, micro_id="motion_design")
        self.assertEqual(result["status"], "review")
        self.assertEqual(result["relevance"], "unknown")

    def test_offer_price_booking_and_source(self):
        row = expert()
        row["content_samples"][0]["text"] = "Провожу консультации по анимации. Стоимость 5 000 ₽. Запись в личные сообщения @expert_contact"
        offers = extract_offers(row)
        item = next(o for o in offers if o["kind"] == "consultation")
        self.assertEqual(item["ownership"], "self_reported")
        self.assertIn("5 000 ₽", item["prices"])
        self.assertEqual(item["source_id"], "expert_blog/1")
        self.assertIn("@expert_contact", item["booking_methods"])
        self.assertEqual(extract_offers({"title": "Без услуг"}), [])

    def test_cached_metrics_reapply_changed_thresholds(self):
        row = expert()
        row.update(eligible_posts=8, post_count=8, avg_gap_days=1, last_post=datetime.now(timezone.utc).isoformat(),
                   avg_views_exact=800, view_ratio_exact=20)
        row['post_observations'] = [{'id': 'expert_blog/1', 'date': (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
                                     'observed_at': row['last_post'], 'views': 800, 'subs_at_observation': 4000}]
        self.assertEqual(reassess_metrics(row, Settings())["metric_status"], "recommended")
        self.assertEqual(reassess_metrics(row, Settings(min_avg_views=1000))["metric_status"], "rejected")

    def test_school_with_author_posts_needs_review(self):
        row = expert()
        row.update(title="Школа анимации", about_text="Школа анимации. Работаем в After Effects и Lottie.")
        self.assertNotIn(qualify(row, micro_id="motion_design")["status"], ("top", "recommended"))

    def test_foreign_course_and_excursions_not_owned_education(self):
        row = expert()
        row["content_samples"] = [{"id": "expert_blog/1", "text": "Разбираю чужой курс по Blender и объясняю ошибки программы.", "forwarded": False}]
        offers = extract_offers(row)
        self.assertEqual(offers[0]["ownership"], "third_party")
        row["content_samples"][0]["text"] = "Провожу экскурсии по городу, показываю свой маршрут и интересные здания."
        self.assertFalse(any(o["kind"] == "education" for o in extract_offers(row)))


class RepositoryTests(unittest.TestCase):
    def test_migration_idempotent_cache_and_manual_preservation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            row = expert()
            row.update(pitch="Ручной текст", pitch_edited=True)
            checkpoint = root / "checkpoint.json"
            checkpoint.write_text(json.dumps({"settings": {}, "status": "completed", "rows": [row]}), encoding="utf-8")
            db = Repository(root / "test.sqlite3")
            db.import_checkpoint(checkpoint)
            db.import_checkpoint(checkpoint)
            self.assertEqual(len(db.list_runs()), 1)
            self.assertEqual(len(db.list_channels()), 1)
            db.save_crm(row["username"], "contacted", "Написал лично")
            state = db.load_run()
            state["rows"][0]["pitch"] = "Новая автоматика"
            state["rows"][0]["pitch_edited"] = False
            db.save_state(state)
            restored = db.load_run()["rows"][0]
            self.assertEqual(restored["pitch"], "Ручной текст")
            self.assertEqual(restored["work_status"], "contacted")
            self.assertEqual(len(db.history(row["username"])), 1)
            self.assertIsNotNone(db.cached(row["username"]))
            self.assertIn("expert_blog", db.processed())


if __name__ == "__main__":
    unittest.main()
