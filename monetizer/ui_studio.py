"""Operator tools. Network work starts only on explicit audit/experiment buttons."""
import json
import re
from dataclasses import replace
from datetime import date

import streamlit as st

from .ui_tables import data_table

from .audit import normalize_username
from .categories import load_categories, category_label
from .preferences import widget_key, last_run_preferences
from .profile import load_profile
from .repository import WORK_STATUSES
from .settings import Settings
from .workspace import Workspace, DEAL_STAGES, economics, distribution, topic_stability, search_eta, funnel, reminder_calendar
from .ui_power import queue_apply


def candidate_editor(repo, row, running=False):
    store = Workspace(repo)
    user = normalize_username(row['username'])
    data = store.get('candidate', user)
    with st.expander('Теги, подборки и экономика'):
        with st.form('business_' + user):
            tags = st.text_input('Теги через запятую', ', '.join(data.get('tags', [])))
            collections = st.text_input('Подборки через запятую', ', '.join(data.get('collections', [])))
            stage = st.selectbox('Этап партнёрства', list(DEAL_STAGES),
                                 index=list(DEAL_STAGES).index(data.get('deal_stage', 'none')), format_func=DEAL_STAGES.get)
            current = data.get('economics', {})
            currency = st.selectbox('Валюта', ['RUB', 'BYN', 'USD', 'EUR'],
                                    index=['RUB', 'BYN', 'USD', 'EUR'].index(current.get('currency', 'RUB')))
            numbers = {}
            for key, label in [('price', 'Предполагаемый средний чек'), ('leads', 'Ожидаемые лиды'),
                               ('conversion', 'Конверсия в оплату, %'), ('share', 'Ваша доля выручки, %'),
                               ('costs', 'Ваши расходы'), ('actual_revenue', 'Фактическая выручка — ввод вручную')]:
                numbers[key] = st.number_input(label, min_value=0., max_value=100. if key in ('conversion', 'share') else 1e12,
                                               value=float(current.get(key, 0.)))
            save = st.form_submit_button('Сохранить теги и экономику', disabled=running)
        if save:
            try:
                store.save('candidate', user, {'tags': tags, 'collections': collections, 'deal_stage': stage,
                                               'economics': {**numbers, 'currency': currency}})
                st.success('Сохранено; публичные аудиты эти данные не заменяют.')
            except ValueError as exc:
                st.error(str(exc))
        model = economics({**numbers, 'currency': currency})
        st.caption(f"Сценарий: {model['sales_forecast']:g} оплат · выручка {model['gross_forecast']:,.2f} {currency} · "
                   f"ваша доля минус расходы {model['partner_forecast']:,.2f} {currency}. Это расчёт по введённым предположениям.")


def search_tools(runner, busy):
    from .preferences import Preferences
    import pandas as pd
    store = Workspace(runner.repo)
    state = runner.snapshot()
    if st.session_state.pop('_next_search_saved', False):
        st.success('Следующий поиск подготовлен. Автоматического старта нет.')
    estimate = search_eta(state)
    st.info(estimate['text'] + (f" · примерно {estimate['minutes']} мин." if estimate['minutes'] is not None else ''))
    plan = dict(state.get('settings', {}).get('category_weights', ()))
    st.write('**План текущих категорий**')
    data_table(pd.DataFrame([{'Категория': category_label(q), 'Вес': plan.get(q.split(':')[-1], 1),
                               'Найдено': stats.get('found', 0), 'Проверено': stats.get('audited', 0),
                               'Подходит': stats.get('qualified', 0)} for q, stats in state.get('query_stats', {}).items()]), hide_index=True)
    failures = [r['username'] for r in state.get('rows', []) if r.get('metric_status', r.get('status')) == 'error']
    if st.button(f'Повторить только ошибки доступа ({len(failures)})', disabled=busy or not failures):
        settings = replace(Settings.from_saved(state['settings']), ai_enabled=False, new_only=False, force_reaudit=True, search_mode='manual')
        runner.start(settings, niche='Повтор ошибок доступа', usernames=failures)
        st.rerun()
    with st.expander('Личный список исключений'):
        value = st.text_area('Не искать эти каналы', '\n'.join('@' + u for u in store.get('blocklist', 'channels', [])),
                             key='blocklist_text', disabled=busy)
        if st.button('Сохранить исключения', disabled=busy):
            users = list(dict.fromkeys(normalize_username(u) for u in re.split(r'[\s,;]+', value.strip()) if u))
            store.save('blocklist', 'channels', users)
            st.success('Будут исключены в новых поисках. История и отчёты сохранены.')
    with st.expander('Подготовить следующий поиск', expanded=True):
        saved = store.get('draft', 'next_search')
        presets = store.list('preset')
        preset = st.selectbox('Основа следующего поиска', ['Текущие настройки'] + list(presets))
        labels = {c['id']: c['title'] for c in load_categories()['categories']}
        categories = st.multiselect('Следующие категории', list(labels), default=[c for c in saved.get('categories', []) if c in labels], format_func=labels.get)
        target = st.number_input('Цель следующего поиска', 1, 1000, saved.get('target', 30))
        if st.button('Сохранить следующий поиск', disabled=not categories):
            values = dict(Preferences(runner.repo).values if preset == 'Текущие настройки' else presets[preset])
            values = {k: v for k, v in values.items() if k != 'proxy_api_key'}
            values.update(tgstat_category_ids=categories)
            values[widget_key('number_input', 'Цель: подходящих каналов')] = target
            values[widget_key('radio', 'Источник')] = 'TGStat — каталог тематик'
            values[widget_key('radio', 'Раздел')] = 'Поиск'
            store.save('draft', 'next_search', {'preferences': values, 'categories': categories, 'target': target})
            st.session_state['_next_search_saved'] = True
            st.rerun()
        if saved and st.button('Подставить подготовленный поиск в панель', disabled=busy, on_click=queue_apply, args=(saved['preferences'],)):
            st.rerun()


def content_tools(runner):
    rows = runner.repo.list_channels()
    if not rows:
        st.info('Сначала соберите каналы.')
        return
    user = st.selectbox('Канал для анализа содержания', [r['username'] for r in rows])
    row = next(r for r in rows if r['username'] == user)
    spread, topics = distribution(row), topic_stability(row)
    st.subheader('Распределение просмотров')
    st.json({k: v for k, v in spread.items() if k != 'outliers'})
    for item in spread['outliers']:
        st.link_button(str(item['views']) + ' просмотров · ' + item['date'][:10], item['url'])
    st.caption('Выбросы не доказывают накрутку. Здесь нет повторного замера через сутки или расчёта доли рекламы.')
    st.subheader('Стабильность тематики')
    st.json({k: v for k, v in topics.items() if k != 'posts'})
    for item in topics['posts']:
        st.caption(item['date'][:10] + ' · ' + (', '.join(item['terms'][:8]) or 'Профильные термины не найдены'))
        st.link_button('Публикация', item['url'])
    st.download_button('Скачать анализ контента', json.dumps({'distribution': spread, 'topics': topics}, ensure_ascii=False, indent=2), 'content_analysis.json')


def calibration_tools(runner, busy):
    from .calibration import propose_threshold
    report = propose_threshold(runner.repo)
    if not report['ready']:
        st.info(report['reason'])
        return
    st.json(report)
    st.caption('Порог выбран на обучающей части; контрольная часть не использовалась при подборе.')
    if st.button('Сохранить предложение порога'):
        Workspace(runner.repo).save('calibration', 'latest', report)
    st.button('Применить порог к следующему поиску', disabled=busy, on_click=queue_apply,
              args=({widget_key('number_input', 'Просмотры поста от подписчиков, %'): float(report['threshold'])},))


@st.fragment(run_every='2s')
def ai_tools(runner, busy, api_key, config):
    from .experiments import preview_request, start_experiment, experiment_job, experiment_active
    from .ai_responses import latest_response
    from .ai_audit import STATUS_LABELS
    rows = runner.repo.list_channels()
    job = experiment_job(runner.repo)
    active = experiment_active(runner.repo)
    parser_busy = runner.snapshot()['status'] in ('running', 'stopping')
    if busy != (parser_busy or active):
        st.rerun()
    if job:
        if active:
            st.info('AI-эксперимент выполняется в фоне: ' + job['username'])
            if st.button('Остановить AI-эксперимент'):
                job['stop'].set()
                st.info('Отменяю локальное ожидание. Удалённая генерация на прокси может продолжиться.')
        else:
            try:
                st.success('Последний AI-эксперимент завершён')
                st.json(job['future'].result())
            except Exception as exc:
                st.error(str(exc))
    if not rows:
        st.info('Нет загруженных текстов для AI.')
        return
    user = st.selectbox('Канал для AI-лаборатории', [r['username'] for r in rows])
    row = next(r for r in rows if r['username'] == user)
    state = runner.snapshot()
    settings = replace(Settings.from_saved(state.get('settings', {})), **config)
    preview = preview_request(row, settings, state.get('niche', ''), load_profile(runner.repo))
    st.caption(f"Модель: {preview['model']} · источников: {preview['sources']} · UTF-8: {preview['bytes']:,} байт · "
               f"лимит: {settings.ai_prompt_bytes:,} · сокращение: {'да' if preview['truncated'] else 'нет'}")
    with st.expander('Что именно будет отправлено модели'):
        st.code(preview['prompt'], language=None)
        st.download_button('Скачать точный запрос AI', json.dumps(preview, ensure_ascii=False, indent=2), 'ai_request_preview.json')
    mode = st.radio('Эксперимент', ['Сравнить модели', 'Сравнить промпты', 'Исправить выбранные выводы'], horizontal=True)
    model_a = st.text_input('Модель A', preview['model'])
    model_b = st.text_input('Модель B', settings.ai_screen_model or settings.ai_model) if mode == 'Сравнить модели' else model_a
    first, second = 'baseline', 'strict'
    if mode == 'Сравнить промпты':
        first = st.selectbox('Промпт A', ['baseline', 'strict', 'concise'])
        second = st.selectbox('Промпт B', ['strict', 'baseline', 'concise'])
    fields = []
    if mode == 'Исправить выбранные выводы':
        fields = st.multiselect('Поля для исправления', ['evidence', 'offers', 'message', 'reason', 'channel_type', 'relevance'], default=['evidence'])
        previous = latest_response(runner.repo, user, row.get('ai_request_key'))
        st.caption('Остальные поля должны сохраниться дословно. Исправленный ответ проходит полную проверку цитат.')
        if previous:
            st.code(previous['validation_error'] or 'Последний ответ был проверен', language=None)
    st.caption('Эксперимент использует общий остаток лимита текущего запуска — максимум пять POST. '
               'Результаты сохраняются отдельно, основной вердикт и ручной текст не заменяются.')
    invalid = (mode == 'Сравнить модели' and model_a == model_b) or (mode == 'Сравнить промпты' and first == second)
    if invalid:
        st.info('Для сравнения выберите разные модели или разные промпты.')
    if st.button('Выполнить AI-эксперимент', disabled=parser_busy or active or not api_key or invalid or not preview['within_limit']):
        kind = 'models' if mode == 'Сравнить модели' else 'prompts' if mode == 'Сравнить промпты' else 'repair'
        models = [model_a] if kind == 'repair' else [model_a, model_b]
        start_experiment(runner, row, api_key, kind, models, [first, second] if kind == 'prompts' else None, fields, config)
        st.rerun()
    reports = Workspace(runner.repo).list('experiment')
    for key, report in list(reports.items())[-15:][::-1]:
        with st.expander(report['username'] + ' · ' + report['kind'] + ' · ' + key[:8]):
            for result in report['results']:
                st.write(result['model'] + ' / ' + result['variant'] + ': ' + STATUS_LABELS.get(result['status'], result['status']))
                st.json(result.get('review') or {'detail': result.get('detail')})
            st.download_button('Скачать эксперимент', json.dumps(report, ensure_ascii=False, indent=2), 'experiment_' + key[:8] + '.json', key='download_' + key)


def tag_tools(runner, busy):
    import pandas as pd
    store = Workspace(runner.repo)
    attrs = store.list('candidate')
    tags = sorted({t for a in attrs.values() for t in a.get('tags', [])})
    collections = sorted({t for a in attrs.values() for t in a.get('collections', [])})
    chosen_tags = st.multiselect('Фильтр тегов', tags)
    chosen_collections = st.multiselect('Фильтр подборок', collections)
    rows = [r for r in runner.repo.list_channels() if
            (not chosen_tags or set(chosen_tags) <= set(attrs.get(normalize_username(r['username']), {}).get('tags', []))) and
            (not chosen_collections or set(chosen_collections) & set(attrs.get(normalize_username(r['username']), {}).get('collections', [])))]
    data_table(pd.DataFrame([{'Канал': r['username'], 'Название': r.get('title'),
                               'Теги': ', '.join(attrs.get(normalize_username(r['username']), {}).get('tags', [])),
                               'Подборки': ', '.join(attrs.get(normalize_username(r['username']), {}).get('collections', []))} for r in rows]), hide_index=True)
    if rows:
        user = st.selectbox('Канал для тегов', [r['username'] for r in rows])
        candidate_editor(runner.repo, next(r for r in rows if r['username'] == user), busy)


def kanban(runner, busy):
    st.subheader('Канбан кандидатов')
    rows = runner.repo.list_channels()
    columns = st.columns(len(WORK_STATUSES))
    for area, (status, label) in zip(columns, WORK_STATUSES.items()):
        group = [r for r in rows if r['work_status'] == status]
        with area:
            st.markdown('**' + label + f' · {len(group)}**')
            for row in group:
                with st.container(border=True):
                    st.write(row.get('title') or row['username'])
                    st.caption(row['username'])
                    value = st.selectbox('Переместить', list(WORK_STATUSES), index=list(WORK_STATUSES).index(status),
                                         format_func=WORK_STATUSES.get, key='kanban_' + row['username'], label_visibility='collapsed', disabled=busy)
                    if st.button('Переместить', key='move_' + row['username'], disabled=busy or value == status):
                        runner.repo.save_crm(row['username'], value, row.get('notes', ''))
                        st.rerun()


def business_analytics(runner, busy):
    import pandas as pd
    store = Workspace(runner.repo)
    st.subheader('Ответы и партнёрства')
    st.json(funnel(runner.repo, store))
    totals = {}
    for item in store.list('candidate').values():
        values = economics(item.get('economics', {}))
        group = totals.setdefault(values['currency'], {'Валюта': values['currency'], 'Прогноз выручки': 0., 'Прогноз вашей доли минус расходы': 0., 'Фактическая выручка, введено': 0.})
        group['Прогноз выручки'] += values['gross_forecast']
        group['Прогноз вашей доли минус расходы'] += values['partner_forecast']
        group['Фактическая выручка, введено'] += values['actual_revenue']
    data_table(pd.DataFrame(totals.values()), hide_index=True)
    st.caption('Валюты не суммируются между собой. Прогноз и вручную введённая выручка показаны отдельно.')
    rows = runner.repo.list_channels()
    planned = [r for r in rows if r.get('next_contact_date')]
    due = [r for r in planned if r['next_contact_date'] <= date.today().isoformat()]
    if due:
        st.warning(f'Контактов на сегодня или просрочено: {len(due)}')
        for row in due:
            st.write(row['username'] + ' · ' + row['next_contact_date'] + ' · ' + row.get('next_action_label', ''))
    minutes = st.number_input('Напомнить за минут до контакта', 1, 10080, 60)
    st.download_button('Скачать напоминания в календарь .ics', reminder_calendar(planned, minutes), 'contacts.ics', mime='text/calendar')
    st.caption('В календаре контакт назначен на 09:00 по местному времени. Импортируйте ICS в свой календарь для уведомлений при закрытой панели; внутри панели напоминания видны при её открытии.')
    if rows:
        user = st.selectbox('Партнёрство для расчёта', [r['username'] for r in rows])
        candidate_editor(runner.repo, next(r for r in rows if r['username'] == user), busy)


def render_studio(runner, section, api_key='', config=None):
    from .experiments import experiment_active
    busy = runner.snapshot()['status'] in ('running', 'stopping') or experiment_active(runner.repo)
    try:
        if section == 'Канбан':
            kanban(runner, busy)
        elif section == 'Аналитика партнёрств':
            business_analytics(runner, busy)
        else:
            st.subheader('Инструменты')
            selected = st.radio('Инструмент', ['Поиск и очередь', 'Анализ контента', 'Калибровка порога', 'AI-лаборатория', 'Теги и подборки'], horizontal=True)
            if selected == 'Поиск и очередь':
                search_tools(runner, busy)
            elif selected == 'Анализ контента':
                content_tools(runner)
            elif selected == 'Калибровка порога':
                calibration_tools(runner, busy)
            elif selected == 'AI-лаборатория':
                ai_tools(runner, busy, api_key, config or {})
            else:
                tag_tools(runner, busy)
    except (ValueError, RuntimeError, KeyError, TypeError) as exc:
        st.error(str(exc))
