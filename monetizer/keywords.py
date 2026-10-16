"""Scoped expansion from the imported catalog; never mix unrelated specialties."""
import json
import hashlib
import re
from functools import lru_cache
from pathlib import Path
from .catalog import load_catalog

CATALOG_PATH = Path(__file__).with_name("niches.json")


def load_niches():
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


CATALOG = load_catalog()
CATALOG_VERSION = hashlib.sha256(json.dumps(CATALOG, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]
NICHES = CATALOG["niches"]
MICRO_NICHES = {micro["id"]: {**micro, "niche_id": niche["id"]}
                for niche in NICHES for micro in niche["micro_niches"]}


def _interleave(groups):
    return [group[index] for index in range(max(map(len, groups), default=0))
            for group in groups if index < len(group)]


COURSE_NICHES = [{**n, "keywords": _interleave([m["discovery_queries"] for m in n["micro_niches"]])}
                for n in NICHES]


def unique_queries(values):
    seen, result = set(), []
    for value in values:
        value = re.sub(r"\s+", " ", value).strip()[:120]
        if value and value.casefold() not in seen:
            seen.add(value.casefold())
            result.append(value)
    return result


def _tokens(value):
    return re.findall(r"[a-zа-я0-9]+", str(value).casefold().replace("ё", "е"))


@lru_cache(maxsize=8192)
def _stem(word):
    if len(word) < 5 or not re.fullmatch(r"[а-я]+", word):
        return word
    return re.sub(r"(?:иями|ями|ами|ого|ему|ов|ев|ей|ах|ях|ой|ий|ый|ая|яя|ое|ые|ие|ом|ам|ям|ем|а|я|ы|и|у|ю|е|о|ь)$", "", word)


def _same_word(left, right):
    left, right = _stem(left), _stem(right)
    return left == right or (min(len(left), len(right)) >= 5 and (left.startswith(right) or right.startswith(left)))


def get_micro_niches(niche_id=None):
    return [m for m in MICRO_NICHES.values() if niche_id is None or m["niche_id"] == str(niche_id)]


def resolve_micro_niches(topic):
    topic_tokens = [t for t in _tokens(topic) if t not in {"для", "и", "в", "по", "на", "с", "от"}]
    if not topic_tokens:
        return []
    # Explicit broad niche aliases select that niche, not every child sharing one word.
    aliases = {n["id"]: n.get("aliases", []) + [n["title"]] for n in NICHES}
    for legacy in load_niches():
        aliases.setdefault(legacy["id"], []).extend([legacy["title"], *legacy.get("aliases", [])])
    for niche_id, values in aliases.items():
        if any(_tokens(topic) == _tokens(value) for value in values):
            return get_micro_niches(niche_id)
    scored = []
    for micro in MICRO_NICHES.values():
        phrases = [micro["title"], *micro["match_terms"], *micro["discovery_queries"]]
        vocabulary = [token for phrase in phrases for token in _tokens(phrase)]
        matches = sum(any(_same_word(token, candidate) for candidate in vocabulary) for token in topic_tokens)
        phrase_length = max((len(_tokens(phrase)) for phrase in phrases
                             if all(any(_same_word(t, q) for q in topic_tokens) for t in _tokens(phrase))), default=0)
        if matches:
            scored.append(((matches, phrase_length), micro))
    if not scored:
        return []
    best = max(score for score, _ in scored)
    # A qualifying custom topic must not collapse to its one generic word.
    if len(topic_tokens) > 1 and best[0] < min(2, len(topic_tokens)):
        return []
    return [micro for score, micro in scored if score == best]


def expand_keywords(topic="", niche_id=None, limit=12, micro_niche_id=None):
    if not 1 <= limit <= 50:
        raise ValueError("Количество ключей: от 1 до 50")
    if micro_niche_id:
        micro = MICRO_NICHES.get(micro_niche_id)
        if micro is None or (str(niche_id) in {n["id"] for n in NICHES} and micro["niche_id"] != str(niche_id)):
            raise ValueError("Направление не относится к выбранной нише")
        return unique_queries(micro["discovery_queries"])[:limit]
    known_niche = next((n for n in NICHES if n["id"] == str(niche_id)), None)
    if known_niche:
        return unique_queries(_interleave([m["discovery_queries"] for m in known_niche["micro_niches"]]))[:limit]
    topic = re.sub(r"\s+", " ", topic).strip()[:120]
    if not topic:
        return []
    matching = resolve_micro_niches(topic)
    if matching:
        return unique_queries([topic, *_interleave([m["discovery_queries"] for m in matching])])[:limit]
    return [topic]  # No invented semantic expansion for a topic absent from the catalog.
