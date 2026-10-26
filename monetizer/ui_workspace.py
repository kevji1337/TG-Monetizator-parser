"""Database, history and offline batch controls. Never starts a server or browser."""
import json

import streamlit as st

from .batch import create_batch, batch_zip, batch_prompt, preview_response, import_response
from .export import csv_bytes, xlsx_bytes
from .repository import WORK_STATUSES
from .qualification import TYPE_LABELS, CONTENT_LABELS, RULE_VERSION
from .ai_audit import STATUS_LABELS
from .ui_quality import quality_panel
from .preferences import Preferences, PersistentUI


def assessment_stale(row):
    return bool(row.get('assessment_stale') or row.get('classification', {}).get('rule_version') != RULE_VERSION)


def show_evidence(row):
    if assessment_stale(row):
        st.warning('Автоматическая оценка устарела. Откройте нужный запуск из истории и пересчитайте сохранённые оценки в разделе «Поиск».')
    st.caption("Тип: " + TYPE_LABELS.get(row.get("channel_type"), "Не установлено") + " · Тематика: " + row.get("relevance", "unknown"))
    if row.get('ai_audit_status'):
        st.caption('AI: ' + STATUS_LABELS.get(row['ai_audit_status'], row['ai_audit_status']) + ' · ' + row.get('ai_detail', ''))
    api_review = row.get('api_ai_review')
    if api_review:
        with st.expander('Анализ Gemini через API', expanded=True):
            st.text(api_review['reason'])
            st.caption('Запрошенная модель: ' + api_review.get('requested_model', api_review.get('model', '')) +
                       ' · исходный вердикт правил: ' + row.get('local_verdict', ''))
            st.caption('Фактическая модель: ' + (api_review.get('actual_model') or 'не сообщается Chat Completions; смотрите Requests в web2api'))
            if api_review.get('proxy_version'):
                st.caption(f"web2api {api_review['proxy_version']} · ответ за {api_review.get('elapsed_seconds', '?')} с · "
                           f"текст запроса: {api_review.get('prompt_bytes', '?')} байт UTF-8")
            if api_review.get('sources_truncated'):
                st.caption('В AI переданы сокращённые начала длинных публикаций; полные загруженные тексты сохранены в базе.')
            if api_review.get('usage'):
                st.caption('Оценка токенов web2api: ' + json.dumps(api_review['usage'], ensure_ascii=False))
            for proof in api_review['evidence']:
                st.text(proof['quote'])
                st.caption(proof.get('date', '') + ' · ' + proof['url'])
            if api_review['offers']:
                st.json(api_review['offers'])
            if api_review.get('message'):
                st.text_area('Черновик Gemini — отдельно от сохранённого сообщения', api_review['message'], disabled=True)
                st.download_button('Скачать черновик Gemini', api_review['message'], f"gemini_{row['username'].lstrip('@')}.txt", mime='text/plain')
    st.link_button("Посмотреть публичную ленту Telegram", 'https://t.me/s/' + row['username'].lstrip('@'))
    classification = row.get('classification', {})
    summary = classification.get('content_summary')
    if summary:
        st.caption(f"Текстовых постов: {summary['text_posts']} · независимых без пересылок: {summary['independent_own_posts']} · "
                   f"с признаками авторской практики / разбора: {summary['expert_evidence_posts']} · "
                   f"пересылок: {summary['forwarded_posts']}")
    with st.expander('Разбор публикаций Telegram'):
        st.caption('Признаки определены локальными правилами по тексту. Отсутствие метки пересылки не подтверждает авторство. Фото, аудио и видео не распознаются.')
        for item in classification.get('post_roles', []):
            st.write(CONTENT_LABELS.get(item['role'], item['role']))
            st.text(item['quote'])
            if item.get('url'):
                st.link_button('Публикация ' + item['source_id'], item['url'])
        if not classification.get('post_roles'):
            st.info('В этом отчёте ещё нет разбора публикаций. Обновите тексты канала или откройте запуск для пересчёта.')
    with st.expander("Основания отбора и цитаты"):
        st.json(classification or {"comment": "Старый отчёт без оценки содержания"})
    with st.expander("Предложения автора"):
        offers = row.get("offers", [])
        if not offers:
            st.write("Не найдено в просмотренной выборке")
        for offer in offers:
            st.markdown(f"**{offer['kind']} · {offer['ownership']}**")
            st.text(offer["quote"])
            st.caption(f"{offer.get('date', '')} · {offer.get('url', '')}")
            if offer.get("prices"):
                st.write("Явная цена: " + "; ".join(offer["prices"]))
            if offer.get("booking_methods"):
                st.write("Запись: " + "; ".join(offer["booking_methods"]))


def database_panel(runner, running):
    import pandas as pd
    ui = PersistentUI(Preferences(runner.repo))
    rows = runner.repo.list_channels()
    if not rows:
        st.info("База пока пуста")
        return
    status_filter = ui.multiselect("Рабочие статусы", list(WORK_STATUSES), format_func=WORK_STATUSES.get)
    search = ui.text_input("Поиск в базе", key="database_search")
    hide_non_target = ui.checkbox('Скрыть нецелевые типы каналов', value=True, key='database_hide_non_target')
    visible = [r for r in rows if (not status_filter or r["work_status"] in status_filter)
               and (not hide_non_target or (r.get('channel_type') or 'unknown') in ('expert', 'unknown'))
               and (not search or search.casefold() in (r.get("title", "") + r["username"]).casefold())]
    st.dataframe(pd.DataFrame([{"Канал": r["username"], "Название": r.get("title"),
                               "Отбор": '⏳ Оценка устарела' if assessment_stale(r) else r.get("verdict"),
                               "Тип": TYPE_LABELS.get(r.get('channel_type'), 'Не установлено'),
                               "Работа": WORK_STATUSES[r["work_status"]], "Заметка": r["notes"]} for r in visible]), hide_index=True, width="stretch")
    if not visible:
        return
    username = st.selectbox("Карточка кандидата", [r["username"] for r in visible])
    row = next(r for r in visible if r["username"] == username)
    show_evidence(row)
    status = st.selectbox("Рабочий статус", list(WORK_STATUSES), index=list(WORK_STATUSES).index(row["work_status"]),
                          format_func=WORK_STATUSES.get, key=f"crm_status_{username}")
    notes = st.text_area("Заметка о кандидате", row["notes"], key=f"crm_notes_{username}")
    if st.button("Сохранить статус и заметку", disabled=running):
        runner.repo.save_crm(username, status, notes)
        st.success("Рабочий статус и заметка сохранены")
    pending_key = f"pending_ai_draft_{username}"
    if pending_key in st.session_state:
        st.session_state[f"database_pitch_{username}"] = st.session_state.pop(pending_key)
    message = st.text_area("Сохранённое сообщение", row.get("pitch", ""), key=f"database_pitch_{username}")
    if st.button("Сохранить сообщение в базе", disabled=running):
        runner.repo.save_draft(username, {"pitch": message, "pitch_edited": True,
                                          "pitch_scenario": row.get("pitch_scenario", ""), "pitch_source": row.get("pitch_source", "")})
        st.success("Ручной текст сохранён")
    review = row.get("ai_review")
    if review:
        with st.expander("Оценка ChatGPT — отдельно от автоматического отбора", expanded=True):
            st.json(review)
            if review.get("stale"):
                st.warning("Данные или направление отбора изменились после AI-разбора; сформируйте новый пакет")
            else:
                if st.button("Принять рекомендацию AI как рабочий статус", disabled=running):
                    accepted = review["channel_type"] == "expert" and review["relevance"] == "matched"
                    decision = "qualified" if accepted else "new" if review["channel_type"] == "unknown" or review["relevance"] == "unknown" else "unsuitable"
                    runner.repo.save_crm(username, decision, row["notes"])
                    st.success("Рабочий статус обновлён вручную; автоматический вердикт сохранён")
                if st.button("Принять сообщение AI вместо сохранённого", disabled=running or not review.get("message")):
                    runner.repo.save_draft(username, {"pitch": review["message"], "pitch_edited": True,
                                                       "pitch_scenario": "ai_review", "pitch_source": review["evidence"][0]["source_id"]})
                    st.session_state[pending_key] = review["message"]
                    st.rerun()
    with st.expander("История работы с кандидатом"):
        for event in runner.repo.history(username):
            st.caption(event["created_at"] + " · " + event["kind"])
            st.json(json.loads(event["data"]))


def history_panel(runner, running):
    import pandas as pd
    runs = runner.repo.list_runs()
    if not runs:
        st.info("История запусков пуста")
        return
    labels = {r["run_id"]: f"{r['started_at']} · {r['niche']} · {r['status']}" for r in runs}
    run_id = st.selectbox("Предыдущий запуск", list(labels), format_func=labels.get)
    state = runner.repo.load_run(run_id)
    st.caption(f"Каналов: {len(state['rows'])}; причина завершения: {state.get('stop_reason', state.get('stage', ''))}")
    st.dataframe(pd.DataFrame([{"Канал": r["username"], "Название": r.get("title"), "Вердикт": r.get("verdict")} for r in state["rows"]]), hide_index=True, width="stretch")
    c1, c2 = st.columns(2)
    c1.download_button("CSV предыдущего запуска", csv_bytes(state["rows"]), f"history_{run_id}.csv", mime="text/csv")
    c2.download_button("Excel предыдущего запуска", xlsx_bytes(state["rows"]), f"history_{run_id}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    if st.button("Открыть этот запуск в текущей вкладке", disabled=running):
        runner.load_saved_run(run_id)
        st.rerun()


def batch_panel(runner, running):
    import pandas as pd
    state = runner.snapshot()
    if state.get("run_id") and state["rows"]:
        size = st.selectbox("Каналов в пакете", [10, 20])
        choices = [r["username"] for r in state["rows"]]
        usernames = st.multiselect("Каналы для пакетного разбора", choices, default=choices[:size])
        if st.button("Создать пакет ChatGPT", disabled=running or not usernames):
            try:
                st.session_state["chat_batch"] = create_batch(runner.repo, state["run_id"], usernames, size)
            except ValueError as exc:
                st.error(str(exc))
        packet = st.session_state.get("chat_batch")
        if packet:
            st.caption(f"Пакет {packet['batch_id']}: {len(packet['channels'])} каналов. Распакуйте ZIP и приложите request.json и PROMPT.txt к ChatGPT.")
            st.download_button("Скачать ZIP с данными, промптом и форматом ответа", batch_zip(packet), f"chatgpt_{packet['batch_id']}.zip", mime="application/zip")
            st.download_button("Скачать данные JSON отдельно", json.dumps(packet, ensure_ascii=False, indent=2), "request.json", mime="application/json")
            st.download_button("Скачать промпт отдельно", batch_prompt(packet), "PROMPT.txt", mime="text/plain")
    upload = st.file_uploader("Ответ ChatGPT в JSON", type=["json"], key="batch_response")
    if upload:
        payload = upload.getvalue()
        try:
            preview = preview_response(runner.repo, payload)
            st.caption(f"Предварительный просмотр: {len(preview['reviews'])} каналов. Тексты и рабочие статусы не заменяются автоматически.")
            st.dataframe(pd.DataFrame([{"Канал": r["channel_id"], "Тип": r["channel_type"], "Тематика": r["relevance"],
                                       "Сообщение": r["message"]} for r in preview["reviews"]]), hide_index=True, width="stretch")
            if st.button("Импортировать проверенный ответ", disabled=running):
                result = import_response(runner.repo, payload)
                st.success("Этот ответ уже импортирован" if result["already_imported"] else f"Импортировано оценок: {result['count']}")
        except (ValueError, TypeError, KeyError) as exc:
            st.error(f"Импорт отклонён без изменения базы: {exc}")


def render_workspace(runner, section):
    running = runner.snapshot()["status"] in ("running", "stopping")
    panels = {'База кандидатов': database_panel, 'История запусков': history_panel,
              'Пакет ChatGPT': batch_panel, 'Качество отбора': lambda r, active: quality_panel(r.repo, active)}
    panels[section](runner, running)
