"""Operator-provided identity and terms, stored locally outside version control."""
import hashlib
import json

FIELDS = {'name': 120, 'role': 240, 'services': 800, 'verified_facts': 1200, 'terms': 500}
EMPTY_PROFILE = {'schema_version': 1, **{key: '' for key in FIELDS}}


def validate_profile(value):
    if not isinstance(value, dict) or value.get('schema_version', 1) != 1:
        raise ValueError('Неподдерживаемый профиль отправителя')
    result = {'schema_version': 1}
    for key, maximum in FIELDS.items():
        text = value.get(key, '')
        if not isinstance(text, str) or len(text) > maximum:
            raise ValueError(f'{key}: ожидается текст до {maximum} символов')
        result[key] = text.strip()
    return result


def load_profile(repo):
    with repo.connect() as db:
        row = db.execute("SELECT value FROM meta WHERE key='sender_profile'").fetchone()
    return validate_profile(json.loads(row['value'])) if row else dict(EMPTY_PROFILE)


def save_profile(repo, value):
    profile = validate_profile(value)
    with repo.connect() as db:
        db.execute("INSERT OR REPLACE INTO meta VALUES('sender_profile',?)",
                   (json.dumps(profile, ensure_ascii=False, sort_keys=True),))
    return profile


def profile_version(profile):
    return hashlib.sha256(json.dumps(validate_profile(profile), ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]
