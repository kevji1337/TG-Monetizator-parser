"""Local profile controls. Chrome opens only after an explicit button click."""
import sqlite3

import streamlit as st

from .tgstat_profiles import TGStatProfiles, DEFAULT_PROFILE, open_for_login, session_state, AUTH_LABELS
from .diagnostics import retained_sessions, close_retained, check_retained_login
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
        notice = st.session_state.pop('_tgstat_profile_notice', None)
        if notice:
            st.info(notice)
        auto_switch = ui.checkbox('Автоматически пропускать ограниченные профили', value=True, disabled=busy,
                                  help='При явном ограничении TGStat переходить к следующему сохранённому профилю. Каждый пробуется один раз за запуск.')
        st.caption('Порядок: выбранный профиль, затем остальные по времени добавления. В каждый профиль заранее войдите вручную. '
                   'При CAPTCHA робот ждёт проверки в Chrome; при сетевой ошибке аккаунт не переключает.')
        st.caption('Названия — локальные ярлыки. В каждый аккаунт входите в отдельном окне Chrome; '
                   'пароли и коды в эту панель вводить не нужно. Сессии сохраняются на этом компьютере.')
        checks = []
        for item in profiles:
            saved = session_state(item['id'])
            checks.append({'Профиль': item['name'], 'Последняя проверка входа': AUTH_LABELS.get(saved.get('auth_status', 'not_checked'), 'Вход не подтверждён'),
                           'Проверено (UTC)': saved.get('checked_at', ''),
                           'Закрытие Chrome': {'closed': 'Штатно завершён', 'already_closed': 'Окно было закрыто вне панели',
                                              'open': 'Был открыт; завершение не подтверждено'}.get(saved.get('close_status'), 'Не проверено')})
        st.dataframe(checks, hide_index=True, width='stretch')
        st.caption('Это последняя проверка страницы, а не гарантия действующей сессии. Чтобы проверить сохранение входа, '
                   'закройте профиль, снова откройте его и нажмите «Проверить вход TGStat».')
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
                if st.button('Проверить вход TGStat: ' + label, key='check_profile_' + session['session_id'], disabled=busy):
                    try:
                        with st.spinner('Проверяю признаки входа на странице TGStat…'):
                            result = check_retained_login(session['session_id'])
                        st.session_state['_tgstat_profile_notice'] = AUTH_LABELS[result['auth_status']] + '. ' + result['auth_reason']
                        st.rerun()
                    except (ValueError, RuntimeError, OSError):
                        st.error('Не удалось проверить окно. Откройте страницу TGStat в этом Chrome и повторите проверку.')
                if st.button('Закрыть Chrome штатно: ' + label, key='close_profile_' + session['session_id'], disabled=busy):
                    try:
                        with st.spinner('Закрываю Chrome и жду записи профиля на диск…'):
                            result = close_retained(session['session_id']) or {}
                        closing = ('Окно уже было закрыто вне панели. ' if result.get('close_status') == 'already_closed'
                                   else 'Chrome закрыт штатно. ')
                        st.session_state['_tgstat_profile_notice'] = (closing +
                            AUTH_LABELS.get(result.get('auth_status'), 'Вход не подтверждён') +
                            '. Сохранение авторизации проверяется повторным открытием профиля.')
                        st.rerun()
                    except Exception:
                        st.error('Chrome не завершился штатно. Закройте его вручную, затем повторите эту кнопку. '
                                 'Профиль остаётся занят; подробности в Dev-диагностике.')
        st.caption('Открытие страницы входа не подтверждает авторизацию. Войдите в TGStat в Chrome и проверьте имя аккаунта на сайте. '
                   'Новый поиск начинается с выбранного профиля. Продолжение использует сохранённую очередь профилей запуска.')
    return selected, auto_switch
