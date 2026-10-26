"""Local final-answer diagnostics; never stores headers, keys or reasoning."""
import json
import time

SCHEMA = '''CREATE TABLE IF NOT EXISTS api_ai_responses(
    attempt_id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, username TEXT NOT NULL,
    cache_key TEXT NOT NULL, created_at REAL NOT NULL, context TEXT NOT NULL,
    content TEXT NOT NULL, metadata TEXT NOT NULL, validation_error TEXT NOT NULL DEFAULT '')'''


def save_response(repo, attempt_id, run_id, username, key, data, content, metadata):
    with repo.connect() as db:
        db.execute(SCHEMA)
        db.execute('INSERT INTO api_ai_responses(attempt_id,run_id,username,cache_key,created_at,context,content,metadata) '
                   'VALUES(?,?,?,?,?,?,?,?,?)',
                   (attempt_id, run_id, username.lstrip('@').lower(), key, time.time(),
                    json.dumps(data, ensure_ascii=False), content, json.dumps(metadata, ensure_ascii=False)))


def set_result(repo, attempt_id, detail):
    with repo.connect() as db:
        db.execute('UPDATE api_ai_responses SET validation_error=? WHERE attempt_id=?', (detail, attempt_id))


def latest_response(repo, username, key=None):
    with repo.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='api_ai_responses'").fetchone():
            return None
        query = 'SELECT * FROM api_ai_responses WHERE username=?'
        parameters = [username.lstrip('@').lower()]
        if key:
            query += ' AND cache_key=?'
            parameters.append(key)
        row = db.execute(query + ' ORDER BY attempt_id DESC LIMIT 1', parameters).fetchone()
    if row is None:
        return None
    return {**dict(row), 'context': json.loads(row['context']), 'metadata': json.loads(row['metadata'])}
