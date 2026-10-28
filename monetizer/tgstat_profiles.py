"""Named local Chrome sessions, selected manually for each search."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4
from .export import atomic_json

DEFAULT_PROFILE = 'default'
DEFAULT_NAME = 'Основной (прежний вход)'
ROOT = Path(__file__).resolve().parents[1] / 'results' / 'monetizer'
SCHEMA = '''CREATE TABLE IF NOT EXISTS tgstat_profiles(
    id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL)'''
ARCHIVE_SCHEMA = '''CREATE TABLE IF NOT EXISTS tgstat_profile_archives(
    id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL, created_at TEXT NOT NULL, deleted_at TEXT NOT NULL)'''
AUTH_LABELS = {'authenticated': 'Вход виден на странице TGStat', 'not_authenticated': 'Требуется вход',
               'unknown': 'Вход не подтверждён', 'not_checked': 'Вход ещё не проверен'}


def session_state(profile_id):
    validate_profile_id(profile_id)
    folder = ROOT / 'chrome_profile' if profile_id == DEFAULT_PROFILE else ROOT / 'chrome_profiles' / profile_id
    try:
        data = json.loads((folder / 'session_state.json').read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def update_session_state(profile_id, **updates):
    folder = profile_directory(profile_id)
    data = session_state(profile_id)
    data.update(updates)
    atomic_json(folder / 'session_state.json', data)


def validate_profile_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'default|[a-f0-9]{32}', value):
        raise ValueError('Выберите сохранённый профиль TGStat')
    return value


def profile_directory(profile_id, create=False):
    validate_profile_id(profile_id)
    # Preserve the old directory and its sign-in exactly where it already lives.
    path = ROOT / 'chrome_profile' if profile_id == DEFAULT_PROFILE else ROOT / 'chrome_profiles' / profile_id
    if create or profile_id == DEFAULT_PROFILE:
        path.mkdir(parents=True, exist_ok=True)
    elif not path.is_dir():
        raise ValueError('Папка профиля TGStat отсутствует. Выберите существующий профиль или создайте новый.')
    return path


def validate_name(name):
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or re.search(r'[\x00-\x1f\x7f]', name):
        raise ValueError('Название профиля: 1–80 символов без переносов строк')
    name = name.strip()
    if name.casefold() == DEFAULT_NAME.casefold():
        raise ValueError('Это название зарезервировано для основного профиля')
    return name


class TGStatProfiles:
    def __init__(self, repo):
        self.repo = repo

    def list(self):
        with self.repo.connect() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tgstat_profiles'").fetchone()
            rows = [dict(row) for row in db.execute('SELECT id,name,created_at FROM tgstat_profiles ORDER BY created_at,id')] if exists else []
            archived_exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tgstat_profile_archives'").fetchone()
            default_deleted = archived_exists and db.execute('SELECT 1 FROM tgstat_profile_archives WHERE id=?', (DEFAULT_PROFILE,)).fetchone()
        return ([] if default_deleted else [{'id': DEFAULT_PROFILE, 'name': DEFAULT_NAME, 'created_at': ''}]) + rows

    def archived(self):
        with self.repo.connect() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tgstat_profile_archives'").fetchone()
            return [dict(row) for row in db.execute('SELECT * FROM tgstat_profile_archives ORDER BY deleted_at DESC,id')] if exists else []

    def name(self, profile_id):
        current = next((row['name'] for row in self.list() if row['id'] == profile_id), None)
        return current or next((row['name'] + ' (удалён)' for row in self.archived() if row['id'] == profile_id), 'Профиль отсутствует')

    def add(self, name):
        name = validate_name(name)
        profile_id = uuid4().hex
        try:
            with self.repo.connect() as db:
                db.execute(SCHEMA)
                db.execute('BEGIN IMMEDIATE')
                db.execute(ARCHIVE_SCHEMA)
                default_active = not db.execute('SELECT 1 FROM tgstat_profile_archives WHERE id=?', (DEFAULT_PROFILE,)).fetchone()
                if db.execute('SELECT count(*) FROM tgstat_profiles').fetchone()[0] + int(default_active) >= 20:
                    raise ValueError('Можно сохранить до 20 активных профилей')
                db.execute('INSERT INTO tgstat_profiles(id,name,name_key,created_at) VALUES(?,?,?,?)',
                           (profile_id, name, name.casefold(), datetime.now(timezone.utc).isoformat()))
                profile_directory(profile_id, create=True)
        except sqlite3.IntegrityError:
            raise ValueError('Профиль с таким названием уже существует') from None
        return profile_id

    def delete(self, profile_id):
        """Remove from active use; keep native Chrome data for explicit recovery."""
        from .runner import RunGuard
        from .diagnostics import retained_sessions
        from .browser_session import BrowserExit
        validate_profile_id(profile_id)
        with RunGuard(self.repo.path):
            if any(item['profile_id'] == profile_id for item in retained_sessions(include_url=False)):
                raise ValueError('Сначала закройте Chrome этого профиля кнопкой в панели')
            saved = session_state(profile_id)
            pid = saved.get('browser_pid')
            if saved.get('close_status') == 'open' and type(pid) is int and pid > 0:
                watcher = BrowserExit(pid)
                try:
                    if not watcher.wait(0):
                        raise ValueError('Chrome этого профиля ещё работает. Закройте его перед удалением.')
                finally:
                    watcher.close()
            with self.repo.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute(ARCHIVE_SCHEMA)
                if db.execute('SELECT 1 FROM tgstat_profile_archives WHERE id=?', (profile_id,)).fetchone():
                    raise ValueError('Профиль уже удалён')
                if profile_id == DEFAULT_PROFILE:
                    record = (DEFAULT_PROFILE, DEFAULT_NAME, DEFAULT_NAME.casefold(), '')
                else:
                    row = db.execute('SELECT id,name,name_key,created_at FROM tgstat_profiles WHERE id=?', (profile_id,)).fetchone()
                    if row is None:
                        raise ValueError('Профиль не найден')
                    record = tuple(row)
                db.execute('INSERT INTO tgstat_profile_archives VALUES(?,?,?,?,?)', (*record, datetime.now(timezone.utc).isoformat()))
                db.execute('DELETE FROM tgstat_profiles WHERE id=?', (profile_id,))

    def restore(self, profile_id):
        from .runner import RunGuard
        validate_profile_id(profile_id)
        with RunGuard(self.repo.path):
            try:
                with self.repo.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    db.execute(ARCHIVE_SCHEMA)
                    row = db.execute('SELECT id,name,name_key,created_at FROM tgstat_profile_archives WHERE id=?', (profile_id,)).fetchone()
                    if row is None:
                        raise ValueError('Удалённый профиль не найден')
                    default_active = not db.execute('SELECT 1 FROM tgstat_profile_archives WHERE id=?', (DEFAULT_PROFILE,)).fetchone()
                    if db.execute('SELECT count(*) FROM tgstat_profiles').fetchone()[0] + int(default_active) >= 20:
                        raise ValueError('Достигнут лимит 20 активных профилей')
                    if profile_id != DEFAULT_PROFILE:
                        profile_directory(profile_id)
                        db.execute('INSERT INTO tgstat_profiles VALUES(?,?,?,?)', tuple(row))
                    db.execute('DELETE FROM tgstat_profile_archives WHERE id=?', (profile_id,))
            except sqlite3.IntegrityError:
                raise ValueError('Сначала переименуйте активный профиль с таким же названием') from None
        return profile_id

    def rename(self, profile_id, name):
        validate_profile_id(profile_id)
        if profile_id == DEFAULT_PROFILE:
            raise ValueError('Основной профиль имеет постоянное название')
        name = validate_name(name)
        try:
            with self.repo.connect() as db:
                cursor = db.execute('UPDATE tgstat_profiles SET name=?,name_key=? WHERE id=?', (name, name.casefold(), profile_id))
                if not cursor.rowcount:
                    raise ValueError('Профиль не найден')
        except sqlite3.IntegrityError:
            raise ValueError('Профиль с таким названием уже существует') from None


def open_for_login(profile_id, log=None, repo=None):
    """User-click action only: open one Chrome, perform no search or scraping."""
    if repo is None:
        return _open_for_login(profile_id, log)
    from .runner import RunGuard
    with RunGuard(repo.path):
        if profile_id not in {row['id'] for row in TGStatProfiles(repo).list()}:
            raise ValueError('Профиль удалён или отсутствует. Выберите активный профиль.')
        return _open_for_login(profile_id, log)


def _open_for_login(profile_id, log):
    from .search import TGStatSearch
    from .settings import Settings
    from .diagnostics import retain, logger
    search = TGStatSearch(Settings(tgstat_profile_id=profile_id, login_wait_seconds=0, keep_browser_on_error=True),
                          log=log or (lambda message: logger().info('TGStat profile: %s', message)))
    try:
        search._open()
        search.stage = 'manual_profile_login'
        search.purpose = 'profile_login'
        search.preserve_on_close = True
        retain(search)
        from selenium.common.exceptions import TimeoutException
        try:
            search.driver.get('https://tgstat.ru/channels/search')
        except TimeoutException:
            search.log('TGStat загружается дольше обычного. Chrome оставлен открытым для ручного входа.')
        search.check_login()
        return search.session_id
    except Exception:
        logger().exception('TGStat profile login failed profile=%s', profile_id)
        search.close(force=True)
        raise
