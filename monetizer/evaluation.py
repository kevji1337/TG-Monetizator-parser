"""Human labels and measured classifier quality, separate from CRM and AI reviews."""
import hashlib
import json

from .audit import normalize_username
from .keywords import CATALOG_VERSION
from .qualification import CHANNEL_TYPES, RULE_VERSION, qualify
from .repository import content_hash, dumps
from .pitches import evidence_sources


def context_hash(row):
    return hashlib.sha256(dumps(row.get('analysis_context', {})).encode()).hexdigest()


def _labels(repo):
    with repo.connect() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='quality_labels'").fetchone()
        if not exists:
            return {}
        return {(r['username'], r['data_hash'], r['context_hash']): json.loads(r['data'])
                for r in db.execute('SELECT * FROM quality_labels')}


def review_set(repo, limit=100):
    """Unlabelled templates are not a benchmark. Existing matching human labels survive export."""
    labels, channels = _labels(repo), []
    for row in sorted(repo.list_channels(), key=lambda r: r['username'])[:limit]:
        username, digest, context = normalize_username(row['username']), content_hash(row), context_hash(row)
        key = (username, digest, context)
        channels.append({'channel_id': username, 'content_hash': digest, 'context_hash': context,
                         'title': row.get('title', ''), 'analysis_context': row.get('analysis_context', {}),
                         'metrics': row.get('metrics', {}), 'metric_status': row.get('metric_status', row.get('status')),
                         'sources': evidence_sources(row),
                         'label': labels.get(key, {'channel_type': None, 'relevance': None, 'qualified': None,
                                                   'annotator': '', 'note': ''})})
    identity = [(r['channel_id'], r['content_hash'], r['context_hash']) for r in channels]
    return {'schema_version': 1, 'dataset_id': hashlib.sha256(dumps(identity).encode()).hexdigest(),
            'purpose': 'Ручная разметка реальных каналов. null означает: ещё не оценён.',
            'channels': channels}


def validate_labels(repo, payload):
    if isinstance(payload, bytes):
        if len(payload) > 5_000_000:
            raise ValueError('Разметка превышает 5 МБ')
        payload = payload.decode('utf-8-sig')
    if isinstance(payload, str):
        if len(payload) > 5_000_000:
            raise ValueError('Разметка превышает 5 МБ')
        try:
            payload = json.loads(payload)
        except ValueError as exc:
            raise ValueError('Нужен JSON без Markdown') from exc
    current = review_set(repo)
    if not isinstance(payload, dict) or payload.get('schema_version') != 1 or payload.get('dataset_id') != current['dataset_id']:
        raise ValueError('Чужой или устаревший набор разметки; выгрузите текущий')
    if not isinstance(payload.get('channels'), list) or len(payload['channels']) > 100:
        raise ValueError('Ожидается не более 100 каналов')
    index = {c['channel_id']: c for c in current['channels']}
    result, seen = [], set()
    for item in payload['channels']:
        if not isinstance(item, dict):
            raise ValueError('Некорректная запись канала')
        username = item.get('channel_id')
        if not isinstance(username, str) or username not in index or username in seen:
            raise ValueError('Чужой или повторный канал')
        seen.add(username)
        original = index[username]
        if any(item.get(k) != original[k] for k in ('content_hash', 'context_hash')):
            raise ValueError('Тексты или направление изменились после выгрузки')
        label = item.get('label')
        if not isinstance(label, dict):
            raise ValueError('Отсутствует label')
        if all(label.get(k) is None for k in ('channel_type', 'relevance', 'qualified')):
            continue
        if (not isinstance(label.get('channel_type'), str) or label['channel_type'] not in CHANNEL_TYPES or label.get('relevance') not in ('matched', 'mismatch', 'unknown')
                or type(label.get('qualified')) is not bool):
            raise ValueError('Нужно заполнить тип, релевантность и qualified=true/false')
        for field in ('annotator', 'note'):
            if not isinstance(label.get(field), str) or not label[field].strip() or len(label[field]) > 2000:
                raise ValueError('Укажите автора разметки и основание решения')
        if label['qualified'] and (label['channel_type'] != 'expert' or label['relevance'] != 'matched'):
            raise ValueError('Подходящий канал должен быть личным экспертом по выбранной теме')
        result.append({'channel_id': username, 'content_hash': original['content_hash'],
                       'context_hash': original['context_hash'], 'label': {k: label[k] for k in
                            ('channel_type', 'relevance', 'qualified', 'annotator', 'note')}})
    return result


def save_labels(repo, payload):
    labels = validate_labels(repo, payload)  # Validate every row before starting a write transaction.
    with repo.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS quality_labels(username TEXT, data_hash TEXT, context_hash TEXT, data TEXT, '
                   'PRIMARY KEY(username,data_hash,context_hash))')
        for row in labels:
            db.execute('INSERT OR REPLACE INTO quality_labels VALUES(?,?,?,?)',
                       (row['channel_id'], row['content_hash'], row['context_hash'], dumps(row['label'])))
    return len(labels)


def evaluate(repo):
    labels = _labels(repo)
    counts = {'tp': 0, 'fp': 0, 'fn': 0, 'tn': 0}
    errors, total, type_correct, relevance_correct = [], 0, 0, 0
    for row in repo.list_channels():
        username = normalize_username(row['username'])
        label = labels.get((username, content_hash(row), context_hash(row)))
        if not label:
            continue
        result = qualify(row, **row.get('analysis_context', {}))
        predicted = result['status'] in ('top', 'recommended')
        actual = label['qualified']
        counts['tp' if predicted and actual else 'fp' if predicted else 'fn' if actual else 'tn'] += 1
        total += 1
        type_correct += result['channel_type'] == label['channel_type']
        relevance_correct += result['relevance'] == label['relevance']
        if predicted != actual or result['channel_type'] != label['channel_type'] or result['relevance'] != label['relevance']:
            errors.append({'channel_id': username, 'predicted_qualified': predicted, 'human_qualified': actual,
                           'predicted_type': result['channel_type'], 'human_type': label['channel_type'],
                           'predicted_relevance': result['relevance'], 'human_relevance': label['relevance'],
                           'reason': result['reasons'], 'human_note': label['note']})
    def ratio(numerator, denominator):
        return numerator / denominator if denominator else None
    return {'rule_version': RULE_VERSION, 'catalog_version': CATALOG_VERSION, 'labelled': total,
            'stored_labels': len(labels), 'stale_labels': len(labels) - total, **counts,
            'precision': ratio(counts['tp'], counts['tp'] + counts['fp']),
            'recall': ratio(counts['tp'], counts['tp'] + counts['fn']),
            'type_accuracy': ratio(type_correct, total), 'relevance_accuracy': ratio(relevance_correct, total),
            'errors': errors, 'interpretation': 'Оценка только по ручной разметке; не показатель всей выдачи TGStat.'}
