"""Explainable preliminary qualification. Text claims are not verified identity."""
import copy
import re
from functools import lru_cache

from .keywords import MICRO_NICHES, get_micro_niches, resolve_micro_niches, _tokens, _same_word
from .pitches import evidence_sources

RULE_VERSION = "qualification-2"
CHANNEL_TYPES = {"expert", "school", "brand", "news", "job_board", "aggregator", "store", "unknown"}
TYPE_LABELS = {"expert": "Личный эксперт", "school": "Школа", "brand": "Бренд", "news": "Новости",
               "job_board": "Вакансии", "aggregator": "Агрегатор", "store": "Магазин", "unknown": "Не установлено"}
NEGATIVE = {
    "job_board": r"ваканси|ищем\s+(?:монтаж|дизайнер|специалист)|требуется\s+|резюме\s+кандидат",
    "store": r"магазин|купить\s+шаблон|каталог\s+товар|скидк.{0,25}заказ|доставка\s+товар",
    "school": r"наша\s+школа|учебный\s+центр|онлайн.школа|команда\s+преподавател|^школа\s+",
    "brand": r"наша\s+компания|официальный\s+канал\s+компании|пресс.служб|корпоративный\s+канал",
    "news": r"новости\s+(?:дня|индустрии|рынка)|новостной|дайджест|сообщает\s+(?:агентство|издание)",
    "aggregator": r"склад\s+(?:файлов|плагинов|пресетов)|скачать\s+|подборка\s+(?:плагинов|файлов)|плагины\s+и\s+аддоны|plugins\s+and\s+addons",
}
POSITIVE = re.compile(r"\b(?:разбираю|объясняю|показываю|разработал[аи]?|сделал[аи]?|консультирую|провожу|запускаю)\b|"
                      r"\b(?:мой|моя|мои|моём|моем)\s+(?:кейс|проект|клиент|опыт|курс|практик|работ)|"
                      r"\bя\s+(?:психолог|врач|стоматолог|дизайнер|монтаж[её]р|тренер|риелтор|репетитор|работаю|помогаю)", re.I)


def _proof(source, match=None):
    text = source.get("text", "")
    position = match[0] if isinstance(match, tuple) else match.start() if match else 0
    start = max(0, position - 70)
    return {"source_id": source["id"], "url": source.get("url", ""), "date": source.get("date", ""),
            "quote": text[start:start + 280]}


@lru_cache(maxsize=256)
def _word_spans(text):
    return tuple((_tokens(t.group())[0], t.start(), t.end()) for t in re.finditer(r"[a-zа-яё0-9]+", text, re.I))


def _term_span(term, text):
    needle = _tokens(term)
    tokens = _word_spans(text)
    for i in range(len(tokens) - len(needle) + 1) if needle else ():
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


def classify_content(row, topic="", niche_id="", micro_id=""):
    sources = evidence_sources(row)
    own = _independent_sources(sources)
    if micro_id:
        profiles = [MICRO_NICHES[micro_id]] if micro_id in MICRO_NICHES else []
    elif niche_id:
        profiles = get_micro_niches(niche_id)
    else:
        profiles = resolve_micro_niches(topic)
    positives = []
    for source in own:
        match = POSITIVE.search(source["text"])
        professional_context = re.search(r"\b(?:кейс|проект|клиент|пациент|консультаци|практик|услуг|портфолио)|"
                                         r"\bя\s+(?:психолог|врач|стоматолог|дизайнер|монтаж[её]р|тренер|риелтор|репетитор)", source["text"], re.I)
        topic_context = any(_contains(term, source["text"]) for profile in profiles for term in profile["match_terms"])
        if match and (professional_context or topic_context):
            positives.append(_proof(source, match))
    posts = [s for s in own if s["id"] != "description"]
    title = row.get("title", "")
    negatives, dominant = [], []
    for kind, pattern in NEGATIVE.items():
        hits = [s for s in posts if re.search(pattern, s["text"], re.I)]
        bio = next((s for s in own if s["id"] == "description" and re.search(pattern, s["text"], re.I)), None)
        explicit = bool(re.search(pattern, title, re.I) and bio)
        if explicit or (len(hits) >= 2 and len(hits) / max(len(posts), 1) >= .6):
            dominant.append(kind)
            negatives += [{"type": kind, **_proof(s, re.search(pattern, s["text"], re.I))}
                          for s in hits[:3] + ([bio] if bio else [])]
    if dominant:
        channel_type = "unknown" if len(positives) >= 2 else dominant[0]
    else:
        channel_type = "expert" if len(positives) >= 2 else "unknown"
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
    if not profiles and len(_tokens(topic)) >= 2 and sum(_contains(topic, s["text"]) for s in own) >= 2:
        relevance = "matched"
        relevance_evidence = [{"term": topic, **_proof(s)} for s in own if _contains(topic, s["text"])][:3]
    reasons = []
    if channel_type == "unknown":
        reasons.append("Недостаточно независимых признаков личной практики или признаки конфликтуют")
    elif channel_type != "expert":
        reasons.append(f"Нецелевой тип: {TYPE_LABELS[channel_type]}")
    if relevance != "matched":
        reasons.append("Тематика не совпадает" if relevance == "mismatch" else "Недостаточно признаков соответствия узкой теме")
    return {"channel_type": channel_type, "confidence": confidence, "positive_evidence": positives,
            "negative_evidence": negatives, "relevance": relevance, "relevance_evidence": relevance_evidence,
            "reasons": reasons, "rule_version": RULE_VERSION}


def qualify(row, topic="", niche_id="", micro_id=""):
    row = copy.deepcopy(row)
    row["analysis_context"] = {"topic": topic, "niche_id": niche_id, "micro_id": micro_id}
    metric_status = row.get("metric_status", row.get("status", "error"))
    metric_reasons = row.get("metric_reasons", row.get("reasons", ""))
    row["metric_status"], row["metric_reasons"] = metric_status, metric_reasons
    row["metrics"] = {k: row.get(k) for k in ("subs", "avg_views", "median_views", "view_ratio", "norm", "avg_gap_days", "days_since_last")}
    classification = classify_content(row, topic, niche_id, micro_id)
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
