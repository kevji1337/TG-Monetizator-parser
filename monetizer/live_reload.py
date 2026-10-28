"""Repair a mixed UI generation without touching an active parser thread."""
import importlib
import inspect
import sys
import threading
from pathlib import Path

BOOTSTRAP_VERSION = 2


def refresh_code():
    """Import a new module generation; never mutate the globals of a running job."""
    root = Path(__file__).resolve().parent
    version = tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in sorted(root.glob('*.py')))
    lock = vars(sys).setdefault('_monetizer_reload_lock', threading.RLock())
    with lock:
        previous = getattr(sys, '_monetizer_import_generation', None)
        if previous == version:
            return
        sys._monetizer_import_generation = version
        # Existing objects keep their original module dictionaries. Reloading them
        # in place would change exception classes/globals in a running worker.
        stable = {'monetizer.live_reload'}
        for name in list(sys.modules):
            if name.startswith('monetizer.') and name not in stable:
                sys.modules.pop(name, None)
    importlib.invalidate_caches()


def ui_api():
    preferences = importlib.import_module('monetizer.preferences')
    quality = importlib.import_module('monetizer.ui_quality')
    if ('disabled' not in inspect.signature(preferences.PersistentUI).parameters or
            'disabled' not in inspect.signature(preferences.api_key_field).parameters):
        preferences = importlib.reload(preferences)
    if 'disabled' not in inspect.signature(quality.profile_panel).parameters:
        quality = importlib.reload(quality)
    return preferences, quality.profile_panel
