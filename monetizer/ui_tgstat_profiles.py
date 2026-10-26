"""Local profile controls. Chrome opens only after an explicit button click."""
import sqlite3

import streamlit as st

from .tgstat_profiles import TGStatProfiles, DEFAULT_PROFILE, open_for_login
from .diagnostics import retained_sessions, close_retained
from .preferences import widget_key


def profile_picker(runner, ui):
    store = TGStatProfiles(runner.repo)
    profiles = store.list()
    labels = {item['id']: item['name'] for item in profiles}
    state = runner.snapshot()
    busy = state['status'] in ('running', 'stopping') or state.get('external_owner', False)
    selected_key = widget_key('selectbox', 'Профиль TGStat')
    pending = st.session_state.pop('_new_tgstat_profile', None)
    if pending in labels:
        st.session_state[selected_key] = pending
        ui.preferences.save(selected_key, pending)
    selected = ui.selectbox('Профиль TGStat', list(labels), format_func=labels.get, disabled=busy,
                            help='Отдельная сессия Chrome. Выберите профиль перед новым поиском; вход выполняется вручную.')
    with st.expander('Управление профилями TGStat', expanded=False):
        st.caption('Названия — локальные ярлыки. В каждый аккаунт входите в отдельном окне Chrome; '
                   'пароли и коды в эту панель вводить не нужно. Сессии сохраняются на этом компьютере.')
        with st.form('add_tgstat_profile'):
            name = st.text_input('Название нового профиля', max_chars=80, placeholder='Например: рабочий аккаунт')
            add = st.form_submit_button('Добавить профиль', disabled=busy)
        if add:
            try:
                st.session_state['_new_tgstat_profile'] = store.add(name)
                st.rerun()
            except (ValueError, OSError, sqlite3.Error) as exc:
                st.error(str(exc) if isinstance(exc, ValueError) else 'Не удалось сохранить профиль на диске')
        if selected != DEFAULT_PROFILE:
            with st.form('rename_tgstat_profile_' + selected):
                renamed = st.text_input('Название выбранного профиля', value=labels[selected], max_chars=80)
                rename = st.form_submit_button('Переименовать профиль', disabled=busy)
            if rename:
                try:
                    store.rename(selected, renamed)
                    st.rerun()
                except (ValueError, OSError, sqlite3.Error) as exc:
                    st.error(str(exc) if isinstance(exc, ValueError) else 'Не удалось сохранить название профиля')
        sessions = retained_sessions(include_url=False)
        if st.button('Открыть выбранный профиль для входа', disabled=busy or bool(sessions)):
            try:
                with st.spinner('Открываю Chrome для ручного входа в TGStat…'):
                    open_for_login(selected)
                st.rerun()
            except Exception:
                st.error('Не удалось открыть профиль. Проверьте, закрыт ли его Chrome; подробности в Dev-диагностике.')
        if sessions:
            st.info('Chrome уже открыт. Завершите вход, затем закройте его кнопкой ниже перед поиском. '
                    'Если окно закрыли вручную, нажмите эту кнопку для освобождения сессии.')
            for session in sessions:
                label = labels.get(session.get('profile_id'), 'Профиль из предыдущего запуска')
                if st.button('Закрыть Chrome и сохранить сессию: ' + label, key='close_profile_' + session['session_id'], disabled=busy):
                    close_retained(session['session_id'])
                    st.rerun()
        st.caption('Открытие страницы входа не подтверждает авторизацию. Войдите в TGStat в Chrome и проверьте имя аккаунта на сайте. '
                   'Новый поиск использует выбранный профиль. Для продолжения старого запуска нужен его исходный профиль.')
    return selected
