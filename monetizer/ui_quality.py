"""Human evaluation controls and an automatically saved operator profile."""
import json
import sqlite3
import streamlit as st

from .ui_tables import data_table

from .profile import load_profile, save_profile, FIELDS
from .evaluation import review_set, validate_labels, save_labels, evaluate
from .qualification import TYPE_LABELS


def profile_panel(repo, disabled=False):
    profile = load_profile(repo)
    def save_field(field, widget_key):
        try:
            current = load_profile(repo)
            current[field] = st.session_state[widget_key]
            save_profile(repo, current)
        except (ValueError, OSError, sqlite3.Error):
            st.warning('Не удалось автоматически сохранить поле профиля.')
    with st.sidebar.expander('Профиль отправителя'):
        st.caption('Профиль сохраняется автоматически после выхода из поля. Используются только введённые вами факты.')
        fields = {}
        for key, label in [('name', 'Имя отправителя'), ('role', 'Ваша роль'), ('services', 'Какие задачи берёте'),
                           ('verified_facts', 'Подтверждённый опыт и кейсы'), ('terms', 'Условия партнёрства, если определены')]:
            widget_key = 'sender_profile_' + key
            options = dict(key=widget_key, on_change=save_field, args=(key, widget_key), max_chars=FIELDS[key], disabled=disabled)
            fields[key] = st.text_area(label, profile[key], height=70, **options) if key not in ('name', 'role') else st.text_input(label, profile[key], **options)
        if st.button('Сохранить профиль отправителя', disabled=disabled):
            try:
                profile = save_profile(repo, fields)
                st.success('Профиль сохранён.')
            except ValueError as exc:
                st.error(str(exc))
    return profile


def quality_panel(repo, running):
    import pandas as pd
    st.caption('Разметка качества отделена от рабочих статусов и оценок AI. Для оценки рекомендуются 50–100 реальных каналов из разных запросов.')
    packet = review_set(repo)
    report = evaluate(repo)
    if not report['labelled']:
        st.info(f"Ручных оценок пока нет. Доступно каналов для разметки: {len(packet['channels'])}. Precision/recall ещё не рассчитаны.")
    else:
        st.json({k: v for k, v in report.items() if k != 'errors'})
        if report['errors']:
            data_table(pd.DataFrame(report['errors']), hide_index=True, width='stretch')
    if packet['channels']:
        channels = {c['channel_id']: c for c in packet['channels']}
        chosen = st.selectbox('Канал для ручной оценки', list(channels))
        channel = channels[chosen]
        st.write(channel['title'])
        st.json({'Направление': channel['analysis_context'], 'Метрики': channel['metrics'],
                 'Отбор по метрикам': channel['metric_status']})
        with st.expander('Исходные тексты для ручной оценки'):
            if not channel['sources']:
                st.info('Текстов нет. Оцените как unknown или явно обновите данные в карточке канала.')
            for source in channel['sources']:
                st.caption(source.get('date', '') + (' · Пересылка' if source.get('forwarded') else ' · Авторство требует оценки'))
                st.text(source['text'])
                if source.get('url'):
                    st.link_button('Источник ' + source['id'], source['url'])
        label = channel['label']
        with st.form('human_label_' + chosen + channel['content_hash'][:12]):
            kinds = list(TYPE_LABELS)
            kind = st.selectbox('Тип канала по вашей оценке', kinds,
                                index=kinds.index(label['channel_type'] or 'unknown'), format_func=TYPE_LABELS.get)
            topics = {'unknown': 'Недостаточно данных', 'matched': 'Соответствует', 'mismatch': 'Не соответствует'}
            relevance = st.selectbox('Соответствие теме по вашей оценке', list(topics),
                                     index=list(topics).index(label['relevance'] or 'unknown'), format_func=topics.get)
            options = [None, False, True]
            qualified = st.selectbox('Проходит предварительный отбор по вашей оценке', options,
                                     index=options.index(label['qualified']),
                                     format_func=lambda v: 'Не оценено' if v is None else 'Да' if v else 'Нет')
            annotator = st.text_input('Кто выполнил ручную оценку', label['annotator'])
            note = st.text_area('Основание ручной оценки', label['note'])
            if st.form_submit_button('Сохранить оценку канала', disabled=running):
                try:
                    updated = {**channel, 'label': {'channel_type': kind, 'relevance': relevance,
                                                    'qualified': qualified, 'annotator': annotator, 'note': note}}
                    save_labels(repo, {**packet, 'channels': [updated]})
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
    st.download_button('Скачать каналы для ручной разметки', json.dumps(packet, ensure_ascii=False, indent=2),
                       'human_review.json', mime='application/json')
    st.download_button('Скачать отчёт качества', json.dumps(report, ensure_ascii=False, indent=2),
                       'quality_report.json', mime='application/json')
    st.caption('В JSON заполните label: channel_type, relevance, qualified (true/false), annotator и note. null оставляет канал без оценки. '
               'Оценивайте тексты самостоятельно; для unknown обычно qualified=false. Данные источников и хеши не меняйте.')
    upload = st.file_uploader('Ручная разметка в JSON', type=['json'], key='human_labels')
    if upload:
        try:
            labels = validate_labels(repo, upload.getvalue())
            st.write(f'Проверено оценок: {len(labels)}. Импорт заменит прежние ручные метки этих же снимков.')
            if st.button('Сохранить ручную разметку', disabled=running or not labels):
                save_labels(repo, upload.getvalue())
                st.success('Разметка сохранена')
                st.rerun()
        except (ValueError, TypeError, KeyError) as exc:
            st.error(f'Разметка отклонена без записи: {exc}')
