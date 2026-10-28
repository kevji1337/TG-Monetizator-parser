"""Compact sidebar picker and a full-width TGStat profile workspace."""
import sqlite3

import streamlit as st

from .tgstat_profiles import TGStatProfiles, DEFAULT_PROFILE, open_for_login, session_state, AUTH_LABELS
from .diagnostics import retained_sessions, close_retained, check_retained_login, logger
from .preferences import widget_key


def _navigate(section, profile_id=None):
    st.session_state[widget_key('radio', 'Раздел')] = section
    if profile_id is not None:
        st.session_state['_tgstat_manager_pending'] = profile_id
        if section == 'Поиск':
            st.session_state['_new_tgstat_profile'] = profile_id
            st.session_state[widget_key('radio', 'Источник')] = 'TGStat — каталог тематик'


def _flash(message):
    st.session_state['_tgstat_profile_notice'] = message
    st.rerun()


def _error(exc):
    if isinstance(exc, (ValueError, RuntimeError)):
        st.error(str(exc))
    else:
        logger().exception('TGStat profile operation failed')
        st.error('Не удалось выполнить действие. Подробности в Dev-диагностике.')


def profile_picker(runner, ui):
    profiles = TGStatProfiles(runner.repo).list()
    labels = {item['id']: item['name'] for item in profiles}
    state = runner.snapshot()
    busy = state['status'] in ('running', 'stopping') or state.get('external_owner', False)
    selected_key = widget_key('selectbox', 'Профиль TGStat')
    pending = st.session_state.pop('_new_tgstat_profile', None)
    if pending in labels:
        st.session_state[selected_key] = pending
        ui.preferences.save(selected_key, pending)
    selected = ui.selectbox('Профиль TGStat', list(labels), format_func=labels.get, disabled=busy or not labels,
                            placeholder='Добавьте профиль', help='С этого профиля начнётся новый поиск.')
    auto_switch = ui.checkbox('Автопереключение TGStat', value=True, disabled=busy,
                              key=widget_key('checkbox', 'Автоматически пропускать ограниченные профили'),
                              help='При явном ограничении переходить к следующему профилю. Ограниченный профиль повторно не используется в запуске.')
    st.button('Управлять профилями', width='stretch', on_click=_navigate, args=('Профили TGStat', selected))
    return selected or DEFAULT_PROFILE, auto_switch


def _close(session_id):
    try:
        with st.spinner('Закрываю Chrome и жду записи профиля…'):
            result = close_retained(session_id) or {}
        message = ('Окно уже было закрыто вне панели.' if result.get('close_status') == 'already_closed' else 'Chrome закрыт штатно.')
        _flash(message + ' ' + AUTH_LABELS.get(result.get('auth_status'), 'Вход не подтверждён'))
    except Exception as exc:
        _error(exc)


def _profile_card(store, selected, labels, sessions, busy):
    saved = session_state(selected)
    st.subheader(labels[selected])
    status = saved.get('auth_status', 'not_checked')
    message = AUTH_LABELS.get(status, 'Вход не подтверждён')
    if status == 'authenticated':
        st.success('Последняя проверка: ' + message)
    elif status == 'not_authenticated':
        st.warning(message)
    else:
        st.info(message)
    if saved.get('checked_at'):
        st.caption('Проверено: ' + saved['checked_at'].replace('T', ' ')[:19] + ' UTC')
    if saved.get('auth_reason'):
        st.caption(saved['auth_reason'])
    opened = next((item for item in sessions if item.get('profile_id') == selected), None)
    first, second, third = st.columns(3)
    if first.button('Открыть Chrome', type='primary', width='stretch', disabled=busy or bool(sessions)):
        try:
            with st.spinner('Открываю профиль для входа…'):
                open_for_login(selected, repo=store.repo)
            _flash('Chrome открыт. Войдите в TGStat, затем нажмите «Проверить вход».')
        except Exception as exc:
            _error(exc)
    if second.button('Проверить вход', width='stretch', disabled=busy or opened is None):
        try:
            with st.spinner('Проверяю страницу TGStat…'):
                result = check_retained_login(opened['session_id'])
            _flash(AUTH_LABELS[result['auth_status']] + '. ' + result['auth_reason'])
        except Exception as exc:
            _error(exc)
    if third.button('Закрыть Chrome', width='stretch', disabled=busy or opened is None):
        _close(opened['session_id'])
    st.button('Использовать для поиска', disabled=busy, on_click=_navigate, args=('Поиск', selected))
    with st.expander('Название и удаление', expanded=False):
        if selected != DEFAULT_PROFILE:
            with st.form('rename_tgstat_profile_' + selected):
                name = st.text_input('Название профиля', value=labels[selected], max_chars=80)
                rename = st.form_submit_button('Сохранить название', disabled=busy)
            if rename:
                try:
                    store.rename(selected, name)
                    _flash('Название сохранено.')
                except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
                    _error(exc)
        st.caption('Удаление уберёт профиль из поиска и автопереключения. Его можно восстановить ниже в «Удалённых профилях».')
        if st.button('Удалить профиль', key='delete_tgstat_' + selected, disabled=busy or opened is not None):
            try:
                store.delete(selected)
                remaining = store.list()
                replacement = remaining[0]['id'] if remaining else None
                st.session_state['_new_tgstat_profile'] = replacement
                st.session_state['_tgstat_manager_pending'] = replacement
                _flash('Профиль удалён из активных. Данные Chrome сохранены для восстановления.')
            except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
                _error(exc)
    with st.expander('Как проверить сохранение входа', expanded=False):
        st.write('1. Откройте Chrome и войдите в нужный аккаунт TGStat.\n'
                 '2. Нажмите «Проверить вход», затем «Закрыть Chrome».\n'
                 '3. Откройте профиль повторно и снова проверьте вход.')
        st.caption('Последняя проверка не гарантирует, что сервер не отозвал сессию позже. Пароли и коды вводятся только на сайте TGStat.')


def profile_manager(runner, ui):
    st.subheader('Профили TGStat')
    st.caption('Отдельный аккаунт в каждом профиле Chrome. Выберите профиль слева, действия доступны справа.')
    notice = st.session_state.pop('_tgstat_profile_notice', None)
    if notice:
        st.info(notice)
    store = TGStatProfiles(runner.repo)
    profiles = store.list()
    labels = {item['id']: item['name'] for item in profiles}
    state = runner.snapshot()
    busy = state['status'] in ('running', 'stopping') or state.get('external_owner', False)
    if busy:
        st.info('Выполняется поиск. Управление аккаунтами будет доступно после остановки.')
    sessions = retained_sessions(include_url=False)
    pending = st.session_state.pop('_tgstat_manager_pending', None)
    selection_key = 'tgstat_manager_selection'
    if pending in labels:
        st.session_state[selection_key] = pending
    elif st.session_state.get(selection_key) not in labels:
        current = st.session_state.get(widget_key('selectbox', 'Профиль TGStat'))
        if current in labels:
            st.session_state[selection_key] = current
        else:
            st.session_state.pop(selection_key, None)
    left, right = st.columns([1, 2], gap='large')
    with left:
        with st.container(border=True):
            st.markdown('**Ваши аккаунты**')
            st.caption(f'Активных профилей: {len(profiles)}')
            selected = st.radio('Аккаунты TGStat', list(labels), format_func=labels.get, key=selection_key,
                                label_visibility='collapsed') if labels else None
            if not labels:
                st.info('Добавьте новый профиль или восстановите удалённый.')
        with st.expander('Добавить аккаунт', expanded=not profiles):
            with st.form('add_tgstat_profile'):
                name = st.text_input('Название нового профиля', max_chars=80, placeholder='Например: рабочий аккаунт')
                add = st.form_submit_button('Добавить профиль', disabled=busy)
            if add:
                try:
                    created = store.add(name)
                    st.session_state['_new_tgstat_profile'] = created
                    st.session_state['_tgstat_manager_pending'] = created
                    _flash('Профиль добавлен. Откройте Chrome, чтобы войти в TGStat.')
                except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
                    _error(exc)
    with right:
        if selected:
            with st.container(border=True):
                _profile_card(store, selected, labels, sessions, busy)
        for session in sessions:
            if session.get('profile_id') != selected:
                st.info('Сейчас открыт другой профиль: ' + store.name(session.get('profile_id')))
                if st.button('Закрыть другое окно Chrome', key='close_other_' + session['session_id'], disabled=busy):
                    _close(session['session_id'])
    deleted = store.archived()
    with st.expander(f'Удалённые профили ({len(deleted)})', expanded=False):
        st.caption('Сессии остаются на этом компьютере. Восстановление вернёт профиль в выбор и будущие новые поиски.')
        for item in deleted:
            name_col, action_col = st.columns([3, 1])
            name_col.write(item['name'])
            if action_col.button('Восстановить', key='restore_tgstat_' + item['id'], disabled=busy):
                try:
                    store.restore(item['id'])
                    st.session_state['_new_tgstat_profile'] = item['id']
                    st.session_state['_tgstat_manager_pending'] = item['id']
                    _flash('Профиль восстановлен.')
                except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
                    _error(exc)
