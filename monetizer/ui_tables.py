"""Content-sized tables with bounded widths, independent of CSV compatibility."""
import copy
import json
import math
import re
import unicodedata

import streamlit as st

from .export import COLUMNS
from .preferences import widget_key

WIDTH_LABEL = 'Лимит ширины текстовых столбцов, px'
WIDTH_KEY = widget_key('number_input', WIDTH_LABEL)
DEFAULT_MAX_WIDTH = 320


def text_pixels(value, ceiling=640):
    """Bounded glyph estimate for the grid font; no browser/server or font download."""
    longest = 0.0
    for line in str(value).splitlines() or ['']:
        size = 0.0
        for char in line:
            if unicodedata.combining(char) or char in ('\u200d', '\ufe0f'):
                continue
            if unicodedata.east_asian_width(char) in ('W', 'F') or ord(char) >= 0x1f000:
                size += 15
            elif char in ' ilI.,:;!|\'`':
                size += 4
            elif char in 'WMШЩЮЖшщюж@%':
                size += 10
            else:
                size += 7.5
            if size >= ceiling:
                return ceiling
        longest = max(longest, size)
    return math.ceil(longest)


def display_text(value, spec):
    import pandas as pd
    if value is None:
        return ''
    if not isinstance(value, (list, tuple, dict)):
        try:
            if pd.isna(value):
                return ''
        except (TypeError, ValueError):
            pass
    kind = spec.get('type_config') or {}
    if kind.get('type') == 'link' and kind.get('display_text'):
        pattern = kind['display_text']
        if '(' in pattern:
            try:
                match = re.search(pattern, str(value))
                return match.group(1) if match and match.lastindex else str(value)
            except re.error:
                return str(value)
        return pattern
    if kind.get('type') in ('number', 'progress') and kind.get('format'):
        try:
            return kind['format'] % value
        except (TypeError, ValueError, OverflowError):
            pass
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def table_layout(frame, column_config=None, column_order=None, max_width=DEFAULT_MAX_WIDTH):
    """Size displayed cells/labels, and avoid stretch distributing extra width."""
    import pandas as pd
    if type(max_width) is not int or not 120 <= max_width <= 600:
        raise ValueError('Максимальная ширина столбца: 120–600 px')
    config = copy.deepcopy(column_config or {})
    visible = list(frame.columns) if column_order is None else list(column_order)
    visible = [name for name in visible if name in frame.columns and config.get(name, {}) is not None]
    for name in frame.columns:
        original = config.get(name, {})
        if original is None:
            continue
        spec = {'label': original} if isinstance(original, str) else dict(original)
        kind = (spec.get('type_config') or {}).get('type')
        label = spec.get('label') or str(name)
        numeric = kind in ('number', 'progress') or pd.api.types.is_numeric_dtype(frame[name])
        cap = min(max_width, 128 if label == 'Приоритет' else 160 if numeric else 140 if kind == 'link' else max_width)
        minimum = min(cap, 64 if numeric else 80)
        measured = text_pixels(label, cap) + 34  # Header text plus sort/menu affordances.
        for value in frame[name]:
            measured = max(measured, text_pixels(display_text(value, spec), cap) + 28)
            if measured >= cap:
                break
        spec['width'] = max(minimum, min(cap, measured))
        config[name] = spec
    # A stretched frame distributes free space among columns, defeating the caps.
    width = sum(config[name]['width'] for name in visible) if visible else 240
    return config, width


def data_table(data, *, column_config=None, column_order=None, **kwargs):
    import pandas as pd
    frame = data if isinstance(data, pd.DataFrame) else pd.DataFrame(data)
    maximum = st.session_state.get(WIDTH_KEY, DEFAULT_MAX_WIDTH)
    if type(maximum) is not int or not 120 <= maximum <= 600:
        maximum = DEFAULT_MAX_WIDTH
    config, width = table_layout(frame, column_config, column_order, maximum)
    kwargs.pop('width', None)
    kwargs.pop('use_container_width', None)
    kwargs.setdefault('hide_index', True)
    kwargs.setdefault('row_height', 38)
    if not kwargs['hide_index']:
        width += min(maximum, max(64, max((text_pixels(i, maximum) for i in frame.index), default=0) + 28))
    return st.dataframe(frame, column_config=config, column_order=column_order, width=width, **kwargs)

COMPACT_COLUMNS = ['verdict', 'title', 'username', 'subs', 'activity_ratio',
                   'ai_audit_status', 'ai_priority_score', 'author_contact', 'link']


def candidate_columns(target_age=24):
    columns = {
        'verdict': st.column_config.TextColumn('Отбор'),
        'title': st.column_config.TextColumn('Название'),
        'username': st.column_config.TextColumn('Канал', pinned=True),
        'subs': st.column_config.NumberColumn('Подписчики', format='%d'),
        'activity_ratio': st.column_config.NumberColumn(f'Пост ≈{target_age} ч, %', format='%.1f%%',
                         help='Просмотры выбранного поста / подписчики × 100. Точный возраст и целевой возраст — в карточке.'),
        'ai_audit_status': st.column_config.TextColumn('AI-аудит'),
        'ai_priority_score': st.column_config.NumberColumn('Приоритет', format='%d',
                            help='Порядок проверки AI: 0–100. Это не оценка качества канала.'),
        'channel_type': st.column_config.TextColumn('Тип'),
        'activity_post_age_hours': st.column_config.NumberColumn('Возраст, ч', format='%.1f'),
        'avg_views': st.column_config.NumberColumn('Ср. просмотры', format='%.0f'),
        'median_views': st.column_config.NumberColumn('Медиана', format='%.0f'),
        'view_ratio': st.column_config.NumberColumn('Средние, %', format='%.1f%%'),
        'freq': st.column_config.TextColumn('Частота'),
        'last_post': st.column_config.TextColumn('Последний пост, UTC'),
        'reasons': st.column_config.TextColumn('Основания'),
        'author_contact': st.column_config.TextColumn('Контакт'),
        'next_contact_date': st.column_config.TextColumn('След. контакт'),
        'next_action_label': st.column_config.TextColumn('Действие'),
        'custom_tags': st.column_config.TextColumn('Теги'),
        'custom_collections': st.column_config.TextColumn('Подборки'),
        'deal_stage': st.column_config.TextColumn('Партнёрство'),
    }
    for key, label in [('link', 'Канал ↗'), ('youtube', 'YouTube ↗'), ('instagram', 'Instagram ↗'),
                       ('activity_post_url', 'Пост ↗')]:
        columns[key] = st.column_config.LinkColumn(label, display_text='Открыть ↗')
    return {COLUMNS[key]: value for key, value in columns.items()}


def workspace_columns():
    return {
        'Канал': st.column_config.TextColumn('Канал', pinned=True),
        'Название': st.column_config.TextColumn('Название'),
        'Приоритет': st.column_config.NumberColumn('Приоритет', format='%d', help='Очередь AI, 0–100'),
        'Отбор': st.column_config.TextColumn('Отбор'),
        'Вердикт': st.column_config.TextColumn('Отбор'),
        'Тип': st.column_config.TextColumn('Тип'),
        'Работа': st.column_config.TextColumn('Работа'),
        'Следующий контакт': st.column_config.TextColumn('След. контакт'),
        'Действие': st.column_config.TextColumn('Действие'),
        'Заметка': st.column_config.TextColumn('Заметка'),
        'Почему': st.column_config.TextColumn('Основание'),
    }


def search_locked(state):
    return state.get('status') in ('running', 'stopping') or bool(state.get('external_owner')) or bool(state.get('experiment_active'))
