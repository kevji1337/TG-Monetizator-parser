"""Explicit table widths and display labels, independent of CSV compatibility."""
import streamlit as st

from .export import COLUMNS

COMPACT_COLUMNS = ['verdict', 'title', 'username', 'subs', 'activity_ratio',
                   'ai_audit_status', 'ai_priority_score', 'author_contact', 'link']


def candidate_columns(target_age=24):
    columns = {
        'verdict': st.column_config.TextColumn('Отбор', width=185),
        'title': st.column_config.TextColumn('Название', width=210),
        'username': st.column_config.TextColumn('Канал', width=140, pinned=True),
        'subs': st.column_config.NumberColumn('Подписчики', width=110, format='%d'),
        'activity_ratio': st.column_config.NumberColumn(f'Пост ≈{target_age} ч, %', width=130, format='%.1f%%',
                         help='Просмотры выбранного поста / подписчики × 100. Точный возраст и целевой возраст — в карточке.'),
        'ai_audit_status': st.column_config.TextColumn('AI-аудит', width=165),
        'ai_priority_score': st.column_config.NumberColumn('Приоритет', width=90, format='%d',
                            help='Порядок проверки AI: 0–100. Это не оценка качества канала.'),
        'channel_type': st.column_config.TextColumn('Тип', width=155),
        'activity_post_age_hours': st.column_config.NumberColumn('Возраст, ч', width=110, format='%.1f'),
        'avg_views': st.column_config.NumberColumn('Ср. просмотры', width=135, format='%.0f'),
        'median_views': st.column_config.NumberColumn('Медиана', width=100, format='%.0f'),
        'view_ratio': st.column_config.NumberColumn('Средние, %', width=115, format='%.1f%%'),
        'freq': st.column_config.TextColumn('Частота', width=140),
        'last_post': st.column_config.TextColumn('Последний пост, UTC', width=180),
        'reasons': st.column_config.TextColumn('Основания', width=320),
        'author_contact': st.column_config.TextColumn('Контакт', width=145),
        'next_contact_date': st.column_config.TextColumn('След. контакт', width=125),
        'next_action_label': st.column_config.TextColumn('Действие', width=190),
        'custom_tags': st.column_config.TextColumn('Теги', width=170),
        'custom_collections': st.column_config.TextColumn('Подборки', width=170),
        'deal_stage': st.column_config.TextColumn('Партнёрство', width=150),
    }
    for key, label in [('link', 'Канал ↗'), ('youtube', 'YouTube ↗'), ('instagram', 'Instagram ↗'),
                       ('activity_post_url', 'Пост ↗')]:
        columns[key] = st.column_config.LinkColumn(label, width=105, display_text='Открыть ↗')
    return {COLUMNS[key]: value for key, value in columns.items()}


def workspace_columns():
    return {
        'Канал': st.column_config.TextColumn('Канал', width=145, pinned=True),
        'Название': st.column_config.TextColumn('Название', width=220),
        'Приоритет': st.column_config.NumberColumn('Приоритет', width=90, format='%d', help='Очередь AI, 0–100'),
        'Отбор': st.column_config.TextColumn('Отбор', width=185),
        'Вердикт': st.column_config.TextColumn('Отбор', width=185),
        'Тип': st.column_config.TextColumn('Тип', width=150),
        'Работа': st.column_config.TextColumn('Работа', width=130),
        'Следующий контакт': st.column_config.TextColumn('След. контакт', width=125),
        'Действие': st.column_config.TextColumn('Действие', width=200),
        'Заметка': st.column_config.TextColumn('Заметка', width=280),
        'Почему': st.column_config.TextColumn('Основание', width=420),
    }


def search_locked(state):
    return state.get('status') in ('running', 'stopping') or bool(state.get('external_owner')) or bool(state.get('experiment_active'))
