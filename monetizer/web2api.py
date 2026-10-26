"""Client for zexadev/gemini-web2api-go (v4.23.0 and newer).

Only final answer text is retained. Reasoning is counted for progress, never logged
or persisted. Importing this module does not contact the proxy.
"""
import asyncio
import json
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

MAX_WIRE_BYTES = 4_000_000
MAX_ANSWER_BYTES = 1_000_000
MODEL_ID = re.compile(r'[A-Za-z0-9._:/-]{1,160}\Z')


class ProxyError(Exception):
    """Locally composed error, safe to display without exposing upstream text."""
    def __init__(self, detail, *, disable=False, status='error'):
        super().__init__(detail)
        self.detail, self.disable, self.status = detail, disable, status


def base_url(value):
    parts = urlsplit(value)
    if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment):
        raise ValueError('Base URL AI: нужен HTTPS-адрес без ключа, параметров или пароля')
    return value.rstrip('/')


def safe_id(value):
    return value if isinstance(value, str) and MODEL_ID.fullmatch(value) else ''


def proxy_error(status, body=None, retry_after=None):
    error = body.get('error', {}) if isinstance(body, dict) else {}
    if not isinstance(error, dict):
        error = {}
    code, kind = error.get('code'), error.get('type')
    # Inspect upstream messages only to choose a fixed description. Never echo them.
    message = error.get('message', '')
    message = message.casefold() if isinstance(message, str) else ''
    detail = f'web2api: HTTP {status}'
    if status in (401, 403):
        detail += ' — доступ отклонён; проверьте API-ключ и настройки доступа к прокси'
    elif code == 'model_downgraded':
        detail += ' — аккаунты не смогли ответить выбранной моделью (model_downgraded)'
    elif code == 'ip_slot_full' or kind == 'rate_limit_exceeded':
        detail += ' — занят IP-слот или исчерпан его лимит (ip_slot_full)'
    elif status == 429:
        detail += ' — ограничение частоты или квоты; подробности в Requests web2api'
    elif code == 'context_length_exceeded':
        detail += ' — превышен лимит текста прокси; уменьшите размер AI-запроса'
    elif status == 400 and any(s in message for s in ('cookie', 'paid', 'not available', 'unknown model')):
        detail += ' — модель недоступна; проверьте Model ID и Cookie Pool'
    elif any(s in message for s in ('google.com/sorry', 'blocked by google')):
        detail += ' — Google заблокировал выходной IP прокси'
    elif any(s in message for s in ('timeout', 'deadline exceeded', 'timed out')):
        detail += ' — истёк upstream timeout на стороне web2api'
    elif status >= 500 or kind == 'upstream_error':
        detail += ' — сбой web2api или соединения с Gemini; подробности в Requests web2api'
    elif 300 <= status < 400:
        detail += ' — получена переадресация; проверьте Base URL, переход не выполнялся'
    else:
        detail += ' — некорректный запрос или ответ прокси'
    if isinstance(retry_after, str) and retry_after.isascii() and retry_after.isdigit():
        seconds = int(retry_after[:8])
        detail += f'; повтор не раньше чем через {seconds} с'
    return ProxyError(detail, disable=status in (401, 403, 429) or status >= 500 or kind == 'upstream_error')


async def _bounded_body(response, maximum):
    content = bytearray()
    async for chunk in response.aiter_bytes():
        content.extend(chunk)
        if len(content) > maximum:
            raise ProxyError('Ответ web2api превышает допустимый размер', disable=True)
    return bytes(content)


def _json(content):
    try:
        value = json.loads(content)
    except (ValueError, UnicodeDecodeError):
        raise ProxyError('web2api вернул некорректный JSON или HTML вместо ответа API', disable=True) from None
    if not isinstance(value, dict):
        raise ProxyError('Ответ web2api должен быть объектом JSON', disable=True)
    return value


async def _events(response):
    """Read bounded SSE events, including split UTF-8, CRLF and data lines."""
    pending, data, size = bytearray(), [], 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > MAX_WIRE_BYTES:
            raise ProxyError('Поток web2api превышает 4 МБ', disable=True)
        pending.extend(chunk)
        while b'\n' in pending:
            line, _, rest = pending.partition(b'\n')
            pending = bytearray(rest)
            line = line.rstrip(b'\r')
            if not line:
                if data:
                    yield b'\n'.join(data)
                    data = []
            elif line.startswith(b'data:'):
                data.append(line[5:].lstrip(b' '))
    # A clean stream from this proxy ends with a complete [DONE] event. Do not
    # salvage a partial final event after a disconnected or truncated response.


def _usage(value):
    return {k: value[k] for k in ('prompt_tokens', 'completion_tokens', 'reasoning_tokens', 'total_tokens')
            if isinstance(value, dict) and type(value.get(k)) is int and value[k] >= 0}


@dataclass
class ChatReply:
    content: str
    response_model: str = ''  # Echoed request name; NOT the backend's actual model.
    request_id: str = ''
    usage: dict | None = None
    elapsed_seconds: float = 0.0
    first_event_seconds: float | None = None


async def _chat(endpoint, key, model, prompt, progress):
    timeout = httpx.Timeout(None, connect=5.0, write=15.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        async with client.stream('POST', endpoint + '/chat/completions',
                                 headers={'Authorization': 'Bearer ' + key, 'Accept': 'text/event-stream'},
                                 json={'model': model, 'messages': [{'role': 'user', 'content': prompt}],
                                       'stream': True, 'stream_options': {'include_usage': True}}) as response:
            if response.status_code != 200:
                raw = await _bounded_body(response, 64_000)
                try:
                    body = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    body = None
                raise proxy_error(response.status_code, body, response.headers.get('Retry-After'))
            progress['phase'] = 'Поток открыт, ожидаю данные модели'
            if 'text/event-stream' not in response.headers.get('content-type', '').lower():
                body = _json(await _bounded_body(response, MAX_WIRE_BYTES))
                if body.get('error'):
                    raise proxy_error(200, body)
                choices = body.get('choices')
                if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                    raise ProxyError('В JSON web2api нет choices', disable=True)
                choice = choices[0]
                message = choice.get('message')
                content = message.get('content') if isinstance(message, dict) else None
                if choice.get('finish_reason') != 'stop' or not isinstance(content, str) or not content.strip():
                    raise ProxyError('web2api не вернул завершённый текстовый ответ')
                if len(content.encode('utf-8')) > MAX_ANSWER_BYTES:
                    raise ProxyError('Текст ответа AI превышает 1 МБ')
                progress['phase'] = 'Получен обычный JSON вместо SSE'
                return ChatReply(content, safe_id(body.get('model')), safe_id(body.get('id')), _usage(body.get('usage')))
            parts, answer_bytes, finished, done = [], 0, False, False
            reply = ChatReply('')
            async for raw in _events(response):
                if progress.get('first_event') is None:
                    progress['first_event'] = time.monotonic()
                if raw == b'[DONE]':
                    done = True
                    break
                event = _json(raw)
                if event.get('error'):
                    raise proxy_error(200, event)
                reply.response_model = safe_id(event.get('model')) or reply.response_model
                reply.request_id = safe_id(event.get('id')) or reply.request_id
                if 'usage' in event:
                    reply.usage = _usage(event['usage'])
                choices = event.get('choices')
                if choices == []:  # include_usage and upstream-error events have no choices.
                    continue
                if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                    raise ProxyError('Некорректная структура события SSE web2api')
                choice = choices[0]
                if choice.get('index', 0) != 0:
                    raise ProxyError('Неожиданный номер ответа SSE web2api')
                delta = choice.get('delta')
                if not isinstance(delta, dict):
                    raise ProxyError('В событии web2api нет delta')
                reasoning = delta.get('reasoning_content')
                if reasoning is not None:
                    if not isinstance(reasoning, str) or finished:
                        raise ProxyError('Некорректный фрагмент reasoning SSE web2api')
                    progress['reasoning_chars'] += len(reasoning)
                    progress['phase'] = 'Модель думает; получаю reasoning'
                content = delta.get('content')
                if content is not None:
                    if not isinstance(content, str) or finished:
                        raise ProxyError('Некорректный фрагмент ответа SSE web2api')
                    answer_bytes += len(content.encode('utf-8'))
                    if answer_bytes > MAX_ANSWER_BYTES:
                        raise ProxyError('Текст ответа AI превышает 1 МБ')
                    parts.append(content)
                    progress['answer_chars'] += len(content)
                    progress['phase'] = 'Получаю итоговый ответ'
                if delta.get('tool_calls'):
                    raise ProxyError('AI вернул вызов инструмента вместо анализа канала')
                if choice.get('finish_reason') is not None:
                    if choice['finish_reason'] != 'stop' or finished:
                        raise ProxyError('Модель не завершила ответ обычным finish_reason=stop')
                    finished = True
            if not (done and finished):
                raise ProxyError('Поток web2api оборвался до finish_reason=stop и [DONE]; ответ не применён', disable=True)
            reply.content = ''.join(parts)
            if not reply.content.strip():
                raise ProxyError('AI завершил поток без текста ответа')
            return reply


async def _controlled(operation, stop, seconds, progress=None, log=None):
    """Enforce a wall-clock budget even before headers or during a silent stream."""
    started, last_log, last_phase = time.monotonic(), 0.0, ''
    task = asyncio.create_task(operation)
    try:
        while True:
            if stop is not None and stop.is_set():
                raise ProxyError('AI-запрос остановлен; соединение закрыто', status='stopped')
            remaining = seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise ProxyError('Истёк общий таймаут AI; соединение закрыто, повтор не отправлялся', disable=True)
            completed, _ = await asyncio.wait({task}, timeout=min(.25, remaining))
            if completed:
                return task.result()
            if progress is not None and log is not None:
                now, phase = time.monotonic(), progress['phase']
                if phase != last_phase or now - last_log >= 15:
                    counters = f"; reasoning: {progress['reasoning_chars']} симв.; ответ: {progress['answer_chars']} симв."
                    log(f"{phase}; прошло {now - started:.0f} с" + counters)
                    last_log, last_phase = now, phase
    except httpx.TimeoutException:
        raise ProxyError('Таймаут подключения или отправки данных в web2api; повтор не отправлялся', disable=True) from None
    except httpx.HTTPError:
        raise ProxyError('Ошибка соединения с web2api; повтор не отправлялся', disable=True) from None
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def chat_completion(endpoint, key, model, prompt, stop, seconds, log):
    endpoint = base_url(endpoint)
    started = time.monotonic()
    progress = {'phase': 'Ожидаю начало ответа web2api', 'reasoning_chars': 0,
                'answer_chars': 0, 'first_event': None}
    reply = asyncio.run(_controlled(_chat(endpoint, key, model, prompt, progress), stop, seconds, progress, log))
    reply.elapsed_seconds = round(time.monotonic() - started, 3)
    if progress['first_event'] is not None:
        reply.first_event_seconds = round(progress['first_event'] - started, 3)
    return reply


async def _inspect(endpoint, key):
    result = {'version': '', 'health_ok': False, 'models': [], 'models_checked': False,
              'auth_checked': False, 'models_http': None, 'notes': []}
    root = endpoint[:-3] + '/' if endpoint.endswith('/v1') else endpoint + '/'
    async with httpx.AsyncClient(timeout=httpx.Timeout(8, connect=5), follow_redirects=False) as client:
        for name, url, headers in [('health', root, {}), ('models', endpoint + '/models',
                                    {'Authorization': 'Bearer ' + key} if key else {})]:
            if name == 'models' and not key:
                continue
            try:
                async with client.stream('GET', url, headers=headers) as response:
                    if name == 'models':
                        result['models_http'] = response.status_code
                    if response.status_code != 200:
                        result['notes'].append(f'{name}: HTTP {response.status_code}')
                        continue
                    body = _json(await _bounded_body(response, 256_000))
                if name == 'health':
                    result['health_ok'] = body.get('status') == 'ok'
                    version = body.get('version', '')
                    result['version'] = version if isinstance(version, str) and re.fullmatch(r'[0-9A-Za-z.+-]{1,40}', version) else ''
                    models = body.get('models')
                    if isinstance(models, list):
                        result.update(models=sorted({safe_id(v) for v in models if safe_id(v)}), models_checked=True)
                elif isinstance(body.get('data'), list):
                    result.update(models=sorted({safe_id(v.get('id')) for v in body['data']
                                                 if isinstance(v, dict) and safe_id(v.get('id'))}),
                                  models_checked=True, auth_checked=True)
            except (httpx.HTTPError, ProxyError):
                result['notes'].append(f'{name}: не удалось получить метаданные API')
    return result


def inspect_proxy(endpoint, key='', stop=None, seconds=20):
    """GET metadata only; no chat creation and no generation quota consumed."""
    endpoint = base_url(endpoint)
    return asyncio.run(_controlled(_inspect(endpoint, key.strip()), stop, seconds))
