"""Live diagnostics controls, loaded only when requested."""
import streamlit as st
from .diagnostics import LOG_PATH, log_tail, retained_sessions, close_retained


@st.fragment(run_every='1s')
def diagnostics_panel(runner):
    st.subheader('Dev-диагностика')
    st.caption('Лог содержит этапы, время выполнения, ответы TGStat и traceback ошибок. Cookies, пароли и HTML страницы не сохраняются.')
    state = runner.snapshot()
    st.json({'status': state['status'], 'stage': state['stage'], 'browser': state.get('browser', {}), 'error': state.get('error', '')})
    if state.get('browser', {}).get('waiting'):
        st.warning(state['browser']['message'])
        if st.button('Продолжить после входа', key='diag_resume'):
            runner.resume_browser()
    for session in retained_sessions():
        st.write('Открытое окно диагностики: ' + session['session_id'])
        st.json(session)
        if st.button('Закрыть сохранённый Chrome', key='close_' + session['session_id']):
            close_retained(session['session_id'])
            st.rerun()
    tail = log_tail()
    st.code(tail, language=None)
    st.download_button('Скачать лог диагностики', tail, 'monetizer-debug.log', mime='text/plain')
    st.caption('Файл на диске: ' + str(LOG_PATH))
