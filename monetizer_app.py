"""Local Streamlit UI; worker threads never call Streamlit APIs."""
import re
import hashlib
import json
import os
import time
from collections import Counter

import streamlit as st

from monetizer.export import COLUMNS, csv_bytes, xlsx_bytes, REPORTS_DIR
from monetizer.audit import WebAuditor
from monetizer.keywords import MICRO_NICHES
from monetizer.categories import load_categories, category_label, assessment_profiles
from monetizer.search import refresh_categories
from monetizer.pitches import (generate_pitch, compose_pitch, evidence_sources, suggest_scenarios,
                              chatgpt_prompt, SCENARIOS, PLAYBOOK_VERSION)
from monetizer.pitches import direct_scenario
from monetizer.runner import JobRunner, sorted_rows
from monetizer.settings import Settings, AI_BASE_URL, AI_MODEL
from monetizer.ai_audit import STATUS_LABELS
from monetizer.web2api import inspect_proxy, ProxyError
from monetizer.ui_workspace import render_workspace, show_evidence
from monetizer.qualification import TYPE_LABELS
from monetizer.ui_quality import profile_panel
from monetizer.profile import profile_version
from monetizer.diagnostics import logger
from monetizer.preferences import Preferences, PersistentUI, api_key_field, last_run_preferences
from monetizer.candidate_insights import selection_summary, selection_bucket, ai_priority, priority_key, BUCKETS
from monetizer.ui_tgstat_profiles import profile_picker, profile_manager
from monetizer.tgstat_profiles import TGStatProfiles
from monetizer.runtime import acquire_runner
from monetizer.ui_navigation import sidebar_navigation
from monetizer.ui_style import apply_style
from monetizer.ui_tables import COMPACT_COLUMNS, candidate_columns, workspace_columns, search_locked

PAGE_STARTED = time.perf_counter()

st.set_page_config(page_title="Монетизатор · Telegram", page_icon="🔎", layout="wide", initial_sidebar_state='expanded')
apply_style()


def get_runner():
    return acquire_runner(JobRunner)


@st.cache_data(show_spinner=False, max_entries=8)
def downloads(rows):
    return csv_bytes(rows), xlsx_bytes(rows)


runner = get_runner()
run_snapshot = runner.snapshot()
search_busy = search_locked(run_snapshot)
preferences = Preferences(runner.repo, last_run_preferences(run_snapshot))
if search_busy:
    # Show the actual immutable run conditions, rather than next-run draft edits.
    for key, value in last_run_preferences(run_snapshot).items():
        if value is not None and st.session_state.get(key) != value:
            st.session_state[key] = value
ui = PersistentUI(preferences)
search_ui = PersistentUI(preferences, disabled=search_busy)
section = sidebar_navigation(ui)
st.title("Монетизатор трафика")
st.caption("Поиск публичных Telegram-каналов экспертов · аудит просмотров · черновики предложений")

with st.sidebar:
    primary_settings = st.container()
    start_actions = st.container()
    st.divider()
    advanced_filters = st.container()
    advanced_connections = st.container()
    with advanced_connections:
        with st.expander('AI-аудит Gemini', expanded=False):
            ai_enabled = search_ui.checkbox('Анализировать посты через Gemini', value=True)
            ai_key = api_key_field(preferences, disabled=search_busy)
            ai_base_url = search_ui.text_input('Base URL AI', value=AI_BASE_URL)
            ai_model = search_ui.text_input('Model ID', value=AI_MODEL)
            ai_max_audits = search_ui.number_input('Максимум запросов AI за запуск', 1, 5, 5)
            ai_timeout = search_ui.number_input('Таймаут AI, секунд', 10, 900, 600, step=10,
                                         help='Общий срок ожидания, включая запуск ответа и reasoning. Подключение ограничено отдельно: 5 секунд.')
            ai_prompt_bytes = search_ui.number_input('Лимит текста AI-запроса, байт UTF-8', 16000, 120000, 96000, step=4000)
            st.caption('Через ваш gemini-web2api-go. Отправляются публичные посты, описание и профиль отправителя. '
                       'Один запрос за раз; ошибки тоже расходуют лимит. Кэш действует 24 часа.')
            st.caption('Streaming показывает ожидание, получение reasoning и итогового ответа. Текст reasoning не сохраняется. '
                       'Остановка закрывает соединение; удалённая генерация может продолжиться на прокси.')
            if ai_timeout <= 180:
                st.warning('Сохранён короткий таймаут AI. Для Argon Thinking с upstream timeout 180 с и тремя попытками '
                           'web2api увеличьте его до 600 с. Прежнее значение не меняется автоматически.')
            connection_key = (ai_key or os.environ.get('MONETIZER_AI_KEY', '')).strip()
            connection_signature = hashlib.sha256((ai_base_url + '\0' + connection_key).encode()).hexdigest()
            if st.button('Проверить web2api — без генерации', disabled=search_busy):
                try:
                    with st.spinner('Проверяю версию и список моделей, без AI-аудита…'):
                        connection_info = inspect_proxy(ai_base_url, connection_key)
                    st.session_state['_web2api_connection'] = (connection_signature, connection_info)
                except (ProxyError, ValueError) as exc:
                    st.error(str(exc))
            connection = st.session_state.get('_web2api_connection')
            if connection and connection[0] == connection_signature:
                info = connection[1]
                st.caption('web2api: ' + (info['version'] or 'версия неизвестна') +
                           (' · API-ключ принят' if info['auth_checked'] else ' · ключ не подтверждён'))
                if info['models_checked'] and ai_model in info['models']:
                    st.success('Model ID есть в списке API. Реальная генерация не проверялась.')
                elif info['models_checked']:
                    st.error('Model ID отсутствует в списке API. Проверьте имя и Cookie Pool в web2api.')
                for note in info['notes']:
                    st.warning(note)
                if info['version'] == '4.23.0':
                    st.warning('В web2api 4.23.0 нет strict_model, а смена upstream timeout может требовать перезапуска прокси. '
                               'Исправление таймаута появилось в 4.24.1. Фактическая модель видна в Requests web2api.')
                st.caption('Список моделей не подтверждает действительность cookie или оставшуюся квоту аккаунта.')
        ai_config = dict(ai_base_url=ai_base_url, ai_model=ai_model, ai_max_audits=ai_max_audits,
                         ai_timeout=ai_timeout, ai_prompt_bytes=ai_prompt_bytes)
        st.caption('Настройки сохраняются автоматически. Текстовые поля — после Enter или выхода из поля.')
        with st.expander('Режим разработки'):
            dev_mode = search_ui.checkbox('Dev-режим', value=os.environ.get('MONETIZER_DEV') == '1')
            keep_browser = search_ui.checkbox('Оставлять Chrome открытым при ошибке', value=True)
            browser_wait = search_ui.number_input('Ожидание ручного входа, секунд', 0, 1800, 300, step=30)
        sender_profile = profile_panel(runner.repo, disabled=search_busy)
    with primary_settings:
        st.subheader("Новый поиск")
        mode = search_ui.radio("Источник", ["TGStat — каталог тематик", "Список каналов — только аудит"])
        tgstat_profile_id, tgstat_auto_switch = profile_picker(runner, search_ui) if mode.startswith('TGStat') else ('default', False)
        snapshot = load_categories()
        labels = {c['id']: c['title'] for c in snapshot['categories']}
        previous = st.session_state.get('tgstat_category_ids')
        if previous is not None and any(c not in labels for c in previous):
            st.session_state['tgstat_category_ids'] = [c for c in previous if c in labels]
        category_ids = search_ui.multiselect('Тематики TGStat', list(labels), default=['34'] if '34' in labels else [],
                                      format_func=labels.get, key='tgstat_category_ids',
                                      placeholder='Выберите одну или несколько тематик')
        topic = '; '.join(labels[c] for c in category_ids)
        micro_id = ''
        if mode.startswith("TGStat"):
            channels_text = ""
            st.caption('Поиск по выбранным тематикам самого TGStat. Поле текстового запроса остаётся пустым.')
            with st.expander('Список тематик'):
                st.caption(f"Тематик: {len(labels)}. Список прочитан с TGStat: {snapshot['observed_at'][:10]}.")
                st.caption('При поиске ID проверяется по форме сайта. Обновление списка открывает Chrome только по этой кнопке.')
                if st.button('Обновить тематики с TGStat', disabled=search_busy):
                    try:
                        with st.spinner('Читаю список тематик TGStat в Chrome…'):
                            refresh_categories(Settings(dev_mode=dev_mode, keep_browser_on_error=keep_browser,
                                                        browser_wait_seconds=browser_wait, tgstat_profile_id=tgstat_profile_id))
                        st.rerun()
                    except Exception as exc:
                        st.error(f'Не удалось обновить тематики: {exc}')
                        st.caption('Полная ошибка сохранена в разделе «Dev-диагностика». Сохранённый список тематик остаётся доступным.')
        else:
            channels_text = search_ui.text_area("@username или ссылки t.me — по одному на строку", height=150)
        count = search_ui.number_input("Цель: подходящих каналов", 1, 1000, 30, step=1)
        new_only = search_ui.checkbox("Искать только новые каналы", value=True, disabled=not mode.startswith("TGStat"))
        login_pause = search_ui.checkbox("Дать минуту на ручной вход в TGStat", disabled=not mode.startswith("TGStat"))
    with advanced_filters:
        with st.expander('Фокус аудита — необязательно'):
            st.caption('Это дополнительный отбор по содержанию после сбора. Он не меняет тематику TGStat и не отправляет слова в поиск.')
            profiles = assessment_profiles(category_ids) if category_ids else list(MICRO_NICHES.values())
            micro_id = search_ui.selectbox('Проверять конкретную специализацию', [''] + [m['id'] for m in profiles],
                                    format_func=lambda value: MICRO_NICHES[value]['title'] if value else 'Все специализации выбранных тематик')
        with st.expander('Фильтры TGStat', expanded=False):
            st.caption('Применяются при поиске на TGStat. Для готового списка каналов работают критерии аудита ниже.')
            country_labels = {c['id']: c['title'] for c in snapshot['countries']}
            language_labels = {c['id']: c['title'] for c in snapshot['languages']}
            countries = search_ui.multiselect('Страна канала', list(country_labels), format_func=country_labels.get,
                                       placeholder='Любая страна')
            languages = search_ui.multiselect('Язык канала', list(language_labels), default=['3'] if '3' in language_labels else [], format_func=language_labels.get,
                                       placeholder='Любой язык')
            channel_types = {'': 'Любой', 'public': 'Публичный', 'private': 'Приватный'}
            channel_type = search_ui.selectbox('Тип канала', list(channel_types), format_func=channel_types.get)
            age = search_ui.slider('Возраст канала, месяцев', 0, 120, (12, 120))
            err = search_ui.slider('Уровень вовлечённости ERR, %', 0, 100, (10, 100))
            st.caption('Крайние значения 120 месяцев и 100% — это 120+ и 100+ в форме TGStat, без верхнего ограничения.')
            er = search_ui.slider('Уровень вовлечённости ER от, %', 0.0, 10.0, 0.0, step=.5)
            male = search_ui.slider('Мужская аудитория от, %', 0, 90, 0, step=5)
            female = search_ui.slider('Женская аудитория от, %', 0, 90, 0, step=5)
            min_subs = search_ui.number_input("Подписчики от", 1, 100_000_000, 3000)
            max_subs = search_ui.number_input("Подписчики до", 1, 100_000_000, 30000)
            tgstat_views_from = search_ui.number_input('Средний охват поста TGStat от', min_value=0, value=None, placeholder='Без ограничения')
            tgstat_views_to = search_ui.number_input('Средний охват поста TGStat до', min_value=0, value=None, placeholder='Без ограничения')
            daily = search_ui.number_input('Средний охват поста за 24 часа от', 0, 10_000_000, 300)
            daily_to = search_ui.number_input('Средний охват поста за 24 часа до', min_value=0, value=None, placeholder='Без ограничения')
            ci_from = search_ui.number_input('Индекс цитирования от', min_value=0.0, value=None, step=.1, placeholder='Без ограничения')
            ci_to = search_ui.number_input('Индекс цитирования до', min_value=0.0, value=None, step=.1, placeholder='Без ограничения')
            exclude_dead = search_ui.checkbox('Скрыть мёртвые каналы', value=True)
            exclude_scam = search_ui.checkbox('Без SCAM/FAKE', value=True)
            exclude_red = search_ui.checkbox('Без красной метки', value=True)
            st.caption('Приватные каналы без публичного username или превью t.me/s нельзя проверить этим аудитором.')
        with st.expander("Критерии и скорость"):
            st.caption('Дополнительные критерии аудита публичного превью; они отделены от фильтров поиска TGStat.')
            avg_ratio = search_ui.number_input('Средние просмотры от подписчиков, %', 0.0, 100.0, 10.0, step=0.5,
                                        help='Средние просмотры постов / число подписчиков × 100. Например, 10% при 5000 подписчиков — 500 просмотров в среднем. 0 отключает этот порог.')
            post_age = search_ui.number_input('Возраст поста для оценки активности, часов', 1, 168, 24,
                                       help='Берётся пост, ближайший к этому возрасту, в пределах допустимого отклонения.')
            post_tolerance = search_ui.number_input('Допустимое отклонение возраста, часов', 0, post_age, min(6, post_age))
            post_ratio = search_ui.number_input('Просмотры поста от подписчиков, %', 0.1, 100.0, 10.0, step=0.5)
            st.caption(f'Берётся ближайший к {post_age} ч пост возрастом {post_age - post_tolerance}–{post_age + post_tolerance} ч. '
                       f'Порог: {post_ratio:g}% подписчиков. При 5000 подписчиков и 10% нужно 500 просмотров.')
            gap = search_ui.number_input("Максимальный интервал публикаций, дней", 1.0, 14.0, 4.0, step=.5)
            workers = search_ui.slider("Параллельных аудитов", 1, 8, 4)
            rate = search_ui.slider("Запросов к Telegram в секунду", .5, 5.0, 2.0, step=.5)
            audit_budget = search_ui.number_input("Максимум аудитов: 0 = десять целей", 0, 10000, 0, step=10)
            time_limit = search_ui.number_input("Лимит времени, минут", 1.0, 1440.0, 60.0)
    with start_actions:
        bounds = [(min_subs, max_subs, 'подписчики'), (tgstat_views_from, tgstat_views_to, 'охват поста TGStat'),
                  (daily, daily_to, 'охват за 24 часа'), (ci_from, ci_to, 'индекс цитирования')]
        invalid_bounds = [label for lower, upper, label in bounds if lower is not None and upper is not None and lower > upper]
        if invalid_bounds:
            st.warning('Минимум больше максимума: ' + ', '.join(invalid_bounds) + '.')
        if search_busy:
            st.info('Поиск идёт. Категории и условия зафиксированы. Чтобы изменить их, остановите запуск и начните новый поиск. Просмотр результатов доступен.')
        if st.button("Начать поиск и аудит" if mode.startswith("TGStat") else "Начать аудит", type="primary", width="stretch",
                     disabled=search_busy or bool(invalid_bounds) or (mode.startswith('TGStat') and not TGStatProfiles(runner.repo).list())):
            try:
                settings = Settings(min_subs=min_subs, max_subs=max_subs, min_daily_reach=daily,
                                    min_avg_views=0, min_avg_view_ratio=avg_ratio, target_count=count, target_post_age_hours=post_age,
                                    post_age_tolerance_hours=post_tolerance, min_post_view_ratio=post_ratio,
                                    max_gap_days=gap, workers=workers, requests_per_second=rate,
                                    login_wait_seconds=60 if login_pause else 0, max_audits=audit_budget,
                                    max_minutes=time_limit, new_only=new_only, micro_id=micro_id,
                                    search_mode='categories' if mode.startswith('TGStat') else 'manual', category_ids=category_ids,
                                    country_ids=countries, language_ids=languages, channel_type=channel_type,
                                    min_age_months=age[0], max_age_months=age[1] if age[1] < 120 else None,
                                    min_err=err[0], max_err=err[1] if err[1] < 100 else None,
                                    min_er=er, min_male=male, min_female=female,
                                    tgstat_min_avg_reach=tgstat_views_from, tgstat_max_avg_reach=tgstat_views_to,
                                    max_daily_reach=daily_to, min_ci=ci_from, max_ci=ci_to,
                                    exclude_dead=exclude_dead, exclude_scam=exclude_scam, exclude_red_label=exclude_red,
                                    dev_mode=dev_mode, keep_browser_on_error=keep_browser, browser_wait_seconds=browser_wait,
                                    tgstat_profile_id=tgstat_profile_id,
                                    tgstat_auto_switch=tgstat_auto_switch,
                                    ai_enabled=ai_enabled, **ai_config)
                usernames = re.split(r"[\s,;]+", channels_text.strip()) if channels_text.strip() else []
                if not mode.startswith("TGStat") and not usernames:
                    raise ValueError("Добавьте хотя бы один канал")
                run_topic = topic or 'Заданные каналы'
                if micro_id:
                    run_topic += ' · ' + MICRO_NICHES[micro_id]['title']
                runner.start(settings, niche=run_topic, usernames=usernames, api_key=ai_key)
                st.rerun()
            except (ValueError, RuntimeError, OSError) as exc:
                st.error(str(exc))
        st.caption('Настройки сохраняются автоматически. Поиск работает в фоне при переходе между разделами.')


@st.fragment(run_every="2s")
def results_panel():
    state = runner.snapshot()
    if search_locked(state) != search_busy:
        st.rerun()  # Unlock the sidebar when the background job finishes.
    running = state["status"] in ("running", "stopping")
    count_found, rows = len(state["candidates"]), sorted_rows(state["rows"])
    green = sum(not r.get('assessment_stale') and r.get("status") in ("top", "recommended") for r in rows)
    has_ai = state.get('settings', {}).get('ai_enabled', False)
    ai_waiting = len(state.get('ai_queue', []))
    metrics = st.columns(5 if has_ai else 4)
    metrics[0].metric("Новых каналов", count_found, help='Уникальные каналы, добавленные в текущий запуск. Повторы не учитываются.')
    metrics[1].metric("Проверено в Telegram", len(rows), help='Каналы с завершённой попыткой публичного аудита, включая ошибки доступа.')
    metrics[2].metric("Подходящие / цель", f"{green} / {state.get('settings', {}).get('target_count', 30)}",
                      help='Каналы, прошедшие все включённые проверки. Ожидающие AI не засчитываются.')
    if has_ai:
        metrics[3].metric('Ожидают AI', ai_waiting, help='Собранные каналы в сохранённой очереди AI. Это ещё не подходящие эксперты.')
    metrics[-1].metric("Ошибки доступа", sum(r.get("status") == "error" for r in rows), help='Не удалось загрузить или разобрать публичную ленту Telegram.')
    st.write(f"**{state['stage']}**")
    if state.get('settings', {}).get('ai_enabled'):
        st.caption(f"AI-запросы: {state.get('ai_requests_used', 0)}/{state['settings'].get('ai_max_audits', 5)}. "
                   'Кэш не расходует лимит. Зелёная метка требует проверенного ответа AI и подходящих метрик.')
        st.caption(state.get('ai_stage', 'AI-очередь сохраняется отдельно от очереди поиска'))
        if state.get('ai_paused') or state.get('ai_requests_used', 0) >= state['settings'].get('ai_max_audits', 5):
            st.info('AI-запросы приостановлены. Публичный сбор продолжает наполнять очередь до цели или лимита поиска. '
                    'Кандидаты в очереди ещё не подтверждены как подходящие эксперты.')
        if state.get('stop_reason') == 'ai_pending':
            st.warning(f'Сохранено кандидатов для AI: {ai_waiting}. Подтверждено: {green}. '
                       'Это частичный результат; очередь можно разобрать отдельно без Chrome.')
    if state.get('assessment_pending'):
        st.warning(f"Устарели автоматические оценки {state['assessment_pending']} каналов. Они не засчитываются в цель, пока не выполнен пересчёт.")
        if st.button('Пересчитать сохранённые оценки', disabled=running or state.get('external_owner', False)):
            progress = st.progress(0.0, text='Пересчёт по сохранённым текстам, без загрузки Telegram…')
            try:
                runner.reassess_saved_run(lambda done, total: progress.progress(done / total, text=f'Пересчитано {done}/{total}'))
                st.rerun()
            except (ValueError, RuntimeError, OSError) as exc:
                st.error(f'Не удалось пересчитать оценки: {exc}')
    st.caption('Автоматические CSV и Excel: ' + str(REPORTS_DIR))
    with st.expander('Что означают статусы каналов'):
        st.markdown('''
| Статус | Значение |
| --- | --- |
| 🟢 РЕКОМЕНДОВАН | Подходят метрики, есть признаки личного эксперта и соответствие теме. Предварительный отбор. |
| 🟢 ТОП | Тот же отбор плюс ссылка YouTube или Instagram. Принадлежность ссылки и наличие воронки требуют проверки. |
| 🟡 Требует проверки | Недостаточно данных, признаки конфликтуют или есть сомнения в регулярности публикаций. |
| 🚩 Не прошёл отбор | Не выполнен числовой критерий, не совпадает тема или определён нецелевой тип канала. |
| ⚪ Не удалось проверить | Ошибка доступа/загрузки или нет публичного превью. Это не оценка качества канала. |
''')
        st.caption('Конкретное основание — в столбце «Основания» и карточке канала. Рабочие статусы «Написал», «Ответил» и другие задаются вручную, отдельно от автоотбора.')
    if state.get('browser', {}).get('waiting'):
        st.warning(state['browser']['message'])
        if st.button('Продолжить после входа', key='search_resume'):
            runner.resume_browser()
    elif any(cursor.get('blocked') for cursor in state.get('query_state', {}).values()):
        st.warning('TGStat ограничил новые страницы одной или нескольких тематик. ' +
                   ('Робот может продолжать аудит уже найденных каналов и поиск по другим доступным тематикам.' if running else
                    'Сохранённые каналы доступны для отдельного AI-разбора. Для возобновления поиска может потребоваться вход в Chrome.'))
    if state.get("niche"):
        st.caption(f"Текущий отчёт: {state['niche']}")
        if not state.get('manual') and not state.get('ai_only'):
            plan = state.get('tgstat_profiles', {})
            active_profile = plan.get('active', state.get('settings', {}).get('tgstat_profile_id', 'default'))
            st.caption('Текущий профиль TGStat: ' + TGStatProfiles(runner.repo).name(active_profile))
            if plan.get('skipped'):
                with st.expander('Пропущенные профили TGStat', expanded=False):
                    for profile_id, detail in plan['skipped'].items():
                        st.write(plan.get('names', {}).get(profile_id, profile_id) + ': ' + detail['reason'])
                    if not running and st.button('Повторить пропущенные профили TGStat', disabled=state.get('external_owner', False)):
                        try:
                            runner.retry_tgstat_profiles()
                            st.rerun()
                        except (ValueError, RuntimeError, OSError) as exc:
                            st.error(str(exc))
    if running:
        st.progress(min(green / state["settings"]["target_count"], 1.0),
                    text=f"Подходят {green}; проверено {len(rows)}; найдено {count_found}")
        if has_ai:
            st.progress(min((green + ai_waiting) / state['settings']['target_count'], 1.0),
                        text=f'Сбор для AI: {ai_waiting} в очереди, {green} подтверждено. Очередь ещё не означает подходящих экспертов.')
        if state.get("external_owner"):
            st.info("Поиск выполняется в другом процессе. Его прогресс показан из базы; остановите его в окне запуска.")
        if st.button("Остановить и сохранить прогресс", disabled=state["status"] == "stopping" or state.get("external_owner", False)):
            runner.stop()
            st.rerun()
    elif state["status"] in ("cancelled", "interrupted", "failed", "partial"):
        st.caption('Продолжение сохраняет категории и фильтры прежнего запуска. Для другого набора тематик начните новый поиск слева.')
        if state.get('settings', {}).get('search_mode', 'keywords') == 'keywords' and not state.get('manual'):
            st.info('Сохранён прежний поиск по словам. Его продолжение сохранит прежний способ поиска. Для поиска по тематикам начните новый запуск слева.')
        if st.button("Продолжить сохранённый запуск"):
            try:
                resume_config = {**state["settings"], "max_audits": audit_budget, "max_minutes": time_limit,
                                 'min_avg_views': 0, 'min_avg_view_ratio': avg_ratio,
                                 'target_post_age_hours': post_age, 'post_age_tolerance_hours': post_tolerance, 'min_post_view_ratio': post_ratio,
                                 "login_wait_seconds": 60 if login_pause else 0,
                                 'dev_mode': dev_mode, 'keep_browser_on_error': keep_browser, 'browser_wait_seconds': browser_wait,
                                 'tgstat_profile_id': state['settings'].get('tgstat_profile_id', 'default'),
                                 'tgstat_auto_switch': tgstat_auto_switch,
                                 'ai_enabled': ai_enabled, **ai_config}
                runner.start(settings=Settings.from_saved(resume_config), resume=True, api_key=ai_key)
                st.rerun()
            except (ValueError, RuntimeError, OSError) as exc:
                st.error(str(exc))
    if not running and ai_waiting:
        st.caption('Продолжение использует остаток лимита текущего запуска. Новый AI-разбор создаёт отдельный запуск '
                   'с сохранёнными постами и новым лимитом до пяти запросов; TGStat и Telegram повторно не загружаются.')
        if st.button('Разобрать только очередь AI — без TGStat', disabled=not ai_enabled or state.get('external_owner', False)):
            try:
                queue_config = {**state['settings'], **ai_config, 'ai_enabled': True, 'max_minutes': time_limit}
                runner.start(Settings.from_saved(queue_config), resume=True, api_key=ai_key, ai_only=True)
                st.rerun()
            except (ValueError, RuntimeError, OSError) as exc:
                st.error(str(exc))
        if st.button('Новый AI-разбор сохранённых каналов — до 5 запросов', disabled=not ai_enabled or state.get('external_owner', False)):
            try:
                queue_config = {**state['settings'], **ai_config, 'ai_enabled': True, 'max_minutes': time_limit}
                runner.start(Settings.from_saved(queue_config), resume=True, api_key=ai_key, ai_only=True, new_ai_round=True)
                st.rerun()
            except (ValueError, RuntimeError, OSError) as exc:
                st.error(str(exc))
    if state.get("error"):
        st.error(state["error"])
    with st.expander("Ход работы", expanded=running):
        st.code("\n".join(state["logs"][-35:]) or "Выберите нишу и запустите поиск слева.", language=None)
    if state.get("query_stats"):
        import pandas as pd
        with st.expander("Эффективность тематик / прежних запросов"):
            st.dataframe(pd.DataFrame([{"Тематика / запрос": category_label(q), **stats} for q, stats in state["query_stats"].items()]),
                         hide_index=True, width="stretch")
            history = runner.repo.query_history(state.get("query_scope", ""), state.get("run_id"))
            if history:
                st.caption("Предыдущие запуски с теми же направлением, фильтрами и версией правил")
                st.dataframe(pd.DataFrame([{"Тематика / запрос": category_label(q), **stats} for q, stats in history.items()]), hide_index=True, width="stretch")
    if not rows:
        st.info("Результаты появятся по мере аудита. Можно также проверить свой список публичных каналов.")
        return
    import pandas as pd
    with st.expander('Сводка отбора и причины'):
        st.caption(f'Проверено Telegram-каналов: {len(rows)}. Каждый канал учитывается ровно в одной главной причине; '
                   'непроверенные находки TGStat сюда не входят. «Проверено AI» означает корректный ответ, а не прохождение отбора.')
        st.dataframe(pd.DataFrame(selection_summary(rows)), hide_index=True, width='stretch')
        types = Counter(TYPE_LABELS.get(row.get('channel_type'), 'Не установлено') for row in rows)
        st.caption('Типы всех проверенных каналов, включая те, которые также не прошли метрики:')
        st.dataframe(pd.DataFrame([{'Тип канала': kind, 'Каналов': value} for kind, value in types.most_common()]),
                     hide_index=True, width='stretch')
    if has_ai:
        with st.expander('Очередь AI по приоритету'):
            waiting_rows = sorted([row for row in rows if row.get('ai_queued')], key=priority_key)
            st.caption('Сначала личная практика и соответствие теме. Балл управляет порядком проверки, не зелёной меткой. '
                       'Первые кандидаты выбираются после небольшой начальной выборки: до 12 публичных аудитов или 8 кандидатов.')
            st.dataframe(pd.DataFrame([{'Канал': row['username'], 'Название': row.get('title'),
                                        'Приоритет': ai_priority(row)['ai_priority_score'],
                                        'Почему': ai_priority(row)['ai_priority_reason']} for row in waiting_rows[:15]],
                                      columns=['Канал', 'Название', 'Приоритет', 'Почему']), hide_index=True, width='stretch',
                         column_config=workspace_columns(), row_height=38)
    reason_filter = ui.multiselect('Отбор по главной причине', list(BUCKETS), format_func=BUCKETS.get,
                                   placeholder='Все причины')
    if 'non_target' in reason_filter:
        st.caption('Для просмотра нецелевых типов снимите ниже флажок «Скрыть новости, агрегаторы, школы…».')
    st.caption("Зелёная метка: метрики, включая активность поста около заданного возраста, + признаки личной практики минимум в двух источниках + соответствие теме. Это предварительный отбор. Охват TGStat и счётчики просмотров Telegram показываются отдельно. Низкое отношение просмотров к подписчикам не доказывает накрутку.")
    filter_columns = st.columns([1, 1, 2])
    only_green = ui.on(filter_columns[0]).checkbox("Только зелёные")
    only_contact = ui.on(filter_columns[1]).checkbox("С кандидатом контакта")
    search = ui.on(filter_columns[2]).text_input("Поиск в результатах", placeholder="Название или @username")
    hide_non_target = ui.checkbox('Скрыть новости, агрегаторы, школы, бренды, магазины и доски вакансий', value=True)
    selected = [r for r in rows if (not only_green or (not r.get('assessment_stale') and r.get("status") in ("top", "recommended")))
                and (not hide_non_target or (r.get('channel_type') or 'unknown') in ('expert', 'unknown'))
                and (not only_contact or r.get("author_contact"))
                and (not reason_filter or selection_bucket(r) in reason_filter)
                and (not search or search.casefold() in f"{r.get('title', '')} {r['username']}".casefold())]
    st.caption(f"Показано {len(selected)} из {len(rows)}. Скачивание учитывает фильтры.")
    st.caption('Неопределённые каналы остаются с жёлтой меткой; они не входят в цель поиска. Снимите фильтр, чтобы увидеть отклонённые типы и основания.')
    visible = ["verdict", "channel_type", "title", "username", "subs", "activity_ratio", "activity_post_age_hours", "activity_post_url", "avg_views", "median_views", "view_ratio", "freq",
               "last_post", "youtube", "instagram", "author_contact", "reasons", "link"]
    if state.get('settings', {}).get('ai_enabled'):
        visible.insert(2, 'ai_audit_status')
        visible.insert(3, 'ai_priority_score')
    visible += ['next_contact_date', 'next_action_label']
    table_mode = ui.radio('Вид таблицы', ['Компактный', 'Подробный'], horizontal=True)
    frame = pd.DataFrame([{COLUMNS[k]: '⏳ Оценка устарела' if k == 'verdict' and r.get('assessment_stale') else
                           STATUS_LABELS.get(r.get(k), '') if k == 'ai_audit_status' else
                           TYPE_LABELS.get(r.get(k), 'Не установлено') if k == 'channel_type' else r.get(k)
                           for k in visible} for r in selected], columns=[COLUMNS[k] for k in visible])
    table_columns = [COLUMNS[k] for k in COMPACT_COLUMNS if k in visible] if table_mode == 'Компактный' else list(frame.columns)
    st.dataframe(frame, hide_index=True, width="stretch", height=min(570, max(150, 42 + len(frame) * 38)), row_height=38,
                 column_order=table_columns,
                 column_config=candidate_columns(state.get('settings', {}).get('target_post_age_hours', 24)))
    if selected:
        csv_data, xlsx_data = downloads(selected)
        filename = f"Монетизация_{state.get('run_id', 'каналы')[:12]}"
        c1, c2 = st.columns(2)
        c1.download_button("Скачать Excel", xlsx_data, filename + ".xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        c2.download_button("Скачать CSV", csv_data, filename + ".csv", mime="text/csv")
        st.subheader("Карточка канала и сообщение")
        chosen = st.selectbox("Канал для черновика", [r["username"] for r in selected])
        row = next(r for r in selected if r["username"] == chosen)
        if st.button('AI-аудит этого канала по сохранённым постам', disabled=running or not ai_enabled):
            try:
                with st.spinner('Gemini анализирует тексты; используется лимит текущего запуска…'):
                    result = runner.audit_with_ai(chosen, ai_key, ai_config)
                if result.get('ai_audit_status') not in ('success', 'cached'):
                    st.warning(STATUS_LABELS.get(result.get('ai_audit_status'), 'AI не выполнен') + ': ' + result.get('ai_detail', ''))
                st.rerun()
            except (ValueError, RuntimeError, OSError) as exc:
                st.error(str(exc))
        show_evidence(row, runner.repo, running)
        if row.get('api_ai_review', {}).get('message'):
            if st.button('Принять черновик Gemini как сохранённое сообщение', disabled=running):
                review = row['api_ai_review']
                try:
                    runner.save_pitch(chosen, review['message'], 'gemini_api', review['evidence'][0]['source_id'])
                    st.rerun()
                except (ValueError, RuntimeError, OSError) as exc:
                    st.error(str(exc))
        context_key = f"content_{state.get('run_id')}_{chosen}"
        editor_row = {**row, **st.session_state.get(context_key, {})}
        st.caption("Редактор работает локально по загруженным сценариям. Для свободной AI-редактуры можно скачать промпт с данными канала и открыть его в ChatGPT или Claude.")
        if st.button("Загрузить / обновить тексты канала", disabled=running):
            try:
                with st.spinner("Загружаю публичное описание и последние посты…"):
                    fresh = runner.reaudit(chosen)
                if fresh.get("status") == "error":
                    st.error(fresh.get("error", fresh.get("reasons")))
                else:
                    context = fresh
                    st.session_state[context_key] = context
                    editor_row.update(context)
            except (ValueError, RuntimeError, OSError) as exc:
                st.error(str(exc))
        sources = evidence_sources(editor_row)
        source = None
        if sources:
            source_ids = [s["id"] for s in sources]
            inferred_source = direct_scenario(editor_row)["evidence_id"]
            default_source = next((i for i, s in enumerate(sources) if s["id"] == inferred_source),
                                  next((i for i, s in enumerate(sources) if s["id"] != "description" and not s.get("forwarded")), 0))
            source_id = st.selectbox("Источник наблюдения", source_ids, index=default_source,
                                     format_func=lambda value: next(s["label"] for s in sources if s["id"] == value))
            source = next(s for s in sources if s["id"] == source_id)
            with st.expander("Текст выбранного источника"):
                st.text(source["text"])
                if source.get("url"):
                    st.link_button("Открыть источник", source["url"])
                if source.get("forwarded"):
                    st.warning("Это пересланный материал; авторство владельца канала не подтверждено.")
            quote_seed = hashlib.sha256(source["text"].encode()).hexdigest()[:10]
            quote_value = source["text"][:220]
            if len(source["text"]) > 220:
                quote_value = quote_value.rsplit(" ", 1)[0]
            quote = st.text_area("Конкретная цитата — можно сократить, сохраняя дословность",
                                 value=quote_value, key=f"quote_{chosen}_{source_id}_{quote_seed}", height=100)
        else:
            st.info("В старом отчёте нет текстов постов. Загрузите их кнопкой выше; пока доступно только уточнение роли по названию канала.")
            source_id, quote = None, None
        suggestions = suggest_scenarios(editor_row)
        if suggestions:
            st.caption("По тексту можно проверить сценарии: " + "; ".join(SCENARIOS[s["scenario_id"]]["title"] for s in suggestions))
        scenario_id = st.selectbox("Сценарий первого сообщения", list(SCENARIOS),
                                   index=list(SCENARIOS).index(direct_scenario(editor_row)["scenario_id"]),
                                   format_func=lambda value: SCENARIOS[value]["title"])
        scenario = SCENARIOS[scenario_id]
        with st.expander("Что нужно подтвердить для сценария", expanded=scenario_id != "limited_evidence"):
            st.write("\n".join("• " + item for item in scenario["required_evidence"]))
            st.write(scenario["observation_rule"])
            st.caption("Избегать: " + "; ".join(scenario["avoid"]))
        confirmed = st.checkbox("Проверил требования сценария по источнику, включая авторство",
                                key=f"confirmed_{chosen}_{scenario_id}_{source_id}")
        hypothesis_index = st.selectbox("Идея сотрудничества", list(range(len(scenario["partnership_hypotheses"]))),
                                        format_func=lambda value: scenario["partnership_hypotheses"][value])
        tone = ui.selectbox("Тон сообщения", ["Деловой", "Неформальный"])
        st.caption('Профиль отправителя: ' + (sender_profile['name'] or 'имя не задано') + ' · ' + (sender_profile['role'] or 'роль не задана'))
        st.caption(f"Контакт: {row.get('author_contact') or 'не найден'} · {row.get('contact_status', '')}. Наличие продюсера не установлено. Проверьте, кому принадлежат внешние ссылки.")
        context_hash = hashlib.sha256(json.dumps([editor_row.get("about_text", ""), editor_row.get("content_samples", []),
                                                  editor_row.get("pitch", "") if editor_row.get("pitch_edited") else ""], ensure_ascii=False).encode()).hexdigest()[:10]
        profile_hash = profile_version(sender_profile) if not editor_row.get('pitch_edited') else 'manual'
        draft_key = f"draft_{state.get('run_id')}_{chosen}_{PLAYBOOK_VERSION}_{context_hash}_{profile_hash}"
        if draft_key not in st.session_state:
            initial_draft = compose_pitch(editor_row, state.get("niche", ""), tone, profile=sender_profile)
            st.session_state[draft_key] = editor_row.get("pitch", "") if editor_row.get("pitch_edited") else initial_draft["message"]
            st.session_state[draft_key + "_meta"] = {"scenario_id": editor_row.get("pitch_scenario") if editor_row.get("pitch_edited") else initial_draft["scenario_id"],
                                                      "source_id": editor_row.get("pitch_source") if editor_row.get("pitch_edited") else (initial_draft["source"] or {}).get("id")}
        if st.button("Собрать черновик по выбранному источнику"):
            try:
                result = compose_pitch(editor_row, state.get("niche", ""), tone,
                                       scenario_id=scenario_id, evidence_id=source_id, quote=quote,
                                       confirmed=confirmed, hypothesis_index=hypothesis_index, profile=sender_profile)
                st.session_state[draft_key] = result["message"]
                st.session_state[draft_key + "_meta"] = {"scenario_id": result["scenario_id"],
                                                          "source_id": (result["source"] or {}).get("id")}
                for warning in result["warnings"]:
                    st.warning(warning)
            except ValueError as exc:
                st.error(str(exc))
        draft = st.text_area("Черновик — можно отредактировать", key=draft_key, height=230)
        st.caption(f"{len(draft)} символов. После изменения источника, имени или сценария нажмите «Собрать черновик».")
        if st.button("Сохранить текст в отчёт", disabled=running):
            try:
                metadata = st.session_state[draft_key + "_meta"]
                runner.save_pitch(chosen, draft, metadata["scenario_id"], metadata["source_id"], st.session_state.get(context_key))
                st.success("Текст сохранён в текущий отчёт и checkpoint. Обновлены полные CSV и Excel.")
            except (ValueError, RuntimeError, OSError) as exc:
                st.error(str(exc))
        st.download_button("Скачать сообщение .txt", draft, f"pitch_{chosen.lstrip('@')}.txt", mime="text/plain")
        st.download_button("Скачать промпт для ChatGPT / Claude", chatgpt_prompt(editor_row, state.get("niche", ""), scenario_id=scenario_id, profile=sender_profile),
                           f"prompt_{chosen.lstrip('@')}.txt", mime="text/plain")
        with st.expander("Учебные примеры сценария"):
            st.caption("Примеры вымышлены. Их факты не используются как данные выбранного канала.")
            for example in scenario.get("examples", []):
                st.json(example["input_evidence"])
                st.write(example["message"])
                st.caption(example["reasoning"])
        st.caption("Отправка выполняется вручную после проверки контакта и текста.")
    if state["paths"]:
        with st.expander("Автоматически сохранённые файлы"):
            for path in state["paths"]:
                st.code(path, language=None)


if section == 'Поиск':
    results_panel()
elif section == 'Профили TGStat':
    profile_manager(runner, ui)
elif section == 'Dev-диагностика':
    from monetizer.ui_diagnostics import diagnostics_panel
    diagnostics_panel(runner)
else:
    render_workspace(runner, section)
if dev_mode:
    st.caption(f'Отрисовка страницы: {time.perf_counter() - PAGE_STARTED:.3f} с')
    logger().debug('UI section=%s render=%.3fs', section, time.perf_counter() - PAGE_STARTED)
