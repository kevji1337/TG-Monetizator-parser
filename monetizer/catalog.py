"""Read user-supplied JSON as data. No evaluation or executable imports."""
import json
from pathlib import Path

DATA_DIR = Path(__file__).with_name("data")


def _strings(value, label, minimum=1):
    if not isinstance(value, list) or len(value) < minimum or any(
            not isinstance(item, str) or not item.strip() or len(item) > 4000 for item in value):
        raise ValueError(f"{label}: ожидается список непустых строк")


def load_catalog(path=None):
    data = json.loads(Path(path or DATA_DIR / "keyword_catalog.json").read_text(encoding="utf-8-sig"))
    if data.get("schema_version") != 1 or data.get("language") != "ru" or not isinstance(data.get("niches"), list):
        raise ValueError("Неподдерживаемая структура каталога ниш")
    niche_ids, micro_ids = set(), set()
    for niche in data["niches"]:
        if not isinstance(niche.get("id"), str) or niche["id"] in niche_ids or not niche.get("title"):
            raise ValueError("Повторный ID или отсутствующее название ниши")
        niche_ids.add(niche["id"])
        _strings(niche.get("aliases"), "aliases", 0)
        if not isinstance(niche.get("micro_niches"), list) or not niche["micro_niches"]:
            raise ValueError("У ниши отсутствуют направления")
        for micro in niche["micro_niches"]:
            if not isinstance(micro.get("id"), str) or micro["id"] in micro_ids or not micro.get("title"):
                raise ValueError("Повторный ID или отсутствующее название направления")
            micro_ids.add(micro["id"])
            for field in ("match_terms", "discovery_queries", "expert_signals", "non_target_signals", "adjacent_topics"):
                _strings(micro.get(field), f"{micro['id']}.{field}")
            queries = [q.strip().casefold() for q in micro["discovery_queries"]]
            if len(queries) != len(set(queries)) or any(len(q) > 120 for q in queries):
                raise ValueError(f"{micro['id']}: повторные или слишком длинные запросы")
            if not isinstance(micro.get("disambiguation"), str):
                raise ValueError(f"{micro['id']}: отсутствует пояснение границ темы")
    return data


def load_playbook(path=None):
    data = json.loads(Path(path or DATA_DIR / "outreach_playbook.json").read_text(encoding="utf-8-sig"))
    if data.get("schema_version") != 1 or data.get("language") != "ru" or not isinstance(data.get("global_rules"), dict):
        raise ValueError("Неподдерживаемая структура сценариев сообщений")
    _strings(data.get("forbidden_claims"), "forbidden_claims")
    ids = set()
    if not isinstance(data.get("scenarios"), list):
        raise ValueError("Отсутствуют сценарии")
    for scenario in data["scenarios"]:
        if not isinstance(scenario.get("id"), str) or scenario["id"] in ids or not scenario.get("title"):
            raise ValueError("Повторный ID или отсутствующее название сценария")
        ids.add(scenario["id"])
        for field in ("required_evidence", "partnership_hypotheses", "message_structure", "avoid"):
            _strings(scenario.get(field), f"{scenario['id']}.{field}")
        if not isinstance(scenario.get("clarification_question"), str) or not isinstance(scenario.get("observation_rule"), str):
            raise ValueError("Сценарий не содержит вопроса или правила наблюдения")
        # Fictional examples are reference material, never channel evidence.
        for example in scenario.get("examples", []):
            evidence = example.get("input_evidence") if isinstance(example, dict) else None
            if not isinstance(evidence, dict) or evidence.get("fictional") is not True:
                raise ValueError("Учебный пример должен быть помечен fictional=true")
    if "limited_evidence" not in ids or "external_link_unverified" not in ids:
        raise ValueError("Нужны сценарии для недостатка данных и непроверенных ссылок")
    return data
