"""Reusable presets, table views, and keyboard selection controls."""
import streamlit as st

from .preferences import widget_key, last_run_preferences
from .settings import Settings
from .workspace import Workspace
from datetime import date


def apply_pending(preferences, busy):
    payload = st.session_state.pop('_workspace_apply', None)
    values = payload.get('values', payload) if payload else None
    if values and (not busy or payload.get('scope') == 'view'):
        for key, value in values.items():
            if key != 'proxy_api_key':
                st.session_state[key] = value
                preferences.save(key, value)


def queue_apply(values, scope='search'):
    st.session_state['_workspace_apply'] = {'values': values, 'scope': scope}


def preset_panel(repo, preferences, busy):
    store = Workspace(repo)
    with st.sidebar.expander('Пресеты поиска', expanded=False):
        presets = store.list('preset')
        chosen = st.selectbox('Сохранённый пресет', [''] + list(presets), format_func=lambda v: v or 'Выберите пресет', key='preset_selected', disabled=busy)
        if chosen:
            st.button('Применить пресет', disabled=busy, on_click=queue_apply, args=(presets[chosen],))
            if st.button('Удалить пресет', disabled=busy):
                store.delete('preset', chosen)
                st.rerun()
        name = st.text_input('Название пресета', key='preset_name', max_chars=120, disabled=busy)
        if st.button('Сохранить текущие настройки как пресет', disabled=busy or not name.strip()):
            keys = set(last_run_preferences({'settings': Settings().to_dict()}))
            keys.add(widget_key('text_area', '@username или ссылки t.me — по одному на строку'))
            keys |= {key for key in preferences.values if key.startswith('category_weight_')}
            store.save('preset', name.strip(), {key: value for key, value in preferences.values.items() if key in keys})
            st.rerun()
        st.caption('API-ключ и рабочие статусы в пресет не входят.')


def reminder_badge(repo, preferences):
    with repo.connect() as db:
        count = db.execute('SELECT count(*) FROM contact_plans WHERE due_date<=?', (date.today().isoformat(),)).fetchone()[0]
    if count:
        def open_contacts():
            key = widget_key('radio', 'Раздел')
            st.session_state[key] = 'План контактов'
            preferences.save(key, 'План контактов')
        st.sidebar.button(f'Контакты на сегодня / просрочено: {count}', on_click=open_contacts, width='stretch')


def table_options(repo, ui, visible, defaults, mode, columns):
    with st.expander('Столбцы и сохранённые виды'):
        store = Workspace(repo)
        views = store.list('view')
        chosen = st.selectbox('Сохранённый вид', [''] + list(views), format_func=lambda v: v or 'Выберите вид', key='view_selected')
        if chosen:
            if st.button('Применить вид'):
                queue_apply(views[chosen], scope='view')
                st.rerun()
            if st.button('Удалить вид'):
                store.delete('view', chosen)
                st.rerun()
        selected = ui.multiselect('Видимые столбцы', visible, default=defaults, key='table_columns_' + mode,
                                  format_func=lambda v: columns[v])
        name = st.text_input('Название вида', key='view_name', max_chars=120)
        if st.button('Сохранить вид таблицы', disabled=not name.strip()):
            keys = [widget_key('radio', 'Вид таблицы'), 'table_columns_' + mode]
            keys.append(widget_key('number_input', 'Лимит ширины текстовых столбцов, px'))
            keys += [widget_key(kind, label) for kind, label in [('checkbox', 'Только зелёные'), ('checkbox', 'С кандидатом контакта'),
                     ('checkbox', 'Карточка рядом с таблицей'),
                     ('text_input', 'Поиск в результатах'), ('multiselect', 'Отбор по главной причине'),
                     ('multiselect', 'Теги в результатах'), ('multiselect', 'Подборки в результатах'),
                     ('checkbox', 'Скрыть новости, агрегаторы, школы, бренды, магазины и доски вакансий')]]
            store.save('view', name.strip(), {key: st.session_state[key] for key in keys if key in st.session_state})
            st.rerun()
    return list(dict.fromkeys(['username'] + selected))


def move_candidate(usernames, amount):
    key = 'channel_for_draft'
    current = st.session_state.get(key)
    index = usernames.index(current) if current in usernames else 0
    st.session_state[key] = usernames[(index + amount) % len(usernames)]


def candidate_selector(rows):
    usernames = [r['username'] for r in rows]
    if st.session_state.get('channel_for_draft') not in usernames:
        st.session_state['channel_for_draft'] = usernames[0]
    selected = st.selectbox('Канал для черновика', usernames, key='channel_for_draft')
    left, right = st.columns(2)
    left.button('← Предыдущий', on_click=move_candidate, args=(usernames, -1), shortcut='Alt+ArrowLeft', width='stretch')
    right.button('Следующий →', on_click=move_candidate, args=(usernames, 1), shortcut='Alt+ArrowRight', width='stretch')
    st.caption('Alt+← / Alt+→ — каналы · Alt+1 — поиск · Alt+S — сохранить текст · Alt+V — вид таблицы')
    return selected


def toggle_table_view():
    key = widget_key('radio', 'Вид таблицы')
    st.session_state[key] = 'Подробный' if st.session_state.get(key, 'Компактный') == 'Компактный' else 'Компактный'
