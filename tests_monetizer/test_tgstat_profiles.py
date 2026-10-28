"""Profile/session regressions using temporary data and fake Chrome only."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from monetizer.browser_session import configure_session_restore, graceful_quit, BrowserStillRunning
from monetizer.profile_search import ProfileSearch, profile_plan, reconcile_profiles
from monetizer.repository import Repository
from monetizer.runner import RunGuard
from monetizer.settings import Settings
from monetizer.tgstat_profiles import TGStatProfiles, profile_directory


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        patcher = patch('monetizer.tgstat_profiles.ROOT', self.root / 'profiles')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = Repository(self.root / 'profiles.sqlite3')
        self.store = TGStatProfiles(self.repo)

    def test_delete_restore_preserves_native_data_and_survives_reload(self):
        created = self.store.add('Рабочий')
        self.assertIsInstance(created, str)
        marker = profile_directory(created) / 'fixture.txt'
        marker.write_text('native profile fixture', encoding='utf-8')
        self.store.delete(created)
        reloaded = TGStatProfiles(Repository(self.repo.path))
        self.assertNotIn(created, [row['id'] for row in reloaded.list()])
        self.assertEqual(reloaded.archived()[0]['id'], created)
        self.assertEqual(marker.read_text(encoding='utf-8'), 'native profile fixture')
        reloaded.restore(created)
        self.assertIn(created, [row['id'] for row in reloaded.list()])
        self.assertEqual(reloaded.archived(), [])

    def test_default_removal_is_durable_and_does_not_recreate_itself(self):
        self.store.delete('default')
        self.assertEqual(TGStatProfiles(Repository(self.repo.path)).list(), [])
        with self.assertRaisesRegex(ValueError, 'Добавьте'):
            profile_plan(self.repo, Settings())
        self.store.restore('default')
        self.assertEqual([row['id'] for row in self.store.list()], ['default'])

    def test_deleted_accounts_are_removed_from_resumed_plan(self):
        created = self.store.add('Первый')
        remaining = self.store.add('Второй')
        plan = profile_plan(self.repo, Settings(tgstat_profile_id=created))
        self.store.delete(created)
        self.store.delete('default')
        updated = reconcile_profiles(self.repo, plan)
        self.assertEqual(updated['order'], [remaining])
        self.assertEqual(updated['active'], remaining)
        self.assertFalse(updated['exhausted'])
        self.assertEqual(plan['active'], created)  # Historical input is untouched.

    def test_delete_is_blocked_during_job_or_open_profile(self):
        with RunGuard(self.repo.path), self.assertRaises(RuntimeError):
            self.store.delete('default')
        with patch('monetizer.diagnostics.retained_sessions', return_value=[{'profile_id': 'default'}]):
            with self.assertRaisesRegex(ValueError, 'закройте'):
                self.store.delete('default')
        self.assertEqual([row['id'] for row in self.store.list()], ['default'])

    def test_restore_name_conflict_is_atomic(self):
        deleted = self.store.add('Рабочий')
        self.store.delete(deleted)
        active = self.store.add('Рабочий')
        with self.assertRaisesRegex(ValueError, 'переименуйте'):
            self.store.restore(deleted)
        self.assertEqual(self.store.archived()[0]['id'], deleted)
        self.assertIn(active, [row['id'] for row in self.store.list()])
        self.assertNotIn(deleted, [row['id'] for row in self.store.list()])

    def test_restricted_profile_switch_restarts_page_and_saves_skip(self):
        first = self.store.add('Первый')
        settings = Settings(tgstat_profile_id=first, tgstat_auto_switch=True)
        blocked, available = Mock(), Mock()
        blocked.last_restriction = 'TGStat: restricted'
        blocked.fetch.return_value = {'usernames': [], 'exhausted': True, 'restricted': True}
        available.fetch.return_value = {'usernames': ['expert_blog'], 'exhausted': False, 'restricted': False}
        factory = Mock(side_effect=[blocked, available])
        changes = []
        search = ProfileSearch(settings, profile_plan(self.repo, settings), None, Mock(), 999, Mock(), changes.append, factory)
        response = search.fetch('tgstat:34', 7)
        blocked.close.assert_called_once_with(force=True)
        blocked.fetch.assert_called_once_with('tgstat:34', 7)
        available.fetch.assert_called_once_with('tgstat:34', 0)
        self.assertEqual(response['page_number'], 0)
        self.assertEqual(changes[-1]['active'], 'default')
        self.assertIn(first, changes[-1]['skipped'])
        self.assertEqual(factory.call_args_list[-1].args[3], 999)  # Same deadline.
        self.assertEqual(settings.tgstat_profile_id, first)

    def test_exhausted_profile_queue_does_not_loop_or_reopen_on_resume(self):
        settings = Settings(tgstat_auto_switch=True)
        blocked = Mock(last_restriction='TGStat: restricted')
        blocked.fetch.return_value = {'usernames': [], 'exhausted': True, 'restricted': True}
        factory = Mock(return_value=blocked)
        changes = []
        search = ProfileSearch(settings, profile_plan(self.repo, settings), None, Mock(), 999, Mock(), changes.append, factory)
        self.assertTrue(search.fetch('tgstat:34')['restricted'])
        self.assertTrue(search.fetch('tgstat:53')['restricted'])
        resumed = ProfileSearch(settings, changes[-1], None, Mock(), 999, Mock(), changes.append, factory)
        self.assertTrue(resumed.fetch('tgstat:34')['restricted'])
        factory.assert_called_once()
        blocked.fetch.assert_called_once()
        self.assertTrue(changes[-1]['exhausted'])


class BrowserSessionTests(unittest.TestCase):
    def test_restore_preferences_keep_other_settings_and_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            prefs = profile / 'Default' / 'Preferences'
            prefs.parent.mkdir()
            original = {'profile': {'name': 'Эксперт'}, 'session': {'restore_on_startup': 5}, 'other': 42}
            prefs.write_text(json.dumps(original, ensure_ascii=False), encoding='utf-8')
            configure_session_restore(profile)
            configure_session_restore(profile)
            current = json.loads(prefs.read_text(encoding='utf-8'))
            self.assertEqual(current['session']['restore_on_startup'], 1)
            self.assertEqual(current['profile'], original['profile'])
            self.assertEqual(current['other'], 42)
            self.assertEqual(json.loads(prefs.with_name('Preferences.before-session-restore-v1.json').read_text(encoding='utf-8')), original)

    def driver(self):
        driver = Mock()
        driver._monetizer_quit_done = False
        driver.browser_pid = 123
        driver.command_executor._client_config.timeout = 120
        return driver

    def test_graceful_close_waits_before_cleanup_and_is_idempotent(self):
        driver = self.driver()
        with patch('monetizer.browser_session.BrowserExit') as watcher_factory:
            watcher = watcher_factory.return_value
            watcher.already_exited = False
            watcher.wait.return_value = True
            driver.execute_cdp_cmd.side_effect = lambda *args: driver.service.stop.assert_not_called()
            watcher.wait.side_effect = lambda seconds: (driver.service.stop.assert_not_called() or True)
            graceful_quit(driver)
            graceful_quit(driver)
            driver.execute_cdp_cmd.assert_called_once_with('Browser.close', {})
            watcher.wait.assert_called_once_with(15)
            driver.service.stop.assert_called_once()
            driver.command_executor.close.assert_called_once()
            watcher.close.assert_called_once()
            self.assertEqual(driver.command_executor._client_config.timeout, 120)

    def test_failed_close_keeps_browser_and_service_for_retry(self):
        driver = self.driver()
        with patch('monetizer.browser_session.BrowserExit') as factory:
            watcher = factory.return_value
            watcher.already_exited = False
            watcher.wait.return_value = False
            with self.assertRaises(BrowserStillRunning):
                graceful_quit(driver)
            driver.service.stop.assert_not_called()
            driver.command_executor.close.assert_not_called()
            self.assertFalse(driver._monetizer_quit_done)
            watcher.close.assert_called_once()
