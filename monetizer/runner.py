"""Bounded discovery/audit pipeline with SQLite history and recovery checkpoint."""
import copy
import hashlib
import json
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .audit import Cancelled, WebAuditor, error_row, normalize_username, reassess_metrics
from .export import RESULTS_DIR, atomic_json, save_reports
from .keywords import unique_queries, CATALOG_VERSION
from .pitches import compose_pitch, PLAYBOOK_VERSION
from .qualification import qualify, RULE_VERSION
from .repository import Repository, dumps, content_hash
from .profile import load_profile, profile_version
from .search import TGStatSearch, SearchBudgetExceeded
from .settings import Settings
from .categories import category_key, category_label, category_id_from_key, CATEGORY_RULES_VERSION
from .tgstat_filters import filter_spec
from .diagnostics import BrowserControl, logger

RANK = {"top": 0, "recommended": 1, "review": 2, "rejected": 3, "error": 4}
STOP_LABELS = {"target_reached": "Цель достигнута", "exhausted": "Доступная выдача исчерпана",
               "restricted": "TGStat ограничил выдачу: требуется вход или достигнут лимит доступа", "audit_budget": "Достигнут лимит аудитов",
               "time_budget": "Достигнут лимит времени", "cancelled": "Остановлено пользователем",
               "manual_exhausted": "Ручной аудит завершён", "error": "Ошибка выполнения"}


def sorted_rows(rows):
    return sorted(rows, key=lambda r: (RANK.get(r.get("status"), 9), -(r.get("avg_views") or 0)))


class RunGuard:
    def __init__(self, database=None):
        self.path = Path(database).resolve().with_suffix('.lock') if database else RESULTS_DIR / 'monetizer.lock'

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.seek(0, 2)
        if not self.file.tell():
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            import os
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise RuntimeError("Другой поиск уже запущен") from exc
        return self

    def __exit__(self, *args):
        self.file.close()


class JobRunner:
    def __init__(self, checkpoint=None, database=None):
        started = time.perf_counter()
        logger().info('runner startup started')
        self.checkpoint = Path(checkpoint) if checkpoint else RESULTS_DIR / "checkpoint.json"
        self.repo = Repository(database or self.checkpoint.with_name("monetizer.sqlite3"))
        self.lock, self.stop_event = threading.Lock(), threading.Event()
        self.thread, self._active_started = None, None
        self._external_owner = False
        self._base_elapsed = 0.0
        self.browser_control = BrowserControl(self._browser_state)
        self.state = {"status": "idle", "stage": "Готов к запуску", "rows": [], "candidates": {},
                      "logs": [], "paths": [], "error": "", "search_complete": False}
        startup_guard = None
        try:
            try:
                startup_guard = RunGuard(self.repo.path)
                startup_guard.__enter__()
            except RuntimeError:
                startup_guard = None
                self._external_owner = True
                loaded = self.repo.load_run()
                if loaded:
                    self.state.update(loaded)
                return
            self.repo.import_checkpoint(self.checkpoint)
            loaded = self.repo.load_run()
            if loaded:
                Settings.from_saved(loaded["settings"])
                interrupted = loaded.get("status") in ("running", "stopping")
                if interrupted:
                    loaded.update(status="interrupted", stage="Сохранён незавершённый запуск")
                self.state.update(loaded)
                settings = Settings.from_saved(self.state["settings"])
                self._mark_pending(settings)
                if interrupted:
                    self.repo.save_state(self.state)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            self.state["error"] = f"Не удалось прочитать сохранённый запуск: {exc}"
        finally:
            if startup_guard:
                startup_guard.__exit__()
            logger().info('runner startup completed elapsed=%.3fs rows=%s pending=%s',
                          time.perf_counter() - started, len(self.state['rows']), self.state.get('assessment_pending', 0))

    def _signature(self, row, settings, profile):
        return hashlib.sha256(dumps([content_hash(row), settings.to_dict(), self.state.get('niche', ''),
                                     RULE_VERSION, CATALOG_VERSION, PLAYBOOK_VERSION, profile_version(profile),
                                     datetime.now(timezone.utc).date().isoformat()]).encode()).hexdigest()

    def _mark_pending(self, settings):
        profile = load_profile(self.repo)
        for row in self.state['rows']:
            row['assessment_stale'] = row.get('_evaluation_signature') != self._signature(row, settings, profile)
        self.state['assessment_pending'] = sum(r['assessment_stale'] for r in self.state['rows'])

    def _evaluate(self, row, settings, profile=None):
        profile = load_profile(self.repo) if profile is None else profile
        signature = self._signature(row, settings, profile)
        if row.get('_evaluation_signature') == signature:
            return {**copy.deepcopy(row), 'assessment_stale': False}
        row = qualify(reassess_metrics(row, settings), self.state.get("niche", ""), settings.niche_id,
                      settings.micro_id, settings.category_ids)
        if not row.get("pitch_edited"):
            draft = compose_pitch(row, self.state.get("niche", ""), profile=profile)
            row.update(pitch=draft["message"], pitch_scenario=draft["scenario_id"],
                       pitch_source=(draft["source"] or {}).get("id", ""), pitch_rule_version=PLAYBOOK_VERSION)
        row['_evaluation_signature'] = signature
        row['assessment_stale'] = False
        return row

    def _refresh_rows(self, settings, progress=None, cancellable=False):
        started = time.perf_counter()
        profile = load_profile(self.repo)
        refreshed, total = [], len(self.state['rows'])
        logger().info('run=%s reassessment started rows=%s', self.state.get('run_id'), total)
        for index, row in enumerate(self.state['rows'], 1):
            if cancellable and self.stop_event.is_set():
                raise Cancelled()
            refreshed.append(self._evaluate(row, settings, profile))
            if progress:
                progress(index, total)
            if index % 10 == 0 or index == total:
                logger().info('run=%s reassessment progress=%s/%s elapsed=%.3fs',
                              self.state.get('run_id'), index, total, time.perf_counter() - started)
            if cancellable:
                with self.lock:
                    self.state['stage'] = f'Пересчёт сохранённых оценок: {index}/{total}'
        with self.lock:
            self.state['rows'] = refreshed
            self.state['assessment_pending'] = 0
        logger().info('run=%s reassessment completed elapsed=%.3fs', self.state.get('run_id'), time.perf_counter() - started)

    def reassess_saved_run(self, progress=None):
        if self.state['status'] in ('running', 'stopping'):
            raise RuntimeError('Сначала остановите поиск')
        with RunGuard(self.repo.path):
            self._refresh_rows(Settings.from_saved(self.state['settings']), progress)
            self._persist()

    def _browser_state(self, state):
        with self.lock:
            self.state['browser'] = state

    def resume_browser(self):
        self.browser_control.resume.set()

    def snapshot(self):
        if self._external_owner:
            external_state = self.repo.load_run()
            if external_state:
                with self.lock:
                    self.state.update(external_state)
                    self._external_owner = external_state.get("status") in ("running", "stopping")
        with self.lock:
            state = copy.deepcopy(self.state)
            state["external_owner"] = self._external_owner
            if self._active_started is not None:
                state["elapsed_seconds"] = self._base_elapsed + time.monotonic() - self._active_started
        state["rows"] = self.repo.enrich_rows(state["rows"])
        return state

    def _stats(self):
        self.state['assessment_pending'] = sum(bool(r.get('assessment_stale')) for r in self.state['rows'])
        for row in self.state["rows"]:
            sources = self.state['candidates'].get(row['username'].lstrip('@'), [])
            row['source_queries'] = ' | '.join(q for q in sources if not q.startswith('tgstat:'))
            row['source_categories'] = ' | '.join(category_label(q) for q in sources if q.startswith('tgstat:'))
            category_sources = [category_id_from_key(q) for q in sources if q.startswith('tgstat:')]
            if not self.state.get('manual') and category_sources:
                # Site filters are recorded as provenance, not inferred from t.me preview metrics.
                row['tgstat_filters'] = filter_spec(Settings.from_saved(self.state['settings']), category_sources[0])
                row['tgstat_filters']['selects']['categories'] = category_sources
        for query, stats in self.state.get("query_stats", {}).items():
            rows = [r for r in self.state["rows"] if query in self.state["candidates"].get(r["username"].lstrip("@"), [])]
            stats.update(audited=len(rows), qualified=sum(not r.get('assessment_stale') and r["status"] in ("top", "recommended") for r in rows),
                         errors=sum(r["status"] == "error" for r in rows))
            stats["yield_percent"] = round(stats["qualified"] / len(rows) * 100, 1) if rows else 0.0
            stats["audit_seconds"] = round(sum(r.get("audit_seconds", 0) for r in rows), 3)
            stats["cache_hits"] = sum(bool(r.get("cache_hit")) for r in rows)

    def _persist(self):
        with self.lock:
            self._stats()
        state = self.snapshot()
        self.repo.save_state(state)
        atomic_json(self.checkpoint, state)

    def log(self, message):
        logger().info('run=%s %s', self.state.get('run_id', 'startup'), message)
        with self.lock:
            self.state["logs"] = (self.state["logs"] + [f"{datetime.now().strftime('%H:%M:%S')}  {message}"])[-300:]

    def start(self, settings=None, queries=None, niche="", usernames=None, resume=False):
        with self.lock:
            if self.state["status"] in ("running", "stopping") or (self.thread and self.thread.is_alive()):
                raise RuntimeError("Задача уже выполняется")
            if resume:
                if not self.state.get("settings"):
                    raise ValueError("Нет сохранённого запуска")
                settings = settings or Settings.from_saved(self.state["settings"])
            else:
                settings = settings or Settings()
                if settings.search_mode == 'categories' and not usernames:
                    if queries:
                        raise ValueError('Поиск по тематикам выполняется без текстовых запросов')
                    queries = [category_key(c) for c in settings.category_ids]
                queries = unique_queries(queries or [])
                usernames = list(dict.fromkeys(normalize_username(u) for u in (usernames or [])))
                if not queries and not usernames:
                    raise ValueError("Выберите тематики TGStat или добавьте каналы")
                if len(queries) > 50:
                    raise ValueError("Допустимо не более 50 запросов")
            guard = RunGuard(self.repo.path)
            guard.__enter__()
            if not resume:
                scope_data = {k: v for k, v in settings.to_dict().items() if k not in (
                    "target_count", "max_audits", "max_minutes", "new_only", "workers", "requests_per_second", "login_wait_seconds", "retries", "connect_timeout", "read_timeout",
                    'dev_mode', 'keep_browser_on_error', 'browser_wait_seconds')}
                self.state = {"run_id": uuid4().hex, "settings": settings.to_dict(), "queries": queries,
                              "query_scope": hashlib.sha256(dumps([niche, scope_data, RULE_VERSION, CATALOG_VERSION, CATEGORY_RULES_VERSION]).encode()).hexdigest(),
                              "niche": niche.strip(), "manual": bool(usernames), "rows": [], "logs": [], "paths": [],
                              "error": "", "search_complete": bool(usernames), "candidates": {u: ["Введён вручную"] for u in usernames},
                              "queue": list(usernames), "query_state": {}, "query_stats": {}, "elapsed_seconds": 0,
                              "started_at": datetime.now(timezone.utc).isoformat()}
            self.state["settings"] = settings.to_dict()
            self._mark_pending(settings)
            # Legacy checkpoints have no page cursor; restart discovery while keeping known candidates.
            done = {r["username"].lstrip("@") for r in self.state["rows"]}
            self.state["queue"] = [u for u in self.state["candidates"] if u not in done]
            for query in self.state.get("queries", []):
                self.state.setdefault("query_state", {}).setdefault(query, {"page": 0, "buffer": [], "exhausted": False, "blocked": False, "served": 0})
                self.state.setdefault("query_stats", {}).setdefault(query, {"found": 0, "duplicates": 0, "seconds": 0, "access_errors": 0})
                if resume and self.state["query_state"][query]["blocked"]:
                    self.state["query_state"][query].update(blocked=False, exhausted=False)
            self.stop_event.clear()
            self.browser_control = BrowserControl(self._browser_state)
            self.state.update(status="running", stage="Подготовка", error="", stop_reason="")
            self._base_elapsed = self.state.get("elapsed_seconds", 0)
            self._active_started = time.monotonic()
            self.guard = guard
            self.thread = threading.Thread(target=self._run, name="monetizer-job", daemon=True)
        try:
            self._persist()
            self.thread.start()
        except Exception:
            guard.__exit__()
            self._active_started = None
            self.state.update(status="failed", stage="Не удалось запустить задачу")
            raise

    def stop(self):
        with self.lock:
            if self.state["status"] == "running":
                self.stop_event.set()
                self.state.update(status="stopping", stage="Останавливаю операции")

    def _pick_query(self, history):
        available = [q for q in self.state.get("queries", []) if self.state["query_state"][q]["buffer"] or not self.state["query_state"][q]["exhausted"]]
        if not available:
            return None
        unseen = [q for q in available if not self.state["query_state"][q]["served"]]
        if unseen:
            return unseen[0]
        def score(q):
            stats, previous = self.state["query_stats"][q], history.get(q, {})
            return (stats.get("qualified", 0) + previous.get("qualified", 0) + 1) / (stats.get("audited", 0) + previous.get("audited", 0) + 2) + 1 / (1 + self.state["query_state"][q]["served"])
        return max(available, key=score)

    def _discover_portion(self, search, query, excluded):
        with self.lock:
            cursor, stats = copy.deepcopy(self.state["query_state"][query]), copy.deepcopy(self.state["query_stats"][query])
        if not cursor["buffer"] and not cursor["exhausted"]:
            started = time.monotonic()
            try:
                response = search.fetch(query, cursor["page"])
                names = list(dict.fromkeys(normalize_username(u) for u in response["usernames"]))
                cursor["buffer"] = names
                if not response.get("restricted"):
                    cursor["page"] += 1
                cursor["exhausted"] = response["exhausted"]
                cursor["blocked"] = response.get("restricted", False)
                stats["found"] += len(names)
                stats['without_public_username'] = stats.get('without_public_username', 0) + response.get('without_public_username', 0)
                if cursor["blocked"]:
                    stats["access_errors"] += 1
            except (Cancelled, SearchBudgetExceeded):
                raise
            except Exception as exc:
                logger().exception('run=%s category=%s discovery failed', self.state.get('run_id'), query)
                cursor.update(exhausted=True, blocked=True)
                stats["access_errors"] += 1
                self.log(f"{category_label(query)}: {type(exc).__name__}: {exc}")
            finally:
                stats["seconds"] += round(time.monotonic() - started, 3)
        portion = cursor["buffer"][:10]
        cursor["buffer"] = cursor["buffer"][10:]
        cursor["served"] += 1
        with self.lock:
            for username in portion:
                if username in self.state["candidates"]:
                    stats["duplicates"] += 1
                    if query not in self.state["candidates"][username]:
                        self.state["candidates"][username].append(query)
                elif username in excluded:
                    stats["duplicates"] += 1
                else:
                    self.state["candidates"][username] = [query]
                    self.state["queue"].append(username)
            self.state["query_state"][query], self.state["query_stats"][query] = cursor, stats
        self.log(f"{category_label(query)}: найдено {stats['found']}, повторов {stats['duplicates']}")
        self._persist()

    def _consume(self, future, username, settings):
        try:
            if future.cancelled():
                return
            row = future.result()
        except Cancelled:
            return
        except Exception as exc:
            logger().exception('run=%s audit worker failed', self.state.get('run_id'))
            row = error_row(username, "worker_error", str(exc))
        row = self._evaluate(row, settings)
        row["source_queries"] = " | ".join(self.state["candidates"][username])
        with self.lock:
            if not any(r["username"].lstrip("@") == username for r in self.state["rows"]):
                self.state["rows"].append(row)
            if username in self.state["queue"]:
                self.state["queue"].remove(username)
        self.log(f"{row['username']}: {row['verdict']}")
        self._persist()

    @staticmethod
    def _audit_one(auditor, username):
        started = time.monotonic()
        row = auditor.audit(username)
        return {**row, "audit_seconds": round(time.monotonic() - started, 3), "cache_hit": False}

    def _run(self):
        try:
            self._execute()
        except Exception as exc:
            logger().exception('run=%s task failed', self.state.get('run_id'))
            with self.lock:
                self._active_started = None
                self.state.update(status="failed", stage="Ошибка сохранения или завершения", error=f"{type(exc).__name__}: {exc}")
            try:
                self._persist()
            except Exception:
                pass  # Database failure must not leave the OS run lock held.
        finally:
            self.guard.__exit__()

    def _execute(self):
        settings = Settings.from_saved(self.state["settings"])
        auditor, search = WebAuditor(settings, self.stop_event), None
        executor, pending = ThreadPoolExecutor(max_workers=settings.workers, thread_name_prefix="audit"), {}
        reason, failure = "exhausted", ""
        deadline = self._active_started + max(0, settings.max_minutes * 60 - self._base_elapsed)
        try:
            # Resume must use current evaluations before counting the target.
            # This runs in the job thread, never inside get_runner().
            if self.state['rows']:
                self._refresh_rows(settings, cancellable=True)
            search = TGStatSearch(settings, self.stop_event, self.log, deadline, self.browser_control)
            excluded = self.repo.processed() if settings.new_only and not self.state.get("manual") else set()
            history = self.repo.query_history(self.state.get("query_scope", ""), self.state.get("run_id"))
            with self.lock:
                self.state["stage"] = "Поиск и предварительный отбор"
            while True:
                if self.stop_event.is_set():
                    raise Cancelled()
                if time.monotonic() >= deadline:
                    reason = "time_budget"
                    break
                for future in [f for f in pending if f.done()]:
                    self._consume(future, pending.pop(future), settings)
                qualified = sum(r["status"] in ("top", "recommended") for r in self.state["rows"])
                if qualified >= settings.target_count and not self.state.get("manual"):
                    reason = "target_reached"
                    break
                if len(self.state["rows"]) >= settings.audit_budget:
                    reason = "audit_budget"
                    break
                in_flight = set(pending.values())
                next_name = next((u for u in self.state["queue"] if u not in in_flight), None)
                if next_name and len(pending) < settings.workers and len(pending) + len(self.state["rows"]) < settings.audit_budget:
                    cached = self.repo.cached(next_name)
                    if cached:
                        from concurrent.futures import Future
                        future = Future()
                        future.set_result({**cached, "audit_seconds": 0.0, "cache_hit": True})
                        self._consume(future, next_name, settings)
                    else:
                        pending[executor.submit(self._audit_one, auditor, next_name)] = next_name
                    continue
                if pending and (len(pending) >= settings.workers or len(self.state["rows"]) + len(pending) >= settings.audit_budget
                                or qualified + len(pending) >= settings.target_count or self.state.get("manual")):
                    wait(pending, timeout=.25, return_when=FIRST_COMPLETED)
                    continue
                if self.state.get("manual"):
                    reason = "manual_exhausted"
                    break
                query = self._pick_query(history)
                if query is None:
                    if pending:
                        wait(pending, timeout=.25, return_when=FIRST_COMPLETED)
                        continue
                    reason = "restricted" if any(c["blocked"] for c in self.state["query_state"].values()) else "exhausted"
                    self.state["search_complete"] = True
                    break
                self._discover_portion(search, query, excluded)
        except Cancelled:
            reason = "cancelled"
        except SearchBudgetExceeded:
            reason = "time_budget"
        except Exception as exc:
            reason, failure = "error", f"{type(exc).__name__}: {exc}"
            self.log(failure)
        finally:
            if reason in ("cancelled", "time_budget", "error"):
                self.stop_event.set()
            executor.shutdown(wait=True, cancel_futures=True)
            auditor.close()
            if search:
                search.close()
            for future, username in pending.items():
                self._consume(future, username, settings)
            with self.lock:
                self.state["rows"] = sorted_rows(self.state["rows"])
                self.state["elapsed_seconds"] = self._base_elapsed + time.monotonic() - self._active_started
                self._active_started = None
                self.state.update(stop_reason=reason, stage=STOP_LABELS[reason], error=failure,
                                  status="cancelled" if reason == "cancelled" else "failed" if reason == "error" else
                                  "completed" if reason in ("target_reached", "manual_exhausted") else "partial",
                                  finished_at=datetime.now(timezone.utc).isoformat())
            try:
                self._persist()
                self._export()
                self._persist()
            except Exception as exc:
                self.state.update(status="failed", error=f"Сохранение результатов: {exc}")

    def _export(self):
        rows = sorted_rows(self.snapshot()["rows"])
        if rows:
            paths, warnings = save_reports(rows, self.state.get("niche") or "каналы")
            with self.lock:
                self.state["paths"] = paths
            for warning in warnings:
                self.log(warning)

    def save_pitch(self, username, message, scenario_id, source_id=None, context=None):
        if self.state["status"] in ("running", "stopping"):
            raise RuntimeError("Дождитесь окончания текущего поиска")
        with RunGuard(self.repo.path):
            row = next((r for r in self.state["rows"] if r["username"] == username), None)
            if row is None:
                raise ValueError("Канал отсутствует в текущем отчёте")
            if context:
                row.update({k: context[k] for k in ("about_text", "content_samples", "youtube", "instagram") if k in context})
                updated = self._evaluate(row, Settings.from_saved(self.state["settings"]))
                row.update(updated)
            draft = dict(pitch=message, pitch_edited=True, pitch_scenario=scenario_id,
                         pitch_source=source_id or "", pitch_rule_version=PLAYBOOK_VERSION)
            row.update(draft)
            self.repo.save_state(self.state)
            self.repo.save_draft(username, draft)
            self._persist()
            self._export()
            self._persist()

    def reaudit(self, username):
        if self.state["status"] in ("running", "stopping"):
            raise RuntimeError("Сначала остановите поиск")
        with RunGuard(self.repo.path):
            auditor = WebAuditor(Settings.from_saved(self.state.get("settings", {})))
            try:
                row = self._evaluate(auditor.audit(username), Settings.from_saved(self.state.get("settings", {})))
            finally:
                auditor.close()
            self.state["rows"] = [r for r in self.state["rows"] if r["username"] != row["username"]] + [row]
            self.state["candidates"].setdefault(normalize_username(username), ["Повторный аудит"])
            self._persist()
            return self.repo.enrich_rows([row])[0]

    def load_saved_run(self, run_id):
        if self.state["status"] in ("running", "stopping"):
            raise RuntimeError("Сначала остановите текущий поиск")
        loaded = self.repo.load_run(run_id)
        if loaded is None:
            raise ValueError("Запуск отсутствует")
        with RunGuard(self.repo.path):
            with self.lock:
                self.state = loaded
                if self.state["status"] in ("running", "stopping"):
                    self.state.update(status="interrupted", stage="Сохранён незавершённый запуск")
                settings = Settings.from_saved(self.state["settings"])
                self._mark_pending(settings)
            self._persist()
