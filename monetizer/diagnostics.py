"""Local rotating logs. No HTTP headers, cookies, page HTML or passwords."""
import functools
import logging
from logging.handlers import RotatingFileHandler
import re
import threading
import time
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .runtime import shared_state

LOG_DIR = Path(os.environ.get('MONETIZER_LOG_DIR', Path(__file__).resolve().parents[1] / 'results/monetizer/logs'))
LOG_PATH = LOG_DIR / 'application.log'
_lock = shared_state()['browser_lock']
_retained = shared_state()['browsers']


def redact(text):
    text = re.sub(r'''(?im)\b(authorization|cookie|set-cookie|password|api_key|token)["']?\s*[:=]\s*[^\r\n]+''', r'\1=[REDACTED]', str(text))
    return re.sub(r'https?://[^\s<>"\']+', lambda m: safe_url(m.group()), text)


def safe_url(value):
    try:
        parts = urlsplit(str(value))
        host = parts.hostname or ''
        if ':' in host:
            host = '[' + host + ']'
        if parts.port:
            host += ':' + str(parts.port)
        return urlunsplit((parts.scheme, host, parts.path, '', ''))
    except ValueError:
        return '[invalid URL]'


class SafeFormatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))


def logger():
    result = logging.getLogger('monetizer.diagnostics')
    with _lock:
        if not result.handlers:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(LOG_PATH, maxBytes=5_000_000, backupCount=3, encoding='utf-8')
            handler.setFormatter(SafeFormatter('%(asctime)s %(levelname)s [%(threadName)s] %(message)s'))
            result.addHandler(handler)
            result.setLevel(logging.DEBUG)
            result.propagate = False
    return result


def diagnosed(stage):
    def decorate(function):
        @functools.wraps(function)
        def run(self, *args, **kwargs):
            started = time.perf_counter()
            self.stage = stage
            logger().info('session=%s stage=%s started', self.session_id, stage)
            try:
                result = function(self, *args, **kwargs)
            except Exception as exc:
                from .audit import Cancelled
                if isinstance(exc, Cancelled):
                    self.preserve_on_close = False
                    logger().info('session=%s stage=%s cancelled', self.session_id, stage)
                elif getattr(exc, 'manual_retry', False):
                    logger().info('session=%s stage=%s retry after manual login', self.session_id, stage)
                else:
                    self.preserve_on_close = True
                    logger().exception('session=%s stage=%s failed url=%s', self.session_id, stage, self.location())
                raise
            logger().info('session=%s stage=%s completed elapsed=%.3fs', self.session_id, stage, time.perf_counter() - started)
            return result
        return run
    return decorate


def retain(search):
    with _lock:
        _retained[search.session_id] = search


def retained_sessions(include_url=True):
    with _lock:
        return [{'session_id': s.session_id, 'stage': s.stage, 'url': s.location() if include_url else '',
                 'profile_id': getattr(s.settings, 'tgstat_profile_id', 'default'),
                 'purpose': getattr(s, 'purpose', 'diagnostics')} for s in _retained.values()]


def close_retained(session_id):
    with _lock:
        search = _retained.get(session_id)
    if search:
        result = search.close(force=True)
        with _lock:
            _retained.pop(session_id, None)
        return result


def check_retained_login(session_id):
    with _lock:
        search = _retained.get(session_id)
    if search is None:
        raise ValueError('Окно профиля уже закрыто')
    return search.check_login()


def log_tail(limit=24000):
    if not LOG_PATH.exists():
        return 'Лог пока пуст.'
    with LOG_PATH.open('rb') as file:
        file.seek(0, 2)
        file.seek(max(0, file.tell() - limit))
        return file.read(limit).decode('utf-8', errors='replace')


class BrowserControl:
    def __init__(self, on_state=None):
        self.resume = threading.Event()
        self.on_state = on_state or (lambda state: None)

    def update(self, **state):
        self.on_state(state)
