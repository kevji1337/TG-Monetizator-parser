"""Actual TGStat form options and content-only qualification of their channels."""
import hashlib
import json
from pathlib import Path

SNAPSHOT_PATH = Path(__file__).with_name('data') / 'tgstat_categories.json'
CACHE_PATH = Path(__file__).resolve().parents[1] / 'results/monetizer/tgstat_categories.json'

# These are assessment scopes, not TGStat subcategories and never text search queries.
CATEGORY_PROFILES = {
    '1': ['ai_for_developers', 'ai_for_analysts', 'ai_office_professionals', 'ai_video_creation',
          'ai_for_marketers', 'ai_for_designers', 'ai_for_photographers'],
    '4': [],  # Blogs alone do not establish professional relevance.
    '5': ['english_exams', 'business_english', 'german_language', 'spanish_french_language',
          'general_english', 'asian_languages', 'it_career_coaching', 'it_portfolio_mentor'],
    '7': ['ai_for_photographers', 'video_production'],
    '9': ['ai_for_developers', 'ai_for_analysts', 'websites_nocode'],
    '10': ['unit_economics', 'personal_budgeting', 'financial_planning', 'long_term_investing', 'taxes_personal_finance'],
    '11': ['video_editing', 'motion_design', 'video_production', 'ai_video_creation'],
    '12': ['business_operations', 'b2b_sales', 'b2b_lead_generation', 'unit_economics', 'hr_leadership',
           'residential_realtor', 'commercial_real_estate'],
    '14': ['b2b_sales', 'beauty_client_acquisition', 'b2b_lead_generation'],
    '20': ['strength_coaching', 'running_endurance', 'mobility_functional_fitness', 'online_fitness_coaching'],
    '21': ['english_exams', 'business_english', 'german_language', 'spanish_french_language', 'general_english', 'asian_languages'],
    '23': ['it_career_coaching', 'it_portfolio_mentor', 'career_psychology', 'hr_leadership'],
    '25': ['author_tours'],
    '27': ['beauty_client_acquisition', 'beauty_business_operations'],
    '28': ['clinical_medicine', 'dentistry_practice', 'physiotherapy_rehab', 'dietitian_nutrition'],
    '31': ['b2b_lead_generation', 'b2b_sales', 'ai_for_marketers', 'beauty_client_acquisition'],
    '33': ['cbt_psychology', 'family_relationship_psychology', 'career_psychology', 'self_reflection_practices', 'psychotherapy_practice'],
    '34': ['ux_ui_design', 'websites_nocode', 'presentation_design', 'brand_portfolio', 'motion_design',
           'three_d_visualization', 'landscape_design', 'ai_for_designers'],
    '36': ['family_relationship_psychology'],
    '45': ['dietitian_nutrition', 'physiotherapy_rehab', 'strength_coaching', 'running_endurance',
           'mobility_functional_fitness', 'online_fitness_coaching', 'yoga_pilates'],
    '47': ['ai_for_marketers', 'video_editing', 'beauty_client_acquisition'],
    '48': [],  # A course directory is not evidence of the owner's expertise.
    '49': ['brand_portfolio', 'three_d_visualization', 'motion_design'],
    '53': ['landscape_design', 'three_d_visualization', 'residential_realtor', 'commercial_real_estate'],
}
BROAD_CATEGORIES = {'4', '5', '17', '41', '48'}


def validate_categories(value):
    if not isinstance(value, dict) or value.get('schema_version') != 1 or not isinstance(value.get('categories'), list):
        raise ValueError('Некорректный каталог тематик TGStat')
    seen, items = set(), value['categories']
    if not 1 <= len(items) <= 500:
        raise ValueError('Не удалось прочитать список тематик TGStat')
    for field in ('categories', 'countries', 'languages'):
        if field not in value:
            continue
        seen = set()
        if not isinstance(value[field], list) or not 1 <= len(value[field]) <= 500:
            raise ValueError('Некорректный список ' + field)
        for item in value[field]:
            if (not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id'].isascii()
                    or not item['id'].isdigit() or item['id'] in seen or not isinstance(item.get('title'), str)
                    or not item['title'].strip() or len(item['title']) > 180):
                raise ValueError('Повторный или некорректный вариант ' + field)
            seen.add(item['id'])
    return value


def load_categories():
    fallback = validate_categories(json.loads(SNAPSHOT_PATH.read_text(encoding='utf-8-sig')))
    if CACHE_PATH.exists():
        try:
            return {**fallback, **validate_categories(json.loads(CACHE_PATH.read_text(encoding='utf-8-sig')))}
        except (ValueError, OSError):
            pass
    return fallback


def category_key(category_id):
    if not isinstance(category_id, str) or not category_id.isascii() or not category_id.isdigit():
        raise ValueError('Нужен числовой ID тематики TGStat')
    return 'tgstat:' + category_id


def category_id_from_key(key):
    if not isinstance(key, str) or not key.startswith('tgstat:'):
        raise ValueError('Ожидается тематика TGStat, а не текстовый запрос')
    value = key[len('tgstat:'):]
    category_key(value)
    return value


def category_label(key):
    if not key.startswith('tgstat:'):
        return key  # Historical keyword reports.
    category_id = category_id_from_key(key)
    return next((c['title'] for c in load_categories()['categories'] if c['id'] == category_id), key)


def assessment_profiles(category_ids):
    from .keywords import MICRO_NICHES
    if set(category_ids) & BROAD_CATEGORIES:
        return list(MICRO_NICHES.values())
    ids = list(dict.fromkeys(m for c in category_ids for m in CATEGORY_PROFILES.get(c, [])))
    return [MICRO_NICHES[mid] for mid in ids if mid in MICRO_NICHES]


CATEGORY_RULES_VERSION = hashlib.sha256(json.dumps([CATEGORY_PROFILES, sorted(BROAD_CATEGORIES)], sort_keys=True).encode()).hexdigest()[:12]
