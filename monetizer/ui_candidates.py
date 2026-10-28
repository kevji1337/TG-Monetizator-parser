"""Compact candidate view and manually scheduled contact work."""
from datetime import date, timedelta
import re

import streamlit as st

from .candidate_insights import ai_priority, relevant_offers, OFFER_LABELS
from .contact_plans import ACTIONS
from .qualification import TYPE_LABELS
from .repository import WORK_STATUSES
from .preferences import PersistentUI, Preferences

RELEVANCE = {'matched': 'Соответствует теме', 'mismatch': 'Другая тема', 'unknown': 'Тема требует проверки'}


def candidate_card(row):
    st.subheader(row.get('title') or row['username'])
    st.caption(row['username'] + ' · ' + row.get('verdict', 'Нет оценки') +
               ' · рабочий статус: ' + WORK_STATUSES.get(row.get('work_status', 'new'), 'Не разобран'))
    left, middle, right = st.columns(3)
    left.metric('Подписчики', row.get('subs') if row.get('subs') is not None else 'Нет данных')
    middle.metric('Средние просмотры', row.get('avg_views') if row.get('avg_views') is not None else 'Нет данных')
    right.metric(f"Пост ≈{row.get('activity_target_age_hours', 24)} ч / подписчики",
                 f"{row['activity_ratio']:g}%" if isinstance(row.get('activity_ratio'), (int, float)) else 'Нет замера')
    if row.get('activity_post_url'):
        st.caption(f"Возраст выбранного поста: {row.get('activity_post_age_hours', '?')} ч. "
                   f"Просмотры: {row.get('activity_views', '?')}; подписчики: {row.get('activity_subscribers', '?')}. "
                   f"Порог: {row.get('norm', 10):g}%.")
        st.link_button('Открыть пост с замером', row['activity_post_url'])
    st.caption(TYPE_LABELS.get(row.get('channel_type'), 'Тип не установлен') + ' · ' +
               RELEVANCE.get(row.get('relevance'), 'Тема требует проверки'))
    review = row.get('api_ai_review') if not row.get('assessment_stale') else None
    if row.get('about_text'):
        st.text(row['about_text'][:350])
    st.write('**Основание отбора**')
    st.text(((review.get('reason') if review else row.get('reasons')) or 'Недостаточно данных')[:500])
    priority = ai_priority(row)
    st.caption(f"Приоритет в очереди AI: {priority['ai_priority_label'].lower()} · {priority['ai_priority_score']}/100. "
               'Это порядок проверки, не оценка вероятности покупки или сотрудничества.')
    with st.expander('Почему такой приоритет'):
        st.text(priority['ai_priority_reason'])
    st.write('**Что предлагает автор**')
    offers = relevant_offers(row)
    own_or_unknown = [offer for offer in offers if offer.get('ownership') != 'third_party']
    if not own_or_unknown:
        st.caption('Не найдено в просмотренной выборке. Это не доказывает отсутствие продукта.')
    else:
        st.caption('По AI-разбору с проверенными источниками; смысл и принадлежность проверьте.' if review else
                   'Предварительные находки локальных правил; принадлежность автору требует проверки.')
        for offer in own_or_unknown[:2]:
            label = OFFER_LABELS.get(offer.get('kind'), 'Предложение')
            ownership = 'заявлено как собственное' if offer.get('ownership') == 'self_reported' else 'принадлежность не установлена'
            prices = '; '.join(offer.get('prices', [])) or 'цена не найдена в выборке'
            st.write(label + ' · ' + ownership + ' · ' + prices)
            st.text(offer.get('quote', '')[:180])
            if offer.get('url'):
                st.caption('Источник: ' + offer['url'])
        if len(own_or_unknown) > 2:
            st.caption(f'Всего предложений: {len(own_or_unknown)}. Остальные доступны в подробном разборе.')
    contact = row.get('author_contact', '')
    st.write('**Контакт**')
    if re.fullmatch(r'@[A-Za-z][A-Za-z0-9_]{3,31}', contact):
        st.link_button('Открыть ' + contact, 'https://t.me/' + contact[1:])
        st.caption('Кандидат контакта из описания; личность и назначение требуют проверки.')
    else:
        st.caption('Личный контакт автора не установлен. Способы записи могут быть указаны в офферах.')
    st.caption('Наличие продюсера: не установлено. Последняя загрузка Telegram: ' + (row.get('audited_at') or 'нет данных'))


def contact_plan_editor(repo, row, running=False):
    username = row['username']
    revision_key = 'contact_revision_' + username
    revision = st.session_state.get(revision_key, 0)
    prefix = f'contact_{username}_{revision}'
    due = row.get('next_contact_date', '')
    with st.expander('Следующий контакт', expanded=bool(due)):
        if due:
            st.caption(f"{row.get('contact_plan_state', '')} · {due} · {row.get('next_action_label', '')}")
        with st.form(prefix):
            enabled = st.checkbox('Запланировать действие', value=bool(due), key=prefix + '_enabled')
            day = st.date_input('Дата', value=date.fromisoformat(due) if due else date.today(),
                                min_value=date(1900, 1, 1), max_value=date(2100, 12, 31), key=prefix + '_date')
            action = st.selectbox('Следующее действие', list(ACTIONS),
                                  index=list(ACTIONS).index(row.get('next_action')) if row.get('next_action') in ACTIONS else 0,
                                  format_func=ACTIONS.get, key=prefix + '_action')
            note = st.text_area('Что нужно сделать', value=row.get('contact_plan_note', ''), max_chars=2000, key=prefix + '_note')
            st.caption('Выключите «Запланировать действие» и сохраните, чтобы убрать текущий план. История сохранится.')
            save = st.form_submit_button('Сохранить план', disabled=running)
        if save:
            try:
                if enabled and day is None:
                    raise ValueError('Укажите дату следующего действия')
                repo.save_contact_plan(username, day.isoformat() if enabled else None, action, note)
                st.session_state[revision_key] = revision + 1
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
        if due and st.button('Действие выполнено', key=prefix + '_complete', disabled=running):
            try:
                repo.complete_contact_plan(username)
                st.session_state[revision_key] = revision + 1
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
        if row.get('last_action_at'):
            st.caption('Последнее выполненное действие (UTC): ' + row['last_action_at'])


def contact_plan_panel(runner, running):
    import pandas as pd

    rows = runner.repo.list_channels()
    ui = PersistentUI(Preferences(runner.repo))
    st.subheader('План контактов')
    st.caption('Даты и действия назначаются вручную. Сообщения отправляете вы; автоматической рассылки здесь нет.')
    hide_closed = ui.checkbox('Скрыть «Не подходит» и «Отказ» в плане контактов', value=True)
    active = [row for row in rows if not hide_closed or row.get('work_status') not in ('unsuitable', 'declined')]
    planned = [row for row in active if row.get('next_contact_date')]
    today = date.today()
    metrics = st.columns(3)
    metrics[0].metric('Просрочено', sum(row['next_contact_date'] < today.isoformat() for row in planned))
    metrics[1].metric('На сегодня', sum(row['next_contact_date'] == today.isoformat() for row in planned))
    metrics[2].metric('Всего назначено', len(planned))
    mode = ui.radio('Показать действия', ['Просрочено и сегодня', 'Ближайшие 7 дней', 'Все назначенные', 'Кандидаты без плана'], horizontal=True)
    if mode == 'Кандидаты без плана':
        selected = [row for row in active if not row.get('next_contact_date') and not row.get('assessment_stale')
                    and row.get('status') in ('top', 'recommended')]
    elif mode == 'Просрочено и сегодня':
        selected = [row for row in planned if row['next_contact_date'] <= today.isoformat()]
    elif mode == 'Ближайшие 7 дней':
        selected = [row for row in planned if row['next_contact_date'] <= (today + timedelta(days=7)).isoformat()]
    else:
        selected = planned
    selected = sorted(selected, key=lambda row: (row.get('next_contact_date') or '9999', row['username']))
    if not selected:
        st.info('В этом списке пока нет действий. Дату можно назначить в карточке канала в поиске или базе кандидатов.')
        return
    st.dataframe(pd.DataFrame([{'Срок': row.get('contact_plan_state'), 'Дата': row.get('next_contact_date'),
                                'Действие': row.get('next_action_label'), 'Канал': row['username'],
                                'Название': row.get('title'), 'Рабочий статус': row.get('work_status_label'),
                                'Пояснение': row.get('contact_plan_note')} for row in selected]), hide_index=True, width='stretch')
    username = st.selectbox('Канал для следующего действия', [row['username'] for row in selected], key='contact_plan_channel')
    row = next(row for row in selected if row['username'] == username)
    candidate_card(row)
    contact_plan_editor(runner.repo, row, running)
    if row.get('pitch'):
        st.text_area('Сохранённое сообщение для ручной отправки', row['pitch'], disabled=True)
