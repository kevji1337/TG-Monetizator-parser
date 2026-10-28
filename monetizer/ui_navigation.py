"""Sidebar navigation with a permanent route back to the search workspace."""
import streamlit as st

from .preferences import widget_key

SECTIONS = ['Поиск', 'База кандидатов', 'План контактов', 'История запусков',
            'Пакет ChatGPT', 'Качество отбора', 'Профили TGStat', 'Dev-диагностика']


def sidebar_navigation(ui):
    def home():
        key = widget_key('radio', 'Раздел')
        st.session_state[key] = 'Поиск'
        ui.preferences.save(key, 'Поиск')

    with st.sidebar:
        st.title('Монетизатор')
        st.caption('Telegram · рабочее пространство')
        st.button('На главную — поиск', icon=':material/home:', width='stretch', on_click=home)
        with st.expander('Разделы приложения', expanded=False):
            section = ui.radio('Раздел', SECTIONS, label_visibility='collapsed')
        st.caption('Сейчас: ' + section)
        st.divider()
    return section
