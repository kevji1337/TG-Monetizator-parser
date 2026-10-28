"""Explicit, quota-bound AI experiments; results never overwrite candidate reviews."""
import copy
import json
import threading
from dataclasses import replace
from uuid import uuid4

from .ai_audit import ProxyAuditor, request_data, response_prompt, cache_key, effective_settings
from .ai_responses import latest_response
from .profile import load_profile
from .repository import content_hash
from .runner import RunGuard
from .settings import Settings
from .workspace import Workspace
from .runtime import shared_state
from concurrent.futures import Future


def preview_request(row, settings, topic, profile):
    settings = effective_settings(settings)
    data = request_data(row, settings, topic, profile)
    prompt = response_prompt(data, cache_key(data, settings))
    return {'model': settings.ai_model, 'stage': settings.ai_analysis_stage, 'bytes': len(prompt.encode('utf-8')),
            'sources': len(data['sources']), 'truncated': any(s.get('truncated') for s in data['sources']),
            'within_limit': len(prompt.encode('utf-8')) <= settings.ai_prompt_bytes, 'data': data, 'prompt': prompt}


def run_experiment(runner, row, api_key, kind, models, variants=None, fields=None, config=None, stop=None):
    if runner.snapshot()['status'] in ('running', 'stopping'):
        raise ValueError('Сначала завершите текущую задачу')
    if not api_key.strip():
        raise ValueError('Введите API-ключ в панели')
    if kind not in ('models', 'prompts', 'repair') or not 1 <= len(models) <= 2:
        raise ValueError('Неверный эксперимент')
    state = runner.snapshot()
    if not state.get('run_id'):
        raise ValueError('Откройте сохранённый запуск')
    allowed = {k: v for k, v in (config or {}).items() if k in ('ai_base_url', 'ai_timeout', 'ai_prompt_bytes', 'ai_max_audits')}
    original = replace(Settings.from_saved(state['settings']), **allowed)
    base, experiment = None, {'kind': kind}
    profile = load_profile(runner.repo)
    if kind == 'repair':
        previous = latest_response(runner.repo, row['username'], row.get('ai_request_key'))
        if previous is None:
            raise ValueError('Нет сохранённого исходного ответа')
        raw = previous['content'].strip()
        if raw.startswith('```'):
            raw = raw.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        decoded = json.loads(raw)
        keys = ('channel_id', 'input_hash', 'channel_type', 'relevance', 'reason', 'evidence', 'offers', 'message')
        base = {k: decoded[k] for k in keys if k in decoded}
        if not fields or not set(fields) <= set(keys) - {'channel_id', 'input_hash'}:
            raise ValueError('Выберите поля исправления')
        current = request_data(row, original, state.get('niche', ''), profile)
        if previous['context'].get('sources') != current['sources'] or previous['context'].get('topic') != current['topic']:
            raise ValueError('Исходные тексты изменились: выполните новый полный разбор')
        experiment.update(base=base, fields=list(fields))
    results = []
    stop = stop or threading.Event()
    common = request_data(row, original, state.get('niche', ''), profile)
    with RunGuard(runner.repo.path):
        for index, model in enumerate(models):
            if stop.is_set():
                break
            settings = replace(original, ai_enabled=True, ai_model=model, ai_screen_model='', ai_deep_model='',
                               ai_prompt_variant=(variants or ['baseline'] * len(models))[index])
            auditor = ProxyAuditor(runner.repo, settings, api_key, stop, runner.log)
            # Same run ID and durable reservation: comparisons also spend the five-call cap.
            prepared = {**common, 'prompt_variant': settings.ai_prompt_variant}
            result = auditor.audit(copy.deepcopy(row), state['run_id'], state.get('niche', ''), profile, experiment=experiment, prepared_data=prepared)
            results.append({'model': model, 'variant': settings.ai_prompt_variant,
                            'status': result.get('ai_audit_status'), 'detail': result.get('ai_detail', ''),
                            'review': result.get('api_ai_review') if result.get('ai_audit_status') in ('success', 'cached') else None})
        record = {'kind': kind, 'username': row['username'], 'run_id': state['run_id'],
                  'data_hash': content_hash(row), 'results': results, 'fields': fields or []}
        Workspace(runner.repo).save('experiment', uuid4().hex, record)
    return record


def experiment_job(repo):
    state = shared_state()
    with state['lock']:
        return state.setdefault('experiments', {}).get(str(repo.path.resolve()))


def experiment_active(repo):
    job = experiment_job(repo)
    return bool(job and not job['future'].done())


def start_experiment(runner, row, api_key, kind, models, variants=None, fields=None, config=None):
    state = shared_state()
    with state['lock']:
        jobs = state.setdefault('experiments', {})
        key = str(runner.repo.path.resolve())
        if key in jobs and not jobs[key]['future'].done():
            raise ValueError('AI-эксперимент уже выполняется')
        future, stop = Future(), threading.Event()
        jobs[key] = {'future': future, 'stop': stop, 'username': row['username']}
        def work():
            try:
                future.set_result(run_experiment(runner, copy.deepcopy(row), api_key, kind, list(models), variants, fields, config, stop))
            except Exception as exc:
                future.set_exception(exc)
        thread = threading.Thread(target=work, name='monetizer-experiment', daemon=True)
        jobs[key]['thread'] = thread
        thread.start()
        return future
