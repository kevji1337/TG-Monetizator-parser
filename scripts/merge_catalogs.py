"""Reproducible, explicitly scoped merge. Input JSON is data, never code."""
import copy
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'monetizer' / 'data'
DIRECT = {
    'video_editing_nle': 'video_editing', 'motion_design_graphics': 'motion_design',
    'three_d_modeling_render': 'three_d_visualization', 'video_shooting_camera_light': 'video_production',
    'ai_for_marketers_copywriters': 'ai_for_marketers', 'doctor_patient_education': 'clinical_medicine',
    'nutrition_dietology': 'dietitian_nutrition', 'rehab_posture_back_pain': 'physiotherapy_rehab',
    'relationships_family_parenting': 'family_relationship_psychology',
    'self_knowledge_reflection': 'self_reflection_practices', 'b2b_sales': 'b2b_sales',
    'realtor_experts': 'residential_realtor', 'landscape_design': 'landscape_design',
    'author_tours_guides': 'author_tours', 'it_career_resume_search': 'it_career_coaching',
    'it_interview_preparation': 'it_career_coaching', 'beauty_client_acquisition': 'beauty_client_acquisition',
    'beauty_business_operations': 'beauty_business_operations', 'personal_budget_savings': 'personal_budgeting',
    'investing_securities': 'long_term_investing', 'taxes_deductions_protection': 'taxes_personal_finance',
    'strength_training_gym': 'strength_coaching', 'running_endurance_triathlon': 'running_endurance',
    'ux_ui_product_design': 'ux_ui_design', 'presentation_design': 'presentation_design',
    'web_design_nocode_builders': 'websites_nocode', 'graphic_design_branding_portfolio': 'brand_portfolio',
}
# Each mixed source is split by explicit vocabulary; leftovers remain in the review report.
SPLIT = {
    'ai_for_photo_video_creators': {
        'ai_for_photographers': r'фотограф|ретуш|photoshop|magnific|topaz|нейрофото|апскейл',
        'ai_for_designers': r'midjourney|stable diffusion|comfyui|flux|генерация изображений',
        'ai_video_creation': r'видео|runway|kling'},
    'ai_for_developers_analysts': {
        'ai_for_developers': r'разработ|copilot|cursor|claude code|llm|rag|langchain|mcp|агент',
        'ai_for_analysts': r'аналитик|sql'},
    'management_operations_finance': {
        'business_operations': r'команд|okr|kpi|процесс|делегир|регламент|операцион|agile|руководител',
        'unit_economics': r'юнит|уч[её]т|p&l|cash flow', 'hr_leadership': r'найм'},
    'english_speaking_exams': {
        'english_exams': r'ielts|toefl|cambridge|\bfce\b|\bcae\b',
        'business_english': r'business|для it',
        'general_english': r'английск|фразов'},
    'european_languages': {
        'german_language': r'немецк|goethe|testdaf',
        'spanish_french_language': r'испанск|dele|француз|delf|dalf',
    },
    'yoga_pilates_mobility': {
        'yoga_pilates': r'йог|пилатес|реформер|барре|хатха|виньяса|аштанг',
        'mobility_functional_fitness': r'стретч|растяж|мобильност'},
}
NEW = {
    'ai_for_office_legal_hr_teachers': ('2', 'ai_office_professionals'),
    'practicing_psychologist': ('4', 'psychotherapy_practice'),
    'b2b_marketing_leadgen': ('5', 'b2b_lead_generation'),
    'asian_languages': ('9', 'asian_languages'),
}
SCENARIO_MAP = {
    'practical_breakdowns': 'practical_breakdown', 'own_projects_cases': 'own_projects_cases',
    'recurring_audience_questions': 'recurring_questions', 'explicit_consultations': 'consultations_explicit',
    'own_course_mentioned': 'own_course_explicit', 'own_video_linked': 'verified_own_video',
    'unverified_external_link': 'external_link_unverified', 'insufficient_channel_data': 'limited_evidence',
    'free_materials_lead_magnet': 'free_materials_lead_magnet',
}


def unique(items):
    result, seen = [], set()
    for value in items:
        value = re.sub(r'\s+', ' ', value).strip()
        if value.casefold() not in seen:
            seen.add(value.casefold())
            result.append(value)
    return result


def read(name):
    return json.loads((DATA / 'sources' / name).read_text(encoding='utf-8-sig'))


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def merge():
    catalog = read('keyword_catalog_chatgpt.json')
    incoming = read('keyword_catalog_claude.json')
    micros = {m['id']: m for n in catalog['niches'] for m in n['micro_niches']}
    original_ids = set(micros)
    report = {'version': 2, 'mappings': {}, 'pending_review': [], 'source_hashes': {}}

    def add_profile(parent, micro):
        next(n for n in catalog['niches'] if n['id'] == parent)['micro_niches'].append(micro)
        micros[micro['id']] = micro

    # Separate video AI, conversational English and yoga rather than broadening existing specialties.
    for parent, source_id, target_id, title in [
        ('2', 'ai_for_photo_video_creators', 'ai_video_creation', 'Нейросети для создания видео'),
        ('9', 'english_speaking_exams', 'general_english', 'Разговорный и общий английский'),
        ('10', 'yoga_pilates_mobility', 'yoga_pilates', 'Йога и пилатес')]:
        source = next(m for n in incoming['niches'] for m in n['micro_niches'] if m['id'] == source_id)
        micro = copy.deepcopy(source)
        micro.update(id=target_id, title=title, match_terms=[], discovery_queries=[],
                     disambiguation=f'{title}. Соседние специальности оцениваются отдельно.')
        add_profile(parent, micro)
    for niche in incoming['niches']:
        for source in niche['micro_niches']:
            sid = source['id']
            if sid in NEW:
                parent, target = NEW[sid]
                micro = copy.deepcopy(source)
                micro['id'] = target
                add_profile(parent, micro)
                report['mappings'][sid] = [target]
            elif sid in DIRECT:
                target = micros[DIRECT[sid]]
                for field in ('discovery_queries', 'match_terms', 'expert_signals', 'non_target_signals', 'adjacent_topics'):
                    target[field] = unique(target[field] + source[field])
                target['disambiguation'] += '\nДополнение Claude: ' + source['disambiguation']
                report['mappings'][sid] = [target['id']]
            elif sid in SPLIT:
                routes = SPLIT[sid]
                report['mappings'][sid] = list(routes)
                for field in ('discovery_queries', 'match_terms'):
                    for value in source[field]:
                        # More specific routes precede general language expressions.
                        target = next((mid for mid, pattern in routes.items() if re.search(pattern, value, re.I)), None)
                        if target:
                            micros[target][field] = unique(micros[target][field] + [value])
                        else:
                            report['pending_review'].append({'source_id': sid, 'field': field, 'value': value,
                                                             'reason': 'Нет однозначного узкого направления'})
            else:
                raise ValueError('Missing explicit mapping: ' + sid)
    catalog['catalog_revision'] = 'merged-2'
    catalog['source_providers'] = ['ChatGPT', 'Claude']
    assert original_ids <= set(micros)
    playbook = read('outreach_playbook_chatgpt.json')
    claude = read('outreach_playbook_claude.json')
    playbook['global_rules']['claude_editorial_rules'] = claude['global_rules']
    # Descriptive prohibitions are guidance, never substituted for literal checks.
    playbook['global_rules']['claude_forbidden_guidance'] = claude['forbidden_claims']
    existing = {s['id']: s for s in playbook['scenarios']}
    for source in claude['scenarios']:
        sid = SCENARIO_MAP[source['id']]
        normalized = copy.deepcopy(source)
        normalized['id'] = sid
        normalized['title'] = existing[sid]['title'] if sid in existing else 'Автор предлагает свой бесплатный материал'
        for example in normalized.get('examples', []):
            evidence = example['input_evidence']
            if not isinstance(evidence, str) or 'вымышлен' not in evidence:
                raise ValueError('Claude example is not explicitly fictional')
            example['input_evidence'] = {'fictional': True, 'description': evidence}
        if sid in existing:
            target = existing[sid]
            for field in ('required_evidence', 'partnership_hypotheses', 'message_structure', 'avoid'):
                target[field] = unique(target[field] + normalized[field])
            target['observation_rule'] += '\n' + normalized['observation_rule']
            target['examples'].extend(normalized.get('examples', []))
        else:
            playbook['scenarios'].append(normalized)
    playbook['playbook_revision'] = 'merged-2'
    for path in sorted((DATA / 'sources').glob('*.json')):
        # Canonical JSON makes provenance independent of Git's Windows line endings.
        canonical = json.dumps(json.loads(path.read_text(encoding='utf-8-sig')), ensure_ascii=False,
                               sort_keys=True, separators=(',', ':')).encode('utf-8')
        report['source_hashes'][path.name] = hashlib.sha256(canonical).hexdigest()
    report.update(preserved_micro_ids=sorted(original_ids), micro_count=len(micros),
                  query_count=sum(len(m['discovery_queries']) for m in micros.values()), scenario_count=len(playbook['scenarios']))
    write(DATA / 'keyword_catalog.json', catalog)
    write(DATA / 'outreach_playbook.json', playbook)
    write(DATA / 'merge_report.json', report)
    print(json.dumps({k: v for k, v in report.items() if k.endswith('_count')}, ensure_ascii=False))


if __name__ == '__main__':
    merge()
