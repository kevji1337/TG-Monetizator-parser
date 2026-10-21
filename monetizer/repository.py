"""SQLite source of truth. Connections are short-lived and thread-local by use."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .audit import normalize_username

WORK_STATUSES = {"new": "Не разобран", "qualified": "Подходит", "unsuitable": "Не подходит",
                 "contacted": "Написал", "replied": "Ответил", "declined": "Отказ"}


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def content_hash(row):
    return hashlib.sha256(dumps({k: row.get(k) for k in ("username", "title", "about_text", "about_links", "content_samples", "audited_at")}).encode()).hexdigest()


class Repository:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, started_at TEXT, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS channels(username TEXT PRIMARY KEY, data TEXT NOT NULL,
                    updated_at TEXT, work_status TEXT NOT NULL DEFAULT 'new', notes TEXT NOT NULL DEFAULT '', draft TEXT);
                CREATE TABLE IF NOT EXISTS run_channels(run_id TEXT, username TEXT, data TEXT NOT NULL,
                    PRIMARY KEY(run_id,username));
                CREATE TABLE IF NOT EXISTS audits(username TEXT, audited_at TEXT, data TEXT NOT NULL,
                    PRIMARY KEY(username,audited_at));
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, username TEXT, created_at TEXT, kind TEXT, data TEXT);
                CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY, run_id TEXT, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS ai_reviews(batch_id TEXT, username TEXT, data TEXT NOT NULL,
                    PRIMARY KEY(batch_id,username));
                CREATE TABLE IF NOT EXISTS imports(digest TEXT PRIMARY KEY, batch_id TEXT, imported_at TEXT);
                CREATE INDEX IF NOT EXISTS idx_events_user ON events(username,id);
                CREATE TABLE IF NOT EXISTS api_ai_cache(cache_key TEXT PRIMARY KEY, created_at REAL NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS api_ai_attempts(id INTEGER PRIMARY KEY, run_id TEXT NOT NULL,
                    endpoint TEXT NOT NULL, cache_key TEXT NOT NULL, started_at REAL NOT NULL,
                    status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '');
                CREATE INDEX IF NOT EXISTS idx_api_ai_run ON api_ai_attempts(run_id);
                CREATE INDEX IF NOT EXISTS idx_api_ai_time ON api_ai_attempts(endpoint,started_at);
                PRAGMA user_version=1;
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def save_state(self, state):
        if not state.get("run_id"):
            return
        with self.connect() as db:
            db.execute("INSERT INTO runs VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                       (state["run_id"], state.get("started_at", ""), dumps(state)))
            db.execute("INSERT OR REPLACE INTO meta VALUES('active_run_id',?)", (state["run_id"],))
            for row in state.get("rows", []):
                username = normalize_username(row["username"])
                draft = {k: row.get(k) for k in ("pitch", "pitch_scenario", "pitch_source", "pitch_rule_version", "pitch_edited")} if row.get("pitch_edited") else None
                # Public snapshots never overwrite CRM fields or manually edited messages.
                db.execute("INSERT INTO channels(username,data,updated_at,draft,work_status,notes) VALUES(?,?,?,?,?,?) "
                           "ON CONFLICT(username) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at",
                           (username, dumps(row), row.get("audited_at", ""), dumps(draft) if draft else None,
                            row.get("work_status", "new") if row.get("work_status", "new") in WORK_STATUSES else "new", row.get("notes", "")))
                db.execute("INSERT OR REPLACE INTO run_channels VALUES(?,?,?)", (state["run_id"], username, dumps(row)))
                if row.get("audited_at"):
                    db.execute("INSERT OR IGNORE INTO audits VALUES(?,?,?)", (username, row["audited_at"], dumps(row)))

    def import_checkpoint(self, path):
        path = Path(path)
        key = "checkpoint:" + str(path.resolve())
        with self.connect() as db:
            if db.execute("SELECT 1 FROM meta WHERE key=?", (key,)).fetchone() or not path.exists():
                return
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state.get("rows"), list) or not isinstance(state.get("settings"), dict):
            raise ValueError("Некорректный старый checkpoint")
        state.setdefault("run_id", "legacy_" + hashlib.sha256(key.encode()).hexdigest()[:20])
        state["legacy"] = True
        if state.get("status") in ("running", "stopping"):
            state.update(status="interrupted", stage="Сохранён незавершённый запуск")
        self.save_state(state)
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO meta VALUES(?,?)", (key, state["run_id"]))

    def load_run(self, run_id=None):
        with self.connect() as db:
            if run_id is None:
                active = db.execute("SELECT value FROM meta WHERE key='active_run_id'").fetchone()
                run_id = active["value"] if active else None
            result = db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone() if run_id else db.execute("SELECT data FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
        if not result:
            return None
        state = json.loads(result["data"])
        state["rows"] = self.enrich_rows(state.get("rows", []))
        return state

    def enrich_rows(self, rows):
        with self.connect() as db:
            stored = {r["username"]: r for r in db.execute("SELECT username,work_status,notes,draft FROM channels")}
            reviews = {}
            for review in db.execute("SELECT username,data FROM ai_reviews ORDER BY rowid"):
                reviews[review["username"]] = json.loads(review["data"])
        result = copy.deepcopy(rows)
        for row in result:
            username = normalize_username(row["username"])
            crm = stored.get(username)
            row.update(work_status=crm["work_status"] if crm else "new", notes=crm["notes"] if crm else "")
            row["work_status_label"] = WORK_STATUSES[row["work_status"]]
            if crm and crm["draft"]:
                row.update(json.loads(crm["draft"]))
            if username in reviews:
                review = reviews[username]
                row["ai_review"] = {**review, "stale": review.get("content_hash") != content_hash(row) or
                                    review.get("analysis_context") != row.get("analysis_context")}
        return result

    def list_runs(self):
        with self.connect() as db:
            return [{"run_id": r["id"], "started_at": r["started_at"], "niche": json.loads(r["data"]).get("niche", ""),
                     "status": json.loads(r["data"]).get("status", "")} for r in db.execute("SELECT * FROM runs ORDER BY rowid DESC")]

    def list_channels(self):
        with self.connect() as db:
            rows = [json.loads(r["data"]) for r in db.execute("SELECT data FROM channels ORDER BY rowid DESC")]
        return self.enrich_rows(rows)

    def processed(self):
        with self.connect() as db:
            return {r["username"] for r in db.execute("SELECT username FROM channels")}

    def cached(self, username, ttl_hours=24):
        username = normalize_username(username)
        with self.connect() as db:
            result = db.execute("SELECT data,updated_at FROM channels WHERE username=?", (username,)).fetchone()
        if not result:
            return None
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(result["updated_at"])).total_seconds()
        except (ValueError, TypeError):
            return None
        row = json.loads(result["data"])
        if not 0 <= age <= ttl_hours * 3600 or row.get("metric_status", row.get("status")) == "error":
            return None
        return self.enrich_rows([row])[0]

    def save_crm(self, username, work_status, notes):
        if work_status not in WORK_STATUSES:
            raise ValueError("Неизвестный рабочий статус")
        username = normalize_username(username)
        with self.connect() as db:
            old = db.execute("SELECT work_status,notes FROM channels WHERE username=?", (username,)).fetchone()
            if not old:
                raise ValueError("Канал отсутствует в базе")
            if old["work_status"] == work_status and old["notes"] == notes:
                return
            db.execute("UPDATE channels SET work_status=?,notes=? WHERE username=?", (work_status, notes, username))
            db.execute("INSERT INTO events(username,created_at,kind,data) VALUES(?,?,?,?)",
                       (username, datetime.now(timezone.utc).isoformat(), "crm", dumps({"before": dict(old), "after": {"work_status": work_status, "notes": notes}})))

    def save_draft(self, username, draft):
        with self.connect() as db:
            cursor = db.execute("UPDATE channels SET draft=? WHERE username=?", (dumps(draft), normalize_username(username)))
            if not cursor.rowcount:
                raise ValueError("Канал отсутствует в базе")
            db.execute("INSERT INTO events(username,created_at,kind,data) VALUES(?,?,?,?)",
                       (normalize_username(username), datetime.now(timezone.utc).isoformat(), "draft", dumps(draft)))

    def history(self, username):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT created_at,kind,data FROM events WHERE username=? ORDER BY id DESC", (normalize_username(username),))]

    def query_history(self, scope, exclude_run_id=None):
        totals = {}
        with self.connect() as db:
            states = [json.loads(r["data"]) for r in db.execute("SELECT data FROM runs")]
        for state in states:
            if state.get("query_scope") != scope or state.get("run_id") == exclude_run_id:
                continue
            for query, stats in state.get("query_stats", {}).items():
                total = totals.setdefault(query, {"audited": 0, "qualified": 0})
                for key in total:
                    total[key] += stats.get(key, 0)
        return totals
