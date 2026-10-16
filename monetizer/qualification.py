"""Explainable preliminary qualification. Text claims are not verified identity."""
import copy
import re
from functools import lru_cache

from .keywords import MICRO_NICHES, get_micro_niches, resolve_micro_niches, _tokens, _same_word, _stem
from .pitches import evidence_sources
from .categories import assessment_profiles

RULE_VERSION = "qualification-4"
CHANNEL_TYPES = {"expert", "school", "brand", "news", "job_board", "aggregator", "store", "unknown"}
TYPE_LABELS = {"expert": "Личный эксперт", "school": "Школа", "brand": "Бренд", "news": "Новости",
               "job_board": "Вакансии", "aggregator": "Агрегатор", "store": "Магазин", "unknown": "Не установлено"}
NEGATIVE = {
    "job_board": r"ваканси|ищем\s+(?:монтаж|дизайнер|специалист)|требуется\s+|резюме\s+кандидат",
    "store": r"магазин|купить\s+шаблон|каталог\s+товар|скидк.{0,25}заказ|доставка\s+товар",
    "school": r"наша\s+школа|учебный\s+центр|онлайн.школа|команда\s+преподавател|^школа\s+",
    "brand": r"наша\s+компания|официальный\s+канал\s+компании|пресс.служб|корпоративный\s+канал",
    "news": r"\bновости\b|новостн[а-я]*|дайджест|сообщает\s+(?:агентство|издание)|"
            r"по\s+данным\s+(?:сми|агентства|издания|reuters|bloomberg|компании)|"
            r"\b(?:openai|google|apple|meta|adobe|microsoft|компания|разработчики)\s+"
            r"(?:[а-яa-z0-9-]+\s+){0,3}(?:выпустил[аи]?|представил[аи]?|анонсировал[аи]?|объявил[аи]?)\b|"
            r"стало\s+известно|как\s+сообщает\b",
    "aggregator": r"склад\s+(?:файлов|плагинов|пресетов)|скачать\s+|подборка\s+(?:плагинов|файлов|сервисов|нейросетей|инструментов)|"
                  r"плагины\s+и\s+аддоны|plugins\s+and\s+addons|бизнес.идеи|"
                  r"история\s+(?:компании|успеха)\s+|\bтоп[ -]+\d+\s+(?:сервисов|нейросетей|инструментов)",
}
POSITIVE = re.compile(r"\b(?:разбираю|объясняю|показываю|консультирую|провожу|запускаю)\b|"
                      r"\b(?:мой|моя|мои|моём|моем)\s+(?:кейс|проект|клиент|опыт|курс|практик|работ)|"
                      r"\bя\s+(?:психолог|врач|стоматолог|дизайнер|монтаж[её]р|тренер|риелтор|репетитор|работаю|помогаю)", re.I)

# First-person editorial phrasing alone does not establish professional practice.
OWN_PRACTICE = re.compile(
    r"\b(?:мой|моя|мои|моём|моем|моего|моей|моих)\s+"
    r"(?:кейс|проект|клиент|пациент|опыт|курс|практик|работ|портфолио|ученик|гайд|шаблон|консультаци)|"
    r"\bя\s+(?:психолог|врач|стоматолог|дизайнер|монтаж[её]р|тренер|риелтор|репетитор|"
    r"разработчик|программист|предприниматель|основатель|сооснователь|техдир|работаю|консультирую|помогаю)|"
    r"\bя\s+(?:создал[аи]?|разработал[аи]?|сделал[аи]?|спроектировал[аи]?|смонтировал[аи]?)\b|"
    r"\b(?:разбираю|показываю|объясняю)[^.!?]{0,100}\b(?:на|в)\s+сво[её]м\s+(?:проект|кейс)|"
    r"\b(?:консультирую|оказываю\s+услуги|беру\s+(?:заказы|проекты)|"
    r"провожу\s+консультации|запускаю\s+(?:мини[ -]?)?курс)\b", re.I)

PROFESSIONAL_CONTEXT = re.compile(
    r"\b(?:кейс|проект|клиент|пациент|консультаци|практик|услуг|портфолио|"
    r"предпринимател|основател|разработчик|программист|техдир)|"
    r"\bя\s+(?:психолог|врач|стоматолог|дизайнер|монтаж[её]р|тренер|риелтор|репетитор)", re.I)

CONTENT_LABELS = {**TYPE_LABELS, "expert": "Признаки авторской практики / разбора",
                  "forwarded": "Пересылка", "unknown": "Недостаточно признаков"}


def _proof(source, match=None):
    text = source.get("text", "")
    position = match[0] if isinstance(match, tuple) else match.start() if match else 0
    start = max(0, position - 70)
    return {"source_id": source["id"], "url": source.get("url", ""), "date": source.get("date", ""),
            "quote": text[start:start + 280]}


@lru_cache(maxsize=256)
def _word_spans(text):
    return tuple((_tokens(t.group())[0], t.start(), t.end()) for t in re.finditer(r"[a-zа-яё0-9]+", text, re.I))


@lru_cache(maxsize=256)
def _word_index(text):
    index = {}
    for position, (word, _, _) in enumerate(_word_spans(text)):
        stem = _stem(word)
        # _same_word permits prefix matches only when both stems have >=5 letters.
        key = stem[:5] if len(stem) >= 5 else stem
        index.setdefault(key, []).append(position)
    return index


@lru_cache(maxsize=2048)
def _term_tokens(term):
    return tuple(_tokens(term))


def _term_span(term, text):
    needle = _term_tokens(term)
    if not needle:
        return None
    tokens = _word_spans(text)
    stem = _stem(needle[0])
    key = stem[:5] if len(stem) >= 5 else stem
    for i in _word_index(text).get(key, ()):
        if i + len(needle) > len(tokens):
            continue
        if all(_same_word(a, b[0]) for a, b in zip(needle, tokens[i:i + len(needle)])):
            return tokens[i][1], tokens[i + len(needle) - 1][2]
    return None


def _contains(term, text):
    return _term_span(term, text) is not None


def _independent_sources(sources):
    seen_ids, seen_texts, result = set(), set(), []
    for source in sources:
        fingerprint = " ".join(_tokens(source["text"]))
        if source.get("forwarded") or source["id"] in seen_ids or fingerprint in seen_texts:
            continue
        seen_ids.add(source["id"])
        seen_texts.add(fingerprint)
        result.append(source)
    return result


def extract_offers(row):
    result = []
    kinds = {"consultation": r"консультаци", "education": r"\bкурс(?:а|ы|ов|е|у|ом)?\b|практикум|обучени|вебинар|интенсив|наставничеств",
             "service": r"оказываю\s+услуг|беру\s+(?:заказы|проекты)|заказать\s+|услуги\s+|сопровождени",
             "booking": r"запис[ьиь]|записаться|оставить\s+заявку"}
    for source in evidence_sources(row):
        text = source["text"]
        for kind, pattern in kinds.items():
            match = re.search(pattern, text, re.I)
            if not match:
                continue
            proof = _proof(source, match)
            quote = proof["quote"]
            own_offer = {
                "education": r"\b(?:запускаю|запускаем|провожу|открываю)\s+(?:(?:мини|онлайн|новый|авторский|бесплатный)[\s-]+){0,3}(?:курс|обучение|практикум|вебинар|интенсив)|\b(?:мой|наш)\s+(?:курс|практикум|вебинар)",
                "consultation": r"\b(?:провожу|веду|даю|предлагаю)\s+(?:(?:личные|индивидуальные|онлайн)[\s-]+){0,2}консультаци|запись\s+ко\s+мне",
                "service": r"мои\s+услуги|оказываю\s+услуг|беру\s+(?:заказы|проекты)",
                "booking": r"запис[а-я]*\s+ко\s+мне|мои\s+услуги|провожу\s+консультаци|запускаю\s+(?:мини.?)?курс",
            }[kind]
            third_party = source.get("forwarded") or re.search(r"чуж(?:ой|ого)\s+курс|курс\s+коллеги|рекомендую\s+курс", quote, re.I)
            ownership = "third_party" if third_party else "self_reported" if re.search(own_offer, quote, re.I) else "unknown"
            prices = re.findall(r"(?:\d[\d\s.,]{0,15})\s*(?:₽|руб\.?|BYN|USD|EUR|\$|€)(?!\w)", quote, re.I)
            methods = list(source.get("links", [])) if kind == "booking" or re.search(r"запис|заявк", quote, re.I) else []
            methods += re.findall(r"(?:в\s+личн\w*\s+сообщени\w*|в\s+директ|@[A-Za-z][A-Za-z0-9_]{3,31})", quote, re.I)
            result.append({"kind": kind, **proof, "ownership": ownership,
                           "prices": [p.strip() for p in prices], "booking_methods": list(dict.fromkeys(methods))[:8]})
    return result


def classify_content(row, topic="", niche_id="", micro_id="", category_ids=()):
    sources = evidence_sources(row)
    own = _independent_sources(sources)
    if micro_id:
        profiles = [MICRO_NICHES[micro_id]] if micro_id in MICRO_NICHES else []
    elif category_ids:
        profiles = assessment_profiles(category_ids)
    elif niche_id:
        profiles = get_micro_niches(niche_id)
    else:
        profiles = resolve_micro_niches(topic)
    positives, personal_sources, source_roles = [], set(), {}
    for source in own:
        text = source["text"]
        practice = OWN_PRACTICE.search(text)
        match = practice or POSITIVE.search(text)
        topic_context = any(_contains(term, text) for profile in profiles for term in profile["match_terms"])
        editorial = any(re.search(NEGATIVE[k], text, re.I) for k in ("news", "aggregator"))
        if match and (PROFESSIONAL_CONTEXT.search(text) or topic_context) and (practice or not editorial):
            positives.append(_proof(source, match))
            if practice:
                personal_sources.add(source["id"])
            source_roles[source["id"]] = ("expert", match)
    posts = [s for s in own if s["id"] != "description"]
    title = row.get("title", "")
    negatives, dominant = [], []
    for kind, pattern in NEGATIVE.items():
        # An author may discuss a release in a client case or share their own files.
        hits = [s for s in posts if re.search(pattern, s["text"], re.I)
                and not (kind in ("news", "aggregator") and s["id"] in personal_sources)]
        for source in hits:
            source_roles.setdefault(source["id"], (kind, re.search(pattern, source["text"], re.I)))
        bio = next((s for s in own if s["id"] == "description" and re.search(pattern, s["text"], re.I)), None)
        explicit = bool(re.search(pattern, title, re.I) and bio)
        if explicit or (len(hits) >= 2 and len(hits) / max(len(posts), 1) >= .6):
            dominant.append(kind)
            negatives += [{"type": kind, **_proof(s, re.search(pattern, s["text"], re.I))}
                          for s in hits[:3] + ([bio] if bio else [])]
    # Forwarded posts cannot supply practice evidence, and a feed dominated by
    # forwarding is not promoted on the strength of a personal bio alone.
    all_posts = [s for s in sources if s["id"] != "description"]
    forwarded = [s for s in all_posts if s.get("forwarded")]
    forwarded_dominates = len(forwarded) >= 3 and len(forwarded) / max(len(all_posts), 1) >= .6
    if forwarded_dominates:
        if 'aggregator' not in dominant:
            dominant.append('aggregator')
        negatives += [{"type": "aggregator", "signal": "forwarded", **_proof(s)} for s in forwarded[:3]]
    has_expert_proof = len(positives) >= 2 and bool(personal_sources) and any(
        p["source_id"] != "description" for p in positives)
    if dominant:
        channel_type = "unknown" if len(positives) >= 2 else dominant[0]
    else:
        channel_type = "expert" if has_expert_proof else "unknown"
    confidence = "high" if channel_type not in ("unknown", "expert") else "medium" if channel_type == "expert" else "low"
    relevance_evidence, best_count = [], 0
    for profile in profiles:
        found, spans = [], []
        for term in sorted(profile["match_terms"], key=len, reverse=True):
            for source in own:
                span = _term_span(term, source["text"])
                if span and not any(sid == source["id"] and span[0] < end and start < span[1]
                                    for sid, start, end in spans):
                    found.append({"term": term, "micro_id": profile["id"], **_proof(source, span)})
                    spans.append((source["id"], *span))
                    break
        if len(found) > best_count:
            best_count, relevance_evidence = len(found), found
    relevance = "matched" if best_count >= 2 else "unknown"
    if profiles and best_count == 0:
        target_ids = {p["id"] for p in profiles}
        for other in MICRO_NICHES.values():
            if other["id"] not in target_ids and sum(any(_contains(t, s["text"]) for s in own) for t in other["match_terms"]) >= 2:
                relevance = "mismatch"
                break
    if not category_ids and not profiles and len(_tokens(topic)) >= 2 and sum(_contains(topic, s["text"]) for s in own) >= 2:
        relevance = "matched"
        relevance_evidence = [{"term": topic, **_proof(s)} for s in own if _contains(topic, s["text"])][:3]
    reasons = []
    if channel_type == "unknown":
        reasons.append("Недостаточно независимых признаков личной практики или признаки конфликтуют")
        if len(positives) >= 2 and not personal_sources:
            reasons.append("Авторские формулировки есть, но личная практика не установлена")
    elif channel_type != "expert":
        reasons.append(f"Нецелевой тип: {TYPE_LABELS[channel_type]}")
    if relevance != "matched":
        reasons.append("Тематика не совпадает" if relevance == "mismatch" else "Недостаточно признаков соответствия узкой теме")
    if dominant:
        reasons.append("Преобладают нецелевые признаки: " + ", ".join(TYPE_LABELS[k] for k in dominant))
    if forwarded_dominates:
        reasons.append(f"Преобладают пересылки: {len(forwarded)}/{len(all_posts)} текстовых постов")
    post_roles = []
    for source in sources:
        if source["id"] == "description":
            continue
        kind, match = ("forwarded", None) if source.get("forwarded") else source_roles.get(source["id"], ("unknown", None))
        post_roles.append({"role": kind, **_proof(source, match)})
    summary = {"text_posts": len(post_roles), "independent_own_posts": len(posts),
               "forwarded_posts": sum(s.get("forwarded", False) for s in sources if s["id"] != "description"),
               "expert_evidence_posts": sum(s["id"] in source_roles and source_roles[s["id"]][0] == "expert" for s in posts),
               "personal_practice_posts": sum(s["id"] in personal_sources for s in posts),
               "dominant_non_target_types": dominant}
    return {"channel_type": channel_type, "confidence": confidence, "positive_evidence": positives,
            "negative_evidence": negatives, "relevance": relevance, "relevance_evidence": relevance_evidence,
            "reasons": reasons, "rule_version": RULE_VERSION,
            "content_summary": summary, "post_roles": post_roles}


def qualify(row, topic="", niche_id="", micro_id="", category_ids=()):
    row = copy.deepcopy(row)
    row["analysis_context"] = {"topic": topic, "niche_id": niche_id, "micro_id": micro_id}
    if category_ids:
        row['analysis_context']['category_ids'] = list(category_ids)
    metric_status = row.get("metric_status", row.get("status", "error"))
    metric_reasons = row.get("metric_reasons", row.get("reasons", ""))
    row["metric_status"], row["metric_reasons"] = metric_status, metric_reasons
    row["metrics"] = {k: row.get(k) for k in ("subs", "avg_views", "median_views", "view_ratio", "norm", "avg_gap_days", "days_since_last")}
    classification = classify_content(row, topic, niche_id, micro_id, category_ids)
    row.update(classification=classification, channel_type=classification["channel_type"],
               relevance=classification["relevance"], offers=extract_offers(row), producer_status="Не установлено")
    row["offers_summary"] = "; ".join(dict.fromkeys(o["kind"] for o in row["offers"])) or "Не найдено в просмотренной выборке"
    if metric_status == "error":
        row.update(status="error", verdict="⚪ Не удалось проверить")
    elif metric_status == "rejected" or classification["channel_type"] not in ("expert", "unknown") or classification["relevance"] == "mismatch":
        row.update(status="rejected", verdict="🚩 Не прошёл отбор")
    elif metric_status == "review" or classification["channel_type"] != "expert" or classification["relevance"] != "matched":
        row.update(status="review", verdict="🟡 Требует проверки")
    else:
        row.update(status="top" if row.get("youtube") or row.get("instagram") else "recommended",
                   verdict="🟢 ТОП (Эксперт + внешние ссылки)" if row.get("youtube") or row.get("instagram") else "🟢 РЕКОМЕНДОВАН (Предварительно)")
    row["reasons"] = "; ".join(([metric_reasons] if metric_status not in ("top", "recommended") and metric_reasons else []) + classification["reasons"]) or "Метрики, признаки личной практики и тематика соответствуют"
    return row
