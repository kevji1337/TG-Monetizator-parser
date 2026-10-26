"""Explainable queue ordering and mutually exclusive selection summaries."""
from collections import Counter

PRIORITY_VERSION = 'candidate-priority-1'
NON_TARGET = {'school', 'brand', 'news', 'job_board', 'aggregator', 'store'}
BUCKETS = {
    'qualified': 'Подходят предварительно', 'metrics': 'Не прошли метрики',
    'non_target': 'Нецелевой тип канала', 'topic': 'Не совпадает тема',
    'access': 'Не удалось загрузить Telegram', 'ai_error': 'Ошибка или недоступность AI',
    'ai_waiting': 'Ожидают AI-разбора', 'stale': 'Нужен пересчёт оценки',
    'manual_review': 'Нужна ручная проверка',
}
OFFER_LABELS = {'consultation': 'Консультации', 'education': 'Обучение', 'service': 'Услуги', 'booking': 'Запись / заявка'}


def ai_priority(row):
    """Rank evidence of personal practice; never change a verdict or eligibility."""
    classification = row.get('classification') or {}
    summary = classification.get('content_summary') or {}
    score, reasons = 0, []
    if classification.get('channel_type') == 'expert':
        score += 30
        reasons.append('личный эксперт по локальным признакам +30')
    personal = int(summary.get('personal_practice_posts', 0) or 0)
    if personal:
        bonus = min(personal, 3) * 12
        score += bonus
        reasons.append(f'публикаций с личной практикой: {personal} +{bonus}')
    else:
        reasons.append('личная практика в публикациях не подтверждена')
    positive_sources = {proof.get('source_id') for proof in classification.get('positive_evidence', []) if proof.get('source_id')}
    if len(positive_sources) >= 2:
        score += 12
        reasons.append('два независимых источника авторского содержания +12')
    if classification.get('relevance') == 'matched':
        score += 12
        reasons.append('соответствует направлению +12')
    if any(offer.get('ownership') == 'self_reported' for offer in row.get('offers', [])):
        score += 6
        reasons.append('найдено заявление о собственном предложении +6')
    if row.get('author_contact'):
        score += 2
        reasons.append('есть кандидат контакта +2')
    if personal and (row.get('youtube') or row.get('instagram')):
        score += 2
        reasons.append('внешний контент при признаках практики +2')
    dominant = summary.get('dominant_non_target_types', [])
    if dominant or classification.get('channel_type') in NON_TARGET:
        score -= 35
        reasons.append('преобладают нецелевые признаки −35')
    roles = Counter(post.get('role') for post in classification.get('post_roles', []))
    total = sum(roles.values())
    if total and sum(roles[role] for role in NON_TARGET) / total >= .5:
        score -= 20
        reasons.append('нецелевые признаки в половине публикаций или чаще −20')
    if total and roles['forwarded'] / total >= .5:
        score -= 15
        reasons.append('половина публикаций или больше — пересылки −15')
    score = max(0, min(100, score))
    label = 'Высокий' if score >= 65 else 'Средний' if score >= 30 else 'Низкий'
    return {'ai_priority_score': score, 'ai_priority_label': label,
            'ai_priority_reason': '; '.join(reasons), 'ai_priority_version': PRIORITY_VERSION}


def priority_key(row):
    return (-ai_priority(row)['ai_priority_score'], row.get('ai_audit_status') == 'error', row['username'])


def selection_bucket(row):
    if row.get('assessment_stale'):
        return 'stale'
    metric = row.get('metric_status', row.get('status'))
    if metric == 'error' or row.get('status') == 'error':
        return 'access'
    if metric == 'rejected':
        return 'metrics'
    if row.get('channel_type') in NON_TARGET:
        return 'non_target'
    if row.get('relevance') == 'mismatch':
        return 'topic'
    if row.get('status') in ('top', 'recommended'):
        return 'qualified'
    if row.get('ai_audit_status') in ('error', 'unavailable', 'missing_key', 'model_unavailable', 'rate_limit', 'input_too_large'):
        return 'ai_error'
    if row.get('ai_queued'):
        return 'ai_waiting'
    return 'manual_review'


def selection_summary(rows):
    counts = Counter(selection_bucket(row) for row in rows)
    return [{'Этап / главная причина': label, 'Каналов': counts[code],
             'Доля, %': round(100 * counts[code] / len(rows), 1) if rows else 0}
            for code, label in BUCKETS.items() if counts[code]]


def relevant_offers(row):
    review = row.get('api_ai_review')
    return review.get('offers', []) if review and not row.get('assessment_stale') else row.get('offers', [])
