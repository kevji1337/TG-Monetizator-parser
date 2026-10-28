"""Process resources survive Streamlit's eviction of watched Python modules."""
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def shared_state():
    # Store data on sys itself: a watched module would be evicted on every edit.
    return vars(sys).setdefault('_tg_monetizer_runtime_v1', {
        'lock': threading.RLock(), 'runners': {},
        'browser_lock': threading.RLock(), 'browsers': {},
    })


def source_version():
    files = []
    for path in sorted((ROOT / 'monetizer').glob('*.py')):
        stat = path.stat()
        files.append((str(path.relative_to(ROOT)), stat.st_mtime_ns, stat.st_size))
    return tuple(files)


def _busy(runner):
    path = getattr(getattr(runner, 'repo', None), 'path', None)
    experiment = shared_state().get('experiments', {}).get(str(path.resolve())) if path else None
    with runner.lock:
        return (runner.state['status'] in ('running', 'stopping') or
                bool(runner.thread and runner.thread.is_alive()) or
                bool(experiment and not experiment['future'].done()))


def _existing_worker(key):
    # Adopt a job started before this registry was installed; never start a copy.
    for thread in threading.enumerate():
        if thread.name != 'monetizer-job':
            continue
        runner = getattr(getattr(thread, '_target', None), '__self__', None)
        if runner is not None and str(Path(runner.checkpoint).resolve()) == key:
            return runner
    return None


def acquire_runner(factory, checkpoint=None, version=None, registry=None):
    state = shared_state() if registry is None else registry
    key = str(Path(checkpoint or ROOT / 'results/monetizer/checkpoint.json').resolve())
    version = source_version() if version is None else version
    with state['lock']:
        record = state['runners'].get(key)
        if record is None:
            existing = _existing_worker(key)
            record = {'runner': existing or factory(), 'version': None if existing else version}
            state['runners'][key] = record
        elif record['version'] != version and not _busy(record['runner']):
            # Reload only between jobs. An active worker keeps its code and events.
            record = {'runner': factory(), 'version': version}
            state['runners'][key] = record
        return record['runner']
