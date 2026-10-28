import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from monetizer.runner import JobRunner, RunGuard
from monetizer.settings import Settings
from test_qualification import expert


class FakeSearch:
    pages = {}
    calls = []
    def __init__(self, *args):
        pass
    def fetch(self, query, page):
        self.calls.append((query, page))
        return self.pages.get((query, page), {"usernames": [], "exhausted": True, "restricted": False})
    def close(self):
        pass


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.guard_patch = patch("monetizer.runner.RESULTS_DIR", self.root)
        self.guard_patch.start()
        self.search_patch = patch("monetizer.runner.TGStatSearch", FakeSearch)
        self.search_patch.start()
        self.export_patch = patch("monetizer.runner.save_reports", return_value=([], []))
        self.export_patch.start()
        FakeSearch.pages, FakeSearch.calls = {}, []
    def tearDown(self):
        self.export_patch.stop()
        self.search_patch.stop()
        self.guard_patch.stop()
        self.temp.cleanup()

    def run_job(self, pages, audit, **settings):
        FakeSearch.pages = pages
        runner = JobRunner(self.root / "checkpoint.json")
        with patch("monetizer.runner.WebAuditor") as factory:
            factory.return_value.audit.side_effect = audit
            runner.start(Settings(target_count=2, workers=1, micro_id="motion_design", **settings), ["moушн"] , "Моушн")
            runner.thread.join(10)
        self.assertFalse(runner.thread.is_alive())
        return runner

    def test_continue_after_reject_until_qualified_target(self):
        def audit(name):
            row = expert(name)
            if name == "bad_blog":
                row.update(status="rejected", reasons="Просмотры ниже порога", avg_views=100, avg_views_exact=100)
            return row
        runner = self.run_job({("moушн", 0): {"usernames": ["bad_blog", "good_blog"], "exhausted": False},
                               ("moушн", 1): {"usernames": ["other_good"], "exhausted": True}}, audit)
        state = runner.snapshot()
        self.assertEqual(state["stop_reason"], "target_reached")
        self.assertEqual(len(state["rows"]), 3)
        self.assertEqual(state["query_stats"]["moушн"]["qualified"], 2)
        self.assertIn(("moушн", 1), FakeSearch.calls)

    def test_audit_budget_and_restricted_are_distinct(self):
        runner = self.run_job({("moушн", 0): {"usernames": ["good_blog", "other_good"], "exhausted": True}}, expert, max_audits=1)
        self.assertEqual(runner.snapshot()["stop_reason"], "audit_budget")
        self.assertEqual(len(runner.snapshot()["rows"]), 1)
        # New run, old usernames excluded; restriction is not counted as an empty keyword.
        FakeSearch.pages = {("moушн", 0): {"usernames": [], "exhausted": True, "restricted": True}}
        with patch("monetizer.runner.WebAuditor"):
            runner.start(Settings(target_count=2, micro_id="motion_design"), ["moушн"], "Моушн")
            runner.thread.join(10)
        state = runner.snapshot()
        self.assertEqual(state["stop_reason"], "restricted")
        self.assertEqual(state["query_stats"]["moушн"]["access_errors"], 1)

    def test_new_only_and_explicit_cache_reuse(self):
        runner = self.run_job({("moушн", 0): {"usernames": ["good_blog"], "exhausted": True}}, expert)
        FakeSearch.pages = {("moушн", 0): {"usernames": ["good_blog"], "exhausted": True}}
        with patch("monetizer.runner.WebAuditor") as factory:
            runner.start(Settings(target_count=1, micro_id="motion_design"), ["moушн"], "Моушн")
            runner.thread.join(10)
            factory.return_value.audit.assert_not_called()
        self.assertEqual(runner.snapshot()["rows"], [])
        with patch("monetizer.runner.WebAuditor") as factory:
            runner.start(Settings(target_count=1, micro_id="motion_design", new_only=False), ["moушн"], "Моушн")
            runner.thread.join(10)
            factory.return_value.audit.assert_not_called()
        self.assertEqual(len(runner.snapshot()["rows"]), 1)

    def test_resume_preserves_unfinished_queue_and_skips_finished(self):
        runner = self.run_job({("moушн", 0): {"usernames": ["good_blog", "other_good"], "exhausted": True}}, expert, max_audits=1)
        self.assertIn("other_good", runner.snapshot()["queue"])
        restored = JobRunner(self.root / "checkpoint.json")
        before = len(FakeSearch.calls)
        with patch("monetizer.runner.WebAuditor") as factory:
            factory.return_value.audit.side_effect = expert
            restored.start(Settings(target_count=2, workers=1, micro_id="motion_design", max_audits=3), resume=True)
            restored.thread.join(10)
            self.assertEqual([call.args[0] for call in factory.return_value.audit.call_args_list], ["other_good"])
        self.assertEqual(restored.snapshot()["stop_reason"], "target_reached")
        self.assertEqual(len(FakeSearch.calls), before)

    def test_time_budget_no_browser_fetch(self):
        import itertools
        with patch("monetizer.runner.time.monotonic", side_effect=itertools.count().__next__):
            runner = self.run_job({}, expert, max_minutes=.01)
        self.assertEqual(runner.snapshot()["stop_reason"], "time_budget")
        self.assertEqual(FakeSearch.calls, [])

    def test_warmup_rotates_before_reusing_keyword(self):
        runner = JobRunner(self.root / "checkpoint.json")
        FakeSearch.pages = {("first", 0): {"usernames": ["first_blog"], "exhausted": False},
                            ("second", 0): {"usernames": ["second_blog"], "exhausted": True}}
        with patch("monetizer.runner.WebAuditor") as factory:
            factory.return_value.audit.side_effect = expert
            runner.start(Settings(target_count=2, workers=1, micro_id="motion_design"), ["first", "second"], "Моушн")
            runner.thread.join(10)
        self.assertEqual(FakeSearch.calls[:2], [("first", 0), ("second", 0)])
        self.assertEqual(runner.snapshot()["stop_reason"], "target_reached")

    def test_second_process_view_never_marks_active_job_interrupted(self):
        runner = JobRunner(self.root / "checkpoint.json")
        state = {"run_id": "active_run", "status": "running", "stage": "Поиск", "settings": Settings().to_dict(),
                 "rows": [], "candidates": {}, "logs": [], "paths": []}
        runner.repo.save_state(state)
        with RunGuard():
            observer = JobRunner(self.root / "checkpoint.json")
            self.assertTrue(observer.snapshot()["external_owner"])
            self.assertEqual(observer.repo.load_run()["status"], "running")
        state.update(status="completed")
        runner.repo.save_state(state)
        self.assertFalse(observer.snapshot()["external_owner"])


if __name__ == "__main__":
    unittest.main()
