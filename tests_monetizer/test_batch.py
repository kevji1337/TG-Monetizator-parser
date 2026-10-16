import copy
import json
import tempfile
import unittest
from pathlib import Path

from monetizer.batch import create_batch, response_template, preview_response, import_response, batch_zip
from monetizer.qualification import qualify
from monetizer.repository import Repository
from test_qualification import expert


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Repository(Path(self.temp.name) / "test.sqlite3")
        self.row = qualify(expert(), micro_id="motion_design")
        self.state = {"run_id": "run_test", "started_at": "", "niche": "Моушн", "rows": [self.row]}
        self.repo.save_state(self.state)
        self.repo.save_crm("expert_blog", "contacted", "Уже написал")
        self.repo.save_draft("expert_blog", {"pitch": "Мой ручной текст", "pitch_edited": True})
        self.packet = create_batch(self.repo, "run_test")
        self.response = response_template(self.packet)
        item = self.response["channels"][0]
        item.update(channel_type="expert", relevance="matched", reason="Есть собственные разборы",
                    message="Здравствуйте! Обсудим разборы анимации?")
        item["evidence"] = [{"source_id": p["id"], "url": p["url"], "quote": p["text"][:40]} for p in self.row["content_samples"]]

    def tearDown(self):
        self.temp.cleanup()

    def test_preview_no_mutation_import_idempotent_manual_untouched(self):
        result = preview_response(self.repo, self.response)
        self.assertEqual(len(result["reviews"]), 1)
        self.assertNotIn("ai_review", self.repo.list_channels()[0])
        self.assertEqual(import_response(self.repo, self.response)["count"], 1)
        self.assertTrue(import_response(self.repo, self.response)["already_imported"])
        row = self.repo.list_channels()[0]
        self.assertEqual(row["pitch"], "Мой ручной текст")
        self.assertEqual(row["work_status"], "contacted")
        self.assertEqual(row["ai_review"]["channel_type"], "expert")
        self.assertFalse(row["ai_review"]["stale"])
        self.assertTrue(batch_zip(self.packet).startswith(b"PK"))

    def test_foreign_ids_fake_quote_and_wrong_link_reject_atomically(self):
        for edit in (lambda r: r["channels"][0].update(channel_id="other_blog"),
                     lambda r: r.update(run_id="wrong_run"),
                     lambda r: r["channels"][0]["evidence"][0].update(quote="Выдуманный успех"),
                     lambda r: r["channels"][0]["evidence"][0].update(url="https://evil.test/")):
            response = copy.deepcopy(self.response)
            edit(response)
            with self.assertRaises(ValueError):
                import_response(self.repo, response)
            self.assertNotIn("ai_review", self.repo.list_channels()[0])

    def test_stale_data_and_two_source_requirement(self):
        response = copy.deepcopy(self.response)
        response["channels"][0]["evidence"] = response["channels"][0]["evidence"][:1]
        with self.assertRaises(ValueError):
            import_response(self.repo, response)
        self.row["content_samples"][0]["text"] += " Новая редакция."
        self.repo.save_state(self.state)
        with self.assertRaises(ValueError):
            import_response(self.repo, self.response)

    def test_identical_texts_are_not_independent_ai_evidence(self):
        self.row['about_text'] = ''
        self.row['content_samples'][1]['text'] = self.row['content_samples'][0]['text']
        self.repo.save_state(self.state)
        packet = create_batch(self.repo, 'run_test')
        response = response_template(packet)
        response['channels'][0].update(channel_type='expert', relevance='matched', reason='Разборы', message='')
        response['channels'][0]['evidence'] = [
            {'source_id': p['id'], 'url': p['url'], 'quote': p['text'][:40]} for p in self.row['content_samples']]
        with self.assertRaises(ValueError):
            import_response(self.repo, response)

    def test_malformed_field_types_rejected_cleanly(self):
        for edit in (lambda r: r.update(batch_id={}),
                     lambda r: r.update(run_id=[]),
                     lambda r: r['channels'][0].update(channel_id=[]),
                     lambda r: r['channels'][0].update(channel_type=[]),
                     lambda r: r['channels'][0]['evidence'][0].update(source_id=[])):
            response = copy.deepcopy(self.response)
            edit(response)
            with self.assertRaises(ValueError):
                import_response(self.repo, response)
            self.assertNotIn('ai_review', self.repo.list_channels()[0])

    def test_valid_partial_response_and_invalid_offer_price(self):
        self.state["rows"].append(qualify(expert("second_blog"), micro_id="motion_design"))
        self.repo.save_state(self.state)
        packet = create_batch(self.repo, "run_test", size=20)
        response = copy.deepcopy(self.response)
        response.update(batch_id=packet["batch_id"])
        self.assertEqual(import_response(self.repo, response)["count"], 1)
        bad = copy.deepcopy(response)
        proof = copy.deepcopy(bad["channels"][0]["evidence"][0])
        bad["channels"][0]["offers"] = [{**proof, "kind": "education", "ownership": "self_reported", "prices": ["5000 USD"]}]
        with self.assertRaises(ValueError):
            import_response(self.repo, bad)

    def test_prohibited_promise_rejects_before_writing(self):
        bad = copy.deepcopy(self.response)
        bad["channels"][0]["message"] = "Гарантирую продажи"
        with self.assertRaises(ValueError):
            import_response(self.repo, bad)
        self.assertNotIn("ai_review", self.repo.list_channels()[0])

    def test_review_becomes_stale_after_public_content_changes(self):
        import_response(self.repo, self.response)
        self.row["about_text"] += " Новое описание."
        self.repo.save_state(self.state)
        self.assertTrue(self.repo.list_channels()[0]["ai_review"]["stale"])

    def test_other_search_context_does_not_reuse_relevance(self):
        import_response(self.repo, self.response)
        self.row["analysis_context"] = {"topic": "Ландшафт", "micro_id": "landscape_design", "niche_id": "6"}
        self.repo.save_state(self.state)
        self.assertTrue(self.repo.list_channels()[0]["ai_review"]["stale"])

    def test_blank_response_template_is_not_an_analysis(self):
        with self.assertRaises(ValueError):
            import_response(self.repo, response_template(self.packet))


if __name__ == "__main__":
    unittest.main()
