"""Local operator data and descriptive analytics, independent of public snapshots."""
import json
import math
import re
import statistics
from datetime import datetime, timezone, date
from uuid import uuid4

from .audit import normalize_username

SCHEMA = '''CREATE TABLE IF NOT EXISTS workspace_records(
    kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY(kind,id));
CREATE TABLE IF NOT EXISTS workspace_events(
    id INTEGER PRIMARY KEY, kind TEXT, record_id TEXT, created_at TEXT, data TEXT);'''


class Workspace:
    def __init__(self, repo):
        self.repo = repo
        with repo.connect() as db:
            db.executescript(SCHEMA)

    def list(self, kind):
        with self.repo.connect() as db:
            return {r['id']: json.loads(r['data']) for r in db.execute(
                'SELECT id,data FROM workspace_records WHERE kind=? ORDER BY updated_at,id', (kind,))}

    def get(self, kind, key, default=None):
        with self.repo.connect() as db:
            record = db.execute('SELECT data FROM workspace_records WHERE kind=? AND id=?', (kind, key)).fetchone()
        return json.loads(record['data']) if record else ({} if default is None else default)

    def save(self, kind, key, data):
        if not isinstance(key, str) or not key.strip() or len(key) > 120:
            raise ValueError('Название: 1–120 символов')
        if kind not in ('preset', 'view', 'candidate', 'blocklist', 'draft', 'experiment', 'calibration', 'reminder'):
            raise ValueError('Неизвестный вид записи')
        if kind == 'candidate':
            key = normalize_username(key)
            data = validate_candidate(data)
        encoded = json.dumps(data, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode('utf-8')) > 500_000:
            raise ValueError('Слишком большая запись')
        if kind in ('preset', 'draft') and any(k in encoded.lower() for k in ('proxy_api_key', 'authorization', 'secret_key')):
            raise ValueError('Ключи API не сохраняются в пресетах')
        now = datetime.now(timezone.utc).isoformat()
        with self.repo.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT data FROM workspace_records WHERE kind=? AND id=?', (kind, key)).fetchone()
            if old and json.loads(old['data']) == data:
                return
            db.execute('INSERT OR REPLACE INTO workspace_records VALUES(?,?,?,?)', (kind, key, encoded, now))
            db.execute('INSERT INTO workspace_events(kind,record_id,created_at,data) VALUES(?,?,?,?)',
                       (kind, key, now, json.dumps({'before': json.loads(old['data']) if old else None, 'after': data}, ensure_ascii=False)))

    def delete(self, kind, key):
        with self.repo.connect() as db:
            db.execute('DELETE FROM workspace_records WHERE kind=? AND id=?', (kind, key))


def names(value):
    parts = value.split(',') if isinstance(value, str) else list(value)
    result = list(dict.fromkeys(str(v).strip() for v in parts if str(v).strip()))
    if len(result) > 30 or any(len(v) > 50 for v in result):
        raise ValueError('До 30 тегов/подборок, каждый до 50 символов')
    return result


DEAL_STAGES = {'none': 'Нет сделки', 'talks': 'Переговоры', 'agreement': 'Договорённость',
               'active': 'Сотрудничество', 'finished': 'Завершено'}


def economics(values):
    currency = values.get('currency', 'RUB')
    if currency not in ('RUB', 'BYN', 'USD', 'EUR'):
        raise ValueError('Выберите валюту RUB, BYN, USD или EUR')
    fields = {'price': 0., 'leads': 0., 'conversion': 0., 'share': 0., 'costs': 0., 'actual_revenue': 0.}
    for key in fields:
        value = values.get(key, 0.)
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1e12:
            raise ValueError('Экономика: нужны конечные неотрицательные числа')
        fields[key] = float(value)
    if fields['conversion'] > 100 or fields['share'] > 100:
        raise ValueError('Конверсия и доля: 0–100%')
    sales = fields['leads'] * fields['conversion'] / 100
    gross = sales * fields['price']
    return {**fields, 'currency': currency, 'sales_forecast': round(sales, 2), 'gross_forecast': round(gross, 2),
            'partner_forecast': round(gross * fields['share'] / 100 - fields['costs'], 2)}


def validate_candidate(data):
    stage = data.get('deal_stage', 'none')
    if stage not in DEAL_STAGES:
        raise ValueError('Неизвестный этап сделки')
    return {'tags': names(data.get('tags', [])), 'collections': names(data.get('collections', [])),
            'deal_stage': stage, 'economics': economics(data.get('economics', {}))}


def distribution(row):
    points = [p for p in row.get('post_observations', []) if type(p.get('views')) is int and p['views'] >= 0
              and p.get('date') and p.get('observed_at')]
    mature = []
    for point in points:
        try:
            age = (datetime.fromisoformat(point['observed_at']) - datetime.fromisoformat(point['date'])).total_seconds() / 3600
            if age >= 18:
                mature.append(point)
        except (ValueError, TypeError):
            pass
    if len(mature) < 5:
        return {'sample': len(mature), 'status': 'Недостаточно постов возрастом от 18 ч', 'outliers': []}
    values = [p['views'] for p in mature]
    median = statistics.median(values)
    mad = statistics.median(abs(v - median) for v in values)
    boundary = max(median * 5, median + 6 * mad)
    unusual = [{'url': 'https://t.me/' + p['id'], 'views': p['views'], 'date': p['date']} for p in mature if p['views'] > boundary]
    return {'sample': len(values), 'median': median, 'mad': mad, 'min': min(values), 'max': max(values),
            'status': 'Есть выбросы: проверьте источники трафика' if unusual else 'Выбросы по выбранному правилу не найдены',
            'outliers': unusual}


def topic_stability(row):
    from .categories import assessment_profiles
    from .keywords import MICRO_NICHES, get_micro_niches
    from .qualification import _contains
    context = row.get('analysis_context', {})
    profiles = ([MICRO_NICHES[context['micro_id']]] if context.get('micro_id') in MICRO_NICHES else
                assessment_profiles(context['category_ids']) if context.get('category_ids') else
                get_micro_niches(context.get('niche_id', '')) if context.get('niche_id') else [])
    terms = list(dict.fromkeys(t for p in profiles for t in p['match_terms']))
    posts = sorted((p for p in row.get('content_samples', []) if not p.get('forwarded')), key=lambda p: p.get('date', ''))
    if not terms or len(posts) < 4:
        return {'status': 'Недостаточно текстов или не задано направление', 'posts': []}
    details = [{'url': p['url'], 'date': p.get('date', ''),
                'terms': [t for t in terms if _contains(t, p['text'])]} for p in posts]
    midpoint = len(details) // 2
    share = lambda items: sum(bool(p['terms']) for p in items) / len(items) * 100
    old, new = share(details[:midpoint]), share(details[midpoint:])
    return {'status': 'Тематические термины встречаются в доле публикаций; это индикатор, не вердикт',
            'coverage': round(share(details), 1), 'earlier': round(old, 1), 'recent': round(new, 1),
            'change': round(new - old, 1), 'posts': details}


def search_eta(state):
    rows = state.get('rows', [])
    seconds = state.get('elapsed_seconds', 0)
    good = sum(r.get('status') in ('top', 'recommended') and not r.get('assessment_stale') for r in rows)
    target = state.get('settings', {}).get('target_count', 30)
    ai = state.get('settings', {}).get('ai_enabled', False)
    ready = good + len(state.get('ai_queue', [])) if ai else good
    if ready >= target:
        return {'text': 'Очередь кандидатов заполнена; окончательный отбор зависит от AI и его лимита' if ai else 'Цель достигнута', 'minutes': 0}
    if ready < 2 or len(rows) < 10 or seconds <= 0:
        return {'text': 'Пока недостаточно подходящих каналов для оценки времени', 'minutes': None}
    needed = math.ceil((target - ready) * len(rows) / ready)
    return {'text': ('До заполнения очереди кандидатов; окончательный отбор зависит от AI и его лимита. ' if ai else '') + 'Оценка по текущей скорости, без гарантии доступности выдачи',
            'minutes': round(needed * seconds / len(rows) / 60, 1), 'additional_audits': needed}


def funnel(repo, store):
    rows = repo.list_channels()
    with repo.connect() as db:
        events = db.execute("SELECT username,data FROM events WHERE kind='crm'").fetchall()
    contacted, replied = set(), set()
    for event in events:
        after = json.loads(event['data']).get('after', {}).get('work_status')
        if after in ('contacted', 'replied', 'declined'):
            contacted.add(event['username'])
        if after == 'replied':
            replied.add(event['username'])
    for row in rows:
        user = normalize_username(row['username'])
        if row['work_status'] in ('contacted', 'replied', 'declined'):
            contacted.add(user)
        if row['work_status'] == 'replied':
            replied.add(user)
    attrs = store.list('candidate')
    agreed = {u for u, data in attrs.items() if data.get('deal_stage') in ('agreement', 'active', 'finished')}
    return {'Всего в базе': len(rows), 'Написали, когда-либо': len(contacted), 'Ответили, когда-либо': len(replied),
            'Договорённости, текущие этапы': len(agreed), 'Доля ответов, %': round(len(replied & contacted) / len(contacted) * 100, 1) if contacted else None,
            'Договорённости среди контактов, %': round(len(agreed & contacted) / len(contacted) * 100, 1) if contacted else None}


def reminder_calendar(rows, minutes=60):
    if type(minutes) is not int or not 1 <= minutes <= 10080:
        raise ValueError('Опережение напоминания: 1–10080 минут')
    def escape(value):
        return str(value).replace('\\', '\\\\').replace('\n', '\\n').replace(';', '\\;').replace(',', '\\,').replace('\r', '')
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//TG Monetizator//Contacts//RU']
    for row in rows:
        if not row.get('next_contact_date'):
            continue
        day = date.fromisoformat(row['next_contact_date']).strftime('%Y%m%d')
        lines += ['BEGIN:VEVENT', 'UID:contact-' + normalize_username(row['username']) + '@tg-monetizator.local',
                  'DTSTAMP:' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
                  'DTSTART:' + day + 'T090000', 'SUMMARY:' + escape(row.get('next_action_label', 'Контакт') + ' ' + row['username']),
                  'DESCRIPTION:' + escape(row.get('contact_plan_note', '')), 'BEGIN:VALARM', 'ACTION:DISPLAY',
                  f'TRIGGER:-PT{int(minutes)}M', 'DESCRIPTION:Контакт с автором', 'END:VALARM', 'END:VEVENT']
    # RFC 5545 folds lines at 75 octets, without splitting a UTF-8 character.
    folded = []
    for line in lines + ['END:VCALENDAR', '']:
        chunk = ''
        for char in line:
            if len((chunk + char).encode('utf-8')) > 75:
                folded.append(chunk)
                chunk = ' '
            chunk += char
        folded.append(chunk)
    return '\r\n'.join(folded)
