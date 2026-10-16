import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
import zipfile

import requests
from monetizer import browser
from monetizer.diagnostics import BrowserControl, redact, retained_sessions, close_retained
from monetizer.search import TGStatSearch, RetryAfterLogin
from monetizer.settings import Settings
from monetizer.tgstat_filters import filter_spec
from monetizer.audit import Cancelled


class DriverPreparationTests(unittest.TestCase):
    def test_matching_cached_driver_never_downloads(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'shared/driver.exe'
            source.parent.mkdir()
            source.write_bytes(b'cached')
            target = Path(directory) / 'project'
            with patch.object(browser, 'DRIVER_DIR', target), patch.object(browser, 'file_version', return_value='155.0.1.2'), \
                    patch.object(browser, 'local_driver_candidates', return_value=[source]), \
                    patch.object(browser, 'driver_version', return_value='155.0.1.2'), patch('requests.Session') as network:
                path, major = browser.prepare_driver('chrome.exe', log=lambda m: None)
                network.assert_not_called()
                self.assertEqual(major, 155)
                self.assertEqual(Path(path).read_bytes(), b'cached')
                self.assertEqual(source.read_bytes(), b'cached')

    def test_driver_download_uses_https_verification(self):
        package = io.BytesIO()
        with zipfile.ZipFile(package, 'w') as archive:
            archive.writestr('chromedriver-win64/chromedriver.exe', b'driver')
            archive.writestr('chromedriver-linux64/chromedriver', b'driver')
        manifest = {'milestones': {'155': {'downloads': {'chromedriver': [
            {'platform': name, 'url': 'https://storage.googleapis.com/chrome-for-testing-public/155/' + name}
            for name in ['win64', 'win32', 'linux64']]}}}}
        with tempfile.TemporaryDirectory() as directory, patch.object(browser, 'DRIVER_DIR', Path(directory)), \
                patch.object(browser, 'file_version', return_value='155.0.1.2'), \
                patch.object(browser, 'local_driver_candidates', return_value=[]), \
                patch.object(browser, 'driver_version', return_value='155.0.1.2'), patch('requests.Session') as factory:
            session = factory.return_value.__enter__.return_value
            metadata, download = MagicMock(), MagicMock()
            metadata.json.return_value = manifest
            download.content = package.getvalue()
            session.get.side_effect = [metadata, download]
            path, _ = browser.prepare_driver('chrome.exe', log=lambda m: None)
            self.assertTrue(Path(path).exists())
            for call in session.get.call_args_list:
                self.assertTrue(call.args[0].startswith('https://'))
                self.assertNotEqual(call.kwargs.get('verify'), False)

    def test_tls_failure_is_actionable_and_does_not_disable_checks(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(browser, 'DRIVER_DIR', Path(directory)), \
                patch.object(browser, 'file_version', return_value='155.0.1.2'), \
                patch.object(browser, 'local_driver_candidates', return_value=[]), patch('requests.Session') as factory:
            factory.return_value.__enter__.return_value.get.side_effect = requests.exceptions.SSLError('TLS failure')
            with self.assertRaisesRegex(RuntimeError, 'TLS остаётся включённой'):
                browser.prepare_driver('chrome.exe', log=lambda m: None)


class BrowserControlTests(unittest.TestCase):
    def search(self, **settings):
        result = TGStatSearch(Settings(search_mode='categories', category_ids=['34'], **settings), log=lambda m: None)
        result.driver = MagicMock()
        result.driver.current_url = 'https://tgstat.ru/channels/search'
        self.addCleanup(close_retained, result.session_id)
        return result

    def test_error_browser_is_kept_until_explicit_close(self):
        search = self.search()
        driver = search.driver
        search.preserve_on_close = True
        search.close()
        driver.quit.assert_not_called()
        self.assertIn(search.session_id, [s['session_id'] for s in retained_sessions()])
        close_retained(search.session_id)
        driver.quit.assert_called_once()
        self.assertIsNone(search.driver)

    def test_success_browser_closes_normally(self):
        search = self.search()
        driver = search.driver
        search.close()
        driver.quit.assert_called_once()

    def test_keep_disabled_allows_cleanup_after_error(self):
        search = self.search(keep_browser_on_error=False)
        driver = search.driver
        search.preserve_on_close = True
        search.close()
        driver.quit.assert_called_once()

    def test_manual_login_wait_is_visible_and_can_continue(self):
        search = self.search(browser_wait_seconds=1)
        states = []
        def update(state):
            states.append(state)
            if state['waiting']:
                search.control.resume.set()
        search.control = BrowserControl(update)
        self.assertTrue(search._wait_for_login())
        self.assertTrue(states[0]['waiting'])
        self.assertFalse(states[-1]['waiting'])

    def test_manual_login_timeout_is_partial_not_fake_success(self):
        search = self.search(browser_wait_seconds=0)
        search.applied_filters = filter_spec(search.settings, '34')
        search.driver.execute_script.side_effect = [[], None, {'http': 200, 'status': 'restricted'}]
        search._wait = MagicMock()
        self.assertFalse(search._request(MagicMock()))
        self.assertTrue(search.preserve_on_close)

    def test_retry_after_login_restarts_requested_page(self):
        search = self.search()
        result = {'usernames': ['expert_blog'], 'exhausted': False}
        search._fetch = MagicMock(side_effect=[RetryAfterLogin(), result])
        self.assertEqual(search.fetch('tgstat:34', 2), result)
        self.assertEqual(search._fetch.call_count, 2)
        self.assertEqual(search.current_page, -1)

    def test_stop_cancels_wait_without_holding_browser(self):
        search = self.search(browser_wait_seconds=1)
        search.stop.set()
        search._fetch = MagicMock(side_effect=Cancelled())
        search.preserve_on_close = True
        with self.assertRaises(Cancelled):
            search.fetch('tgstat:34')
        self.assertFalse(search.preserve_on_close)

    def test_diagnostics_scrub_credentials_and_url_query(self):
        value = redact("Cookie: session=secret\nURL https://user:pass@tgstat.ru/path?token=hidden#fragment\n'password': 'secret2'")
        for secret in ['session=secret', 'user:pass', 'hidden', 'secret2']:
            self.assertNotIn(secret, value)
        self.assertIn('https://tgstat.ru/path', value)
