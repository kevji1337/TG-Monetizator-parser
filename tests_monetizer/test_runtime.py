import importlib
import threading
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from monetizer import runtime
from launch_monetizer import server_command


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.registry = {'lock': threading.RLock(), 'runners': {}}
        self.runner = SimpleNamespace(lock=threading.Lock(), state={'status': 'idle'}, thread=None)
        self.factory = Mock(return_value=self.runner)
        self.path = Path(__file__).parent / 'temporary-checkpoint.json'

    def acquire(self, version):
        return runtime.acquire_runner(self.factory, self.path, version, self.registry)

    def test_rerender_returns_same_worker_without_initializing_database_again(self):
        self.assertIs(self.acquire('v1'), self.runner)
        self.assertIs(self.acquire('v1'), self.runner)
        self.factory.assert_called_once()

    def test_source_change_defers_replacement_until_job_finishes(self):
        self.acquire('v1')
        for status in ('running', 'stopping'):
            self.runner.state['status'] = status
            self.assertIs(self.acquire('v2'), self.runner)
        self.factory.assert_called_once()
        self.runner.state['status'] = 'completed'
        replacement = SimpleNamespace(lock=threading.Lock(), state={'status': 'idle'}, thread=None)
        self.factory.return_value = replacement
        self.assertIs(self.acquire('v2'), replacement)
        self.assertEqual(self.factory.call_count, 2)

    def test_alive_thread_prevents_duplicate_worker_even_after_status_changed(self):
        self.acquire('v1')
        self.runner.thread = Mock()
        self.runner.thread.is_alive.return_value = True
        self.assertIs(self.acquire('v2'), self.runner)
        self.factory.assert_called_once()

    def test_resource_registry_and_browser_handles_survive_module_reload(self):
        state = runtime.shared_state()
        importlib.reload(runtime)
        self.assertIs(runtime.shared_state(), state)
        from monetizer import diagnostics
        handles = diagnostics._retained
        importlib.reload(diagnostics)
        self.assertIs(diagnostics._retained, handles)

    def test_background_job_remains_controllable_through_reload(self):
        stop = threading.Event()
        started = threading.Event()
        def work():
            started.set()
            stop.wait(5)
        self.runner.state['status'] = 'running'
        self.runner.thread = threading.Thread(target=work, name='test-background-job')
        self.runner.thread.start()
        self.addCleanup(self.runner.thread.join, 5)
        self.addCleanup(stop.set)
        self.assertTrue(started.wait(2))
        self.acquire('v1')
        importlib.reload(runtime)
        same = self.acquire('v2')
        self.assertIs(same.thread, self.runner.thread)
        self.assertTrue(same.thread.is_alive())
        self.factory.assert_called_once()
        stop.set()
        same.thread.join(2)
        self.assertFalse(same.thread.is_alive())

    def test_existing_worker_is_adopted_without_creating_second_runner(self):
        with patch.object(runtime, '_existing_worker', return_value=self.runner):
            self.assertIs(self.acquire('v1'), self.runner)
        self.factory.assert_not_called()

    def test_auto_reload_enabled_for_both_launchers_and_output_folders_ignored(self):
        for dev in (False, True):
            args = server_command(dev)
            self.assertIn('--server.runOnSave=true', args)
            self.assertIn('--server.fileWatcherType=poll', args)
            self.assertIn('--server.address=127.0.0.1', args)
        config = tomllib.loads((runtime.ROOT / '.streamlit/config.toml').read_text(encoding='utf-8'))
        self.assertTrue(config['server']['runOnSave'])
        for folder in ('results', 'reports', '.venv', '.git'):
            self.assertIn(folder, config['server']['folderWatchBlacklist'])
