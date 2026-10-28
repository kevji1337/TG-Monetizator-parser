"""Local UI preferences and a Windows user-bound encrypted API credential."""
import base64
import ctypes
import hashlib
import inspect
import json
import math
import os
import sqlite3
from ctypes import wintypes

import streamlit as st

PREFERENCES_KEY = 'ui_preferences_v1'
SECRET_KEY = 'proxy_api_key_dpapi_v1'


def protect_secret(value, decrypt=False):
    """DPAPI without LOCAL_MACHINE: bound to the current Windows user."""
    if os.name != 'nt':
        raise OSError('Сохранение ключа доступно только в Windows; используйте MONETIZER_AI_KEY')
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    raw = base64.b64decode(value, validate=True) if decrypt else value.encode('utf-8')
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    # The optional description/entropy/prompt fields are NULL for both operations.
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise OSError('Windows не смогла расшифровать ключ' if decrypt else 'Windows не смогла зашифровать ключ')
    try:
        result = ctypes.string_at(output.data, output.size)
        return result.decode('utf-8') if decrypt else base64.b64encode(result).decode('ascii')
    finally:
        kernel32.LocalFree(ctypes.cast(output.data, ctypes.c_void_p))


def widget_key(kind, label):
    return 'pref_' + hashlib.sha256((kind + ':' + label).encode()).hexdigest()[:16]


class Preferences:
    def __init__(self, repo, initial=None):
        self.repo = repo
        with repo.connect() as db:
            record = db.execute('SELECT value FROM meta WHERE key=?', (PREFERENCES_KEY,)).fetchone()
        try:
            self.values = json.loads(record['value']) if record else dict(initial or {})
            if not isinstance(self.values, dict):
                self.values = dict(initial or {})
        except (ValueError, TypeError):
            self.values = dict(initial or {})

    def save(self, key, value):
        # Never accept the secret widget in the plain-text preferences document.
        if key == 'proxy_api_key':
            return
        value = json.loads(json.dumps(value, ensure_ascii=False))
        if key in self.values and self.values[key] == value:
            return
        with self.repo.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT value FROM meta WHERE key=?', (PREFERENCES_KEY,)).fetchone()
            try:
                current = json.loads(row['value']) if row else dict(self.values)
                if not isinstance(current, dict):
                    current = {}
            except (ValueError, TypeError):
                current = {}
            current[key] = value
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                       (PREFERENCES_KEY, json.dumps(current, ensure_ascii=False)))
        self.values.update(current)

    def load_key(self):
        with self.repo.connect() as db:
            row = db.execute('SELECT value FROM meta WHERE key=?', (SECRET_KEY,)).fetchone()
        return protect_secret(row['value'], decrypt=True) if row else ''

    def save_key(self, value):
        encrypted = protect_secret(value) if value else None
        with self.repo.connect() as db:
            if encrypted:
                db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (SECRET_KEY, encrypted))
            else:
                db.execute('DELETE FROM meta WHERE key=?', (SECRET_KEY,))


def last_run_preferences(state):
    """Use actual previous run values once, without triggering a search/audit."""
    settings = state.get('settings', {})
    if not settings:
        return {}
    fields = {
        'ai_enabled': ('checkbox', 'Анализировать посты через Gemini'),
        'ai_base_url': ('text_input', 'Base URL AI'), 'ai_model': ('text_input', 'Model ID'),
        'ai_max_audits': ('number_input', 'Максимум запросов AI за запуск'),
        'ai_timeout': ('number_input', 'Таймаут AI, секунд'),
        'ai_prompt_bytes': ('number_input', 'Лимит текста AI-запроса, байт UTF-8'),
        'ai_screen_model': ('text_input', 'Модель первичного разбора'),
        'ai_deep_model': ('text_input', 'Модель глубокого разбора'),
        'ai_analysis_stage': ('selectbox', 'Этап AI-разбора'),
        'ai_prompt_variant': ('selectbox', 'Вариант промпта'),
        'posts_limit': ('number_input', 'Публикаций для аудита'),
        'dev_mode': ('checkbox', 'Dev-режим'), 'keep_browser_on_error': ('checkbox', 'Оставлять Chrome открытым при ошибке'),
        'browser_wait_seconds': ('number_input', 'Ожидание ручного входа, секунд'),
        'tgstat_profile_id': ('selectbox', 'Профиль TGStat'),
        'tgstat_auto_switch': ('checkbox', 'Автоматически пропускать ограниченные профили'),
        'country_ids': ('multiselect', 'Страна канала'), 'language_ids': ('multiselect', 'Язык канала'),
        'channel_type': ('selectbox', 'Тип канала'), 'micro_id': ('selectbox', 'Проверять конкретную специализацию'),
        'target_count': ('number_input', 'Цель: подходящих каналов'), 'new_only': ('checkbox', 'Искать только новые каналы'),
        'min_er': ('slider', 'Уровень вовлечённости ER от, %'), 'min_male': ('slider', 'Мужская аудитория от, %'),
        'min_female': ('slider', 'Женская аудитория от, %'), 'min_subs': ('number_input', 'Подписчики от'),
        'max_subs': ('number_input', 'Подписчики до'),
        'tgstat_min_avg_reach': ('number_input', 'Средний охват поста TGStat от'),
        'tgstat_max_avg_reach': ('number_input', 'Средний охват поста TGStat до'),
        'min_daily_reach': ('number_input', 'Средний охват поста за 24 часа от'),
        'max_daily_reach': ('number_input', 'Средний охват поста за 24 часа до'),
        'min_ci': ('number_input', 'Индекс цитирования от'), 'max_ci': ('number_input', 'Индекс цитирования до'),
        'exclude_dead': ('checkbox', 'Скрыть мёртвые каналы'), 'exclude_scam': ('checkbox', 'Без SCAM/FAKE'),
        'exclude_red_label': ('checkbox', 'Без красной метки'),
        'min_avg_view_ratio': ('number_input', 'Средние просмотры от подписчиков, %'),
        'target_post_age_hours': ('number_input', 'Возраст поста для оценки активности, часов'),
        'post_age_tolerance_hours': ('number_input', 'Допустимое отклонение возраста, часов'),
        'min_post_view_ratio': ('number_input', 'Просмотры поста от подписчиков, %'),
        'max_gap_days': ('number_input', 'Максимальный интервал публикаций, дней'),
        'workers': ('slider', 'Параллельных аудитов'), 'requests_per_second': ('slider', 'Запросов к Telegram в секунду'),
        'max_audits': ('number_input', 'Максимум аудитов: 0 = десять целей'), 'max_minutes': ('number_input', 'Лимит времени, минут'),
    }
    result = {widget_key(*pair): settings[field] for field, pair in fields.items() if field in settings}
    result.update({'category_weight_' + key: weight for key, weight in settings.get('category_weights', ())})
    result['tgstat_category_ids'] = list(settings.get('category_ids', []))
    result[widget_key('slider', 'Возраст канала, месяцев')] = [settings.get('min_age_months', 12), settings.get('max_age_months') or 120]
    result[widget_key('slider', 'Уровень вовлечённости ERR, %')] = [settings.get('min_err', 10), settings.get('max_err') or 100]
    result[widget_key('checkbox', 'Дать минуту на ручной вход в TGStat')] = bool(settings.get('login_wait_seconds'))
    result[widget_key('radio', 'Источник')] = 'Список каналов — только аудит' if state.get('manual') else 'TGStat — каталог тематик'
    if state.get('manual'):
        result[widget_key('text_area', '@username или ссылки t.me — по одному на строку')] = '\n'.join('@' + name for name in state.get('candidates', {}))
    return result


class PersistentUI:
    KINDS = {'checkbox', 'text_input', 'text_area', 'number_input', 'slider', 'selectbox', 'multiselect', 'radio'}

    def __init__(self, preferences, target=None, disabled=False):
        self.preferences, self.target = preferences, st if target is None else target
        self.disabled = disabled

    def on(self, target):
        return PersistentUI(self.preferences, target, disabled=self.disabled)

    def __getattr__(self, kind):
        if kind not in self.KINDS:
            raise AttributeError(kind)
        return lambda label, *args, **kwargs: self._render(kind, label, *args, **kwargs)

    @staticmethod
    def _normalize(kind, value, args):
        if kind in ('selectbox', 'radio', 'multiselect'):
            options = list(args['options'])
            if kind == 'multiselect':
                if not isinstance(value, (list, tuple)):
                    raise ValueError()
                return [v for v in value if v in options]
            if value not in options:
                raise ValueError()
            return value
        if kind == 'checkbox':
            if type(value) is not bool:
                raise ValueError()
            return value
        if kind in ('text_input', 'text_area'):
            if not isinstance(value, str):
                raise ValueError()
            return value
        default = args.get('value', 'min' if kind == 'number_input' else None)
        if value is None and default is None and kind == 'number_input':
            return None
        ranged = kind == 'slider' and isinstance(default, (list, tuple))
        if ranged and (not isinstance(value, (list, tuple)) or len(value) != len(default)):
            raise ValueError()
        values = list(value) if ranged else [value]
        minimum, maximum = args.get('min_value'), args.get('max_value')
        exemplar = default[0] if ranged else default
        use_float = any(isinstance(v, float) for v in (exemplar, minimum, maximum, args.get('step')))
        normalized = []
        for item in values:
            if type(item) not in (int, float) or not math.isfinite(item):
                raise ValueError()
            if not use_float and int(item) != item:
                raise ValueError()
            if (minimum is not None and item < minimum) or (maximum is not None and item > maximum):
                raise ValueError()
            normalized.append(float(item) if use_float else int(item))
        if ranged and normalized != sorted(normalized):
            raise ValueError()
        return tuple(normalized) if ranged else normalized[0]

    def _changed(self, key):
        try:
            self.preferences.save(key, st.session_state[key])
        except (OSError, ValueError, sqlite3.Error):
            st.warning('Не удалось сохранить настройки панели; текущее значение осталось в этой сессии.')

    def _render(self, kind, label, *args, **kwargs):
        method = getattr(self.target, kind)
        call = dict(inspect.signature(method).bind(label, *args, **kwargs).arguments)
        call['disabled'] = self.disabled or call.get('disabled', False)
        key = call.setdefault('key', widget_key(kind, label))
        if key == 'proxy_api_key':
            raise ValueError('Для API-ключа используйте отдельное защищённое поле')
        available = key in st.session_state or key in self.preferences.values
        if available:
            try:
                stored = st.session_state[key] if key in st.session_state else self.preferences.values[key]
                current = self._normalize(kind, stored, call)
                if kind in ('selectbox', 'radio'):
                    call['index'] = list(call['options']).index(current)
                elif kind == 'multiselect':
                    call['default'] = current
                else:
                    call['value'] = current
                if key in st.session_state and current != stored:
                    st.session_state[key] = current
            except (ValueError, TypeError, KeyError, OverflowError):
                if key in st.session_state:
                    del st.session_state[key]
        call.update(on_change=self._changed, args=(key,))
        value = method(**call)
        self._changed(key)
        return value


def api_key_field(preferences, disabled=False):
    key = 'proxy_api_key'
    if key not in st.session_state:
        try:
            st.session_state[key] = preferences.load_key()
        except (OSError, ValueError, UnicodeError, sqlite3.Error):
            st.session_state[key] = ''
            st.warning('Сохранённый API-ключ недоступен этому пользователю Windows. Введите ключ заново.')
    def changed():
        try:
            preferences.save_key(st.session_state.get(key, '').strip())
            st.session_state['_saved_key_fingerprint'] = hashlib.sha256(st.session_state.get(key, '').encode()).hexdigest()
        except (OSError, ValueError, sqlite3.Error):
            st.warning('Ключ действует в текущей сессии, но зашифрованное сохранение не удалось.')
    result = st.text_input('API-ключ прокси', type='password', key=key, on_change=changed, disabled=disabled,
                           help='Автоматически сохраняется через Windows DPAPI. Очистите поле, чтобы удалить сохранённый ключ.')
    # Capture an existing session's key once when upgrading from the previous UI.
    fingerprint = hashlib.sha256(result.encode()).hexdigest()
    if st.session_state.get('_saved_key_fingerprint') != fingerprint:
        changed()
        st.session_state['_saved_key_fingerprint'] = fingerprint
    return result
