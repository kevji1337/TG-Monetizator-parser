"""Local Streamlit UI; worker threads never call Streamlit APIs."""
import re
import hashlib
import json

import pandas as pd
import streamlit as st

from monetizer.export import COLUMNS, csv_bytes, xlsx_bytes
from monetizer.audit import WebAuditor
from monetizer.keywords import COURSE_NICHES, expand_keywords, unique_queries, get_micro_niches, resolve_micro_niches, MICRO_NICHES, CATALOG_VERSION
from monetizer.pitches import (generate_pitch, compose_pitch, evidence_sources, suggest_scenarios,
                              chatgpt_prompt, SCENARIOS, PLAYBOOK_VERSION)
from monetizer.pitches import direct_scenario
from monetizer.runner import JobRunner, sorted_rows
from monetizer.settings import Settings
from monetizer.ui_workspace import render_workspace, show_evidence
from monetizer.ui_quality import profile_panel
from monetizer.profile import profile_version

st.set_page_config(page_title="Монетизатор · Telegram", page_icon="🔎", layout="wide")


@st.cache_resource
def get_runner():
    return JobRunner()


@st.cache_data(show_spinner=False, max_entries=8)
def downloads(rows):
    return csv_bytes(rows), xlsx_bytes(rows)


runner = get_runner()
sender_profile = profile_panel(runner.repo)
st.title("Монетизатор трафика")
st.caption("Поиск публичных Telegram-каналов экспертов · аудит просмотров · черновики предложений")

with st.sidebar:
    st.header("Новый поиск")
    mode = st.radio("Источник", ["TGStat — поиск по нише", "Список каналов — только аудит"])
    ids = [n["id"] for n in COURSE_NICHES] + ["custom"]
    labels = {n["id"]: n["title"] for n in COURSE_NICHES} | {"custom": "Своя тема"}
    niche_id = st.selectbox("Ниша", ids, format_func=lambda value: labels[value])
    topic = st.text_input("Ваша тема", placeholder="Например: дизайн презентаций") if niche_id == "custom" else labels[niche_id]
    micros = get_micro_niches(niche_id) if niche_id != "custom" else get_micro_niches()
    micro_id = st.selectbox("Узкое направление", [""] + [m["id"] for m in micros],
                            format_func=lambda value: MICRO_NICHES[value]["title"] if value else
                            ("Автоматически по вашей теме" if niche_id == "custom" else "Все направления ниши"))
    key_count = st.slider("Максимум поисковых запросов", 3, 40, 12)
    seed = (niche_id, topic, key_count, micro_id, CATALOG_VERSION)
    if st.session_state.get("keyword_seed") != seed:
        st.session_state["keyword_seed"] = seed
        st.session_state["keyword_text"] = "\n".join(expand_keywords(topic, niche_id, key_count, micro_id))
    if mode.startswith("TGStat"):
        st.text_area("Запросы — можно изменить", key="keyword_text", height=200)
        channels_text = ""
        matched = [MICRO_NICHES[micro_id]] if micro_id else (resolve_micro_niches(topic) if niche_id == "custom" else micros)
        total_queries = sum(len(m['discovery_queries']) for m in MICRO_NICHES.values())
        st.caption(f"Каталог: {len(MICRO_NICHES)} направлений и {total_queries} запросов. Выбрано запросов: {len(st.session_state['keyword_text'].splitlines())}.")
        if matched:
            st.caption("Семантика: " + "; ".join(m["title"] for m in matched))
            with st.expander("Границы темы и признаки эксперта"):
                for micro in matched:
                    st.markdown(f"**{micro['title']}**")
                    st.write(micro["disambiguation"])
                    st.write("Ищем: " + "; ".join(micro["expert_signals"]))
                    st.write("Проверяем вручную: " + "; ".join(micro["non_target_signals"]))
        elif topic:
            st.info("Для этой темы нет точного направления в каталоге. Оставлен исходный запрос: добавьте свои ключи или выберите направление вручную.")
    else:
        channels_text = st.text_area("@username или ссылки t.me — по одному на строку", height=150)
    count = st.number_input("Цель: подходящих каналов", 1, 1000, 30, step=1)
    new_only = st.checkbox("Искать только новые каналы", value=True, disabled=not mode.startswith("TGStat"))
    login_pause = st.checkbox("Дать минуту на ручной вход в TGStat", disabled=not mode.startswith("TGStat"))
    with st.expander("Критерии и скорость"):
        min_subs = st.number_input("Подписчики от", 1, 100_000_000, 3000)
        max_subs = st.number_input("Подписчики до", 1, 100_000_000, 30000)
        daily = st.number_input("Суточный охват TGStat от", 0, 10_000_000, 300)
        views = st.number_input("Средние просмотры поста от", 0, 10_000_000, 300)
        mature = st.number_input("Возраст поста для просмотров, часов", 0, 168, 24)
        gap = st.number_input("Максимальный интервал публикаций, дней", 1.0, 14.0, 4.0, step=.5)
        workers = st.slider("Параллельных аудитов", 1, 8, 4)
        rate = st.slider("Запросов к Telegram в секунду", .5, 5.0, 2.0, step=.5)
        audit_budget = st.number_input("Максимум аудитов: 0 = десять целей", 0, 10000, 0, step=10)
        time_limit = st.number_input("Лимит времени, минут", 1.0, 1440.0, 60.0)
    if st.button("Начать поиск и аудит" if mode.startswith("TGStat") else "Начать аудит", type="primary", width="stretch"):
        try:
            settings = Settings(min_subs=min_subs, max_subs=max_subs, min_daily_reach=daily,
                                min_avg_views=views, target_count=count, mature_hours=mature,
                                max_gap_days=gap, workers=workers, requests_per_second=rate,
                                login_wait_seconds=60 if login_pause else 0, max_audits=audit_budget,
                                max_minutes=time_limit, new_only=new_only, niche_id=niche_id if niche_id != "custom" else "", micro_id=micro_id)
            usernames = re.split(r"[\s,;]+", channels_text.strip()) if channels_text.strip() else []
            if not mode.startswith("TGStat") and not usernames:
                raise ValueError("Добавьте хотя бы один канал")
            run_topic = MICRO_NICHES[micro_id]["title"] if micro_id else topic
            runner.start(settings, unique_queries(st.session_state["keyword_text"].splitlines()), run_topic, usernames)
            st.rerun()
        except (ValueError, RuntimeError, OSError) as exc:
            st.error(str(exc))
    st.caption("TGStat открывается в видимом Chrome. При запросе проверки выполните её в этом окне.")


@st.fragment(run_every="2s")
def results_panel():
    state = runner.snapshot()
    running = state["status"] in ("running", "stopping")
    count_found, rows = len(state["candidates"]), sorted_rows(state["rows"])
    green = sum(r.get("status") in ("top", "recommended") for r in rows)
    metrics = st.columns(4)
    metrics[0].metric("Найдено", count_found)
    metrics[1].metric("Проверено", len(rows))
    metrics[2].metric("Подходящие / цель", f"{green} / {state.get('settings', {}).get('target_count', 30)}")
    metrics[3].metric("Ошибки доступа", sum(r.get("status") == "error" for r in rows))
    st.write(f"**{state['stage']}**")
    if state.get("niche"):
        st.caption(f"Текущий отчёт: {state['niche']}")
    if running:
        st.progress(min(green / state["settings"]["target_count"], 1.0),
                    text=f"Подходят {green}; проверено {len(rows)}; найдено {count_found}")
        if state.get("external_owner"):
            st.info("Поиск выполняется в другом процессе. Его прогресс показан из базы; остановите его в окне запуска.")
        if st.button("Остановить и сохранить прогресс", disabled=state["status"] == "stopping" or state.get("external_owner", False)):
            runner.stop()
            st.rerun()
    elif state["status"] in ("cancelled", "interrupted", "failed", "partial"):
        if st.button("Продолжить сохранённый запуск"):
            try:
                resume_config = {**state["settings"], "max_audits": audit_budget, "max_minutes": time_limit,
                                 "login_wait_seconds": 60 if login_pause else 0}
                runner.start(settings=Settings(**resume_config), resume=True)
                st.rerun()
            except (ValueError, RuntimeError, OSError) as exc:
                st.error(str(exc))
    if state.get("error"):
        st.error(state["error"])
    with st.expander("Ход работы", expanded=running):
        st.code("\n".join(state["logs"][-35:]) or "Выберите нишу и запустите поиск слева.", language=None)
    if state.get("query_stats"):
        with st.expander("Эффективность поисковых запросов"):
            st.dataframe(pd.DataFrame([{"Запрос": q, **stats} for q, stats in state["query_stats"].items()]),
                         hide_index=True, width="stretch")
            history = runner.repo.query_history(state.get("query_scope", ""), state.get("run_id"))
            if history:
                st.caption("Предыдущие запуски с теми же направлением, фильтрами и версией правил")
                st.dataframe(pd.DataFrame([{"Запрос": q, **stats} for q, stats in history.items()]), hide_index=True, width="stretch")
    if not rows:
        st.info("Результаты появятся по мере аудита. Можно также проверить свой список публичных каналов.")
        return
    st.caption("Зелёная метка: метрики + признаки личной практики минимум в двух источниках + соответствие теме. Это предварительный отбор. Суточный охват проверяется фильтром TGStat; здесь показываются просмотры постов. Низкое отношение просмотров к подписчикам не доказывает накрутку.")
    filter_columns = st.columns([1, 1, 2])
    only_green = filter_columns[0].checkbox("Только зелёные")
    only_contact = filter_columns[1].checkbox("С кандидатом контакта")
    search = filter_columns[2].text_input("Поиск в результатах", placeholder="Название или @username")
    selected = [r for r in rows if (not only_green or r.get("status") in ("top", "recommended"))
                and (not only_contact or r.get("author_contact"))
                and (not search or search.casefold() in f"{r.get('title', '')} {r['username']}".casefold())]
    st.caption(f"Показано {len(selected)} из {len(rows)}. Скачивание учитывает фильтры.")
    visible = ["verdict", "title", "username", "subs", "avg_views", "median_views", "view_ratio", "freq",
               "last_post", "youtube", "instagram", "author_contact", "reasons", "link"]
    frame = pd.DataFrame([{COLUMNS[k]: r.get(k) for k in visible} for r in selected])
    st.dataframe(frame, hide_index=True, width="stretch",
                 column_config={COLUMNS[k]: st.column_config.LinkColumn(COLUMNS[k]) for k in ("link", "youtube", "instagram")})
    if selected:
        csv_data, xlsx_data = downloads(selected)
        filename = f"Монетизация_{state.get('run_id', 'каналы')[:12]}"
        c1, c2 = st.columns(2)
        c1.download_button("Скачать Excel", xlsx_data, filename + ".xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        c2.download_button("Скачать CSV", csv_data, filename + ".csv", mime="text/csv")
        st.subheader("Сообщение автору")
        chosen = st.selectbox("Канал для черновика", [r["username"] for r in selected])
        row = next(r for r in selected if r["username"] == chosen)
        show_evidence(row)
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
        tone = st.selectbox("Тон сообщения", ["Деловой", "Неформальный"])
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


results_panel()
render_workspace(runner)
