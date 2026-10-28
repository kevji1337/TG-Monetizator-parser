"""HTML parsing is pure; all public-web transport is isolated in WebAuditor."""
from __future__ import annotations

import re
import statistics
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter

from .settings import Settings
from .view_metrics import assess_post_views


class Cancelled(Exception):
    pass


class AuditError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def normalize_username(value):
    value = str(value).strip()
    if value.startswith(("https://", "http://")):
        parts = urlsplit(value)
        if (parts.hostname or "").lower() not in ("t.me", "www.t.me", "telegram.me"):
            raise ValueError("Ожидается публичная ссылка t.me или @username")
        path = parts.path.strip("/").split("/")
        value = path[1] if path[0] == "s" and len(path) > 1 else path[0]
    value = value.lstrip("@")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", value):
        raise ValueError("Некорректный публичный username")
    return value.lower()


def parse_count(text):
    """Telegram counters: 1,234 / 3 500 / 4.5K / 1,2M / 5 тыс."""
    text = re.sub(r"[\s\u200b]", "", str(text)).lower()
    match = re.fullmatch(r"([0-9]+(?:[.,][0-9]+)*)(k|m|тыс\.?|млн\.?)?", text)
    if not match:
        return None
    number, suffix = match.groups()
    if suffix:
        number = number.replace(",", ".")
        multiplier = 1_000_000 if suffix in ("m", "млн", "млн.") else 1000
    else:
        # Unabbreviated subscriber counts often use comma grouping.
        number = number.replace(",", "").replace(".", "")
        multiplier = 1
    try:
        return int(float(number) * multiplier)
    except ValueError:
        return None


def utc_date(value):
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None:
            return None  # Do not guess timezone for changed markup.
        return date.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        return None


def _links(nodes):
    found = []
    for node in nodes:
        if node is None:
            continue
        values = [a.get("href", "") for a in node.select("a[href]")]
        values += re.findall(r"https?://[^\s<>\"']+", node.get_text(" ", strip=True))
        for value in values:
            value = value.rstrip(".,);]")
            if value.startswith("//"):
                value = "https:" + value
            if value.startswith(("https://", "http://")) and value not in found:
                found.append(value)
    return found


def _platform(url, domains):
    host = (urlsplit(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def _contact_candidates(about, username):
    if about is None:
        return []
    values = re.findall(r"(?<!\w)@([A-Za-z][A-Za-z0-9_]{3,31})", about.get_text(" ", strip=True))
    for a in about.select("a[href]"):
        href = a.get("href", "")
        if _platform(href, ("t.me", "telegram.me")):
            path = urlsplit(href).path.strip("/").split("/")
            if len(path) == 1 and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", path[0]):
                values.append(path[0])
    return list(dict.fromkeys("@" + v.lower() for v in values
                              if v.lower() != username and not v.lower().endswith("bot")))


def error_row(username, code, message):
    return {"username": "@" + username, "title": username, "link": f"https://t.me/{username}",
            "status": "error", "verdict": "⚪ Не удалось проверить", "error_code": code,
            "error": str(message), "reasons": str(message), "subs": None, "avg_views": None,
            "median_views": None, "view_ratio": None, "norm": None, "freq": "Нет данных",
            "last_post": "", "youtube": "", "instagram": "", "author_contact": "",
            "contact_status": "Не установлен", "producer_status": "Не установлено",
            "post_count": 0, "eligible_posts": 0, "pitch": ""}


def parse_channel_html(html, username, settings=None, now=None):
    settings = settings or Settings()
    username = normalize_username(username)
    now = now or datetime.now(timezone.utc)
    soup = BeautifulSoup(html, "html.parser")
    info = soup.select_one(".tgme_channel_info")
    if info is None:
        return error_row(username, "preview_unavailable", "Нет публичной ленты: канал закрыт, удалён или превью ограничено")
    title_el = info.select_one(".tgme_channel_info_header_title, .tgme_channel_info_title")
    title = title_el.get_text(" ", strip=True) if title_el else username
    about = info.select_one(".tgme_channel_info_description")
    subs = None
    for counter in info.select(".tgme_channel_info_counter"):
        label = counter.select_one(".counter_type")
        if label and re.search(r"subscribers|подписчик", label.get_text(), re.I):
            val = counter.select_one(".counter_value")
            subs = parse_count(val.get_text()) if val else None
            break
    posts = {}
    for message in soup.select(".tgme_widget_message[data-post]"):
        post_id = message.get("data-post", "")
        if post_id.split("/")[0].lower() != username:
            continue
        date_el = message.select_one(".tgme_widget_message_date time[datetime]")
        date = utc_date(date_el.get("datetime")) if date_el else None
        views_el = message.select_one(".tgme_widget_message_views")
        views = parse_count(views_el.get_text()) if views_el else None
        posts[post_id] = {"id": post_id, "date": date, "views": views, "node": message}
    # Sort explicitly; do not rely on Telegram's DOM order or on pinned posts.
    dated = sorted((p for p in posts.values() if p["date"] is not None),
                   key=lambda p: p["date"], reverse=True)[:settings.posts_limit]
    valid_dates = [p["date"] for p in dated if p["date"] <= now]
    observed_views = [p["views"] for p in dated if p["views"] is not None and p['date'] <= now]
    mean_views = statistics.mean(observed_views) if observed_views else None
    avg = round(mean_views, 1) if mean_views is not None else None
    median = round(statistics.median(observed_views), 1) if observed_views else None
    exact_ratio = mean_views / subs * 100 if mean_views is not None and subs else None
    ratio = round(exact_ratio, 1) if exact_ratio is not None else None
    norm = float(settings.min_post_view_ratio)
    last_date = max(valid_dates) if valid_dates else None
    days_since = (now - last_date).total_seconds() / 86400 if last_date else None
    interval = None
    if len(valid_dates) >= 2:
        interval = (max(valid_dates) - min(valid_dates)).total_seconds() / 86400 / (len(valid_dates) - 1)
    freq = f"1 пост в {interval:.1f} дн." if interval is not None else "Недостаточно дат"
    if interval is not None and 0 < interval < 1:
        freq = f"{1 / interval:.1f} постов/день"
    links = _links([about, *(p["node"].select_one(".tgme_widget_message_text") for p in dated)])
    youtube = [u for u in links if _platform(u, ("youtube.com", "youtu.be"))]
    instagram = [u for u in links if _platform(u, ("instagram.com",))]
    sales = [u for u in links if _platform(u, ("getcourse.ru", "getcourse.io", "calendly.com", "taplink.cc", "taplink.ru"))]
    contacts = _contact_candidates(about, username)
    samples = []
    for post in dated:
        text_node = post["node"].select_one(".tgme_widget_message_text")
        text = re.sub(r"\s+", " ", text_node.get_text(" ", strip=True)) if text_node else ""
        system_text = re.fullmatch(r"(?:pinned (?:a |the )?(?:photo|video|message)|"
                                  r"закрепил[аи]? (?:фотографию|видео|сообщение))[.!]?", text, re.I)
        if len(text) >= 20 and not system_text:
            samples.append({"id": post["id"], "url": f"https://t.me/{post['id']}",
                            "date": post["date"].isoformat(), "text": text[:4000],
                            "forwarded": bool(post["node"].select_one(".tgme_widget_message_forwarded_from")),
                            "links": _links([text_node])[:8]})
    row = {"username": "@" + username, "title": title, "link": f"https://t.me/{username}",
           "subs": subs, "avg_views": avg, "median_views": median, "view_ratio": ratio, "norm": norm,
           "freq": freq, "avg_gap_days": round(interval, 2) if interval is not None else None,
           "last_post": last_date.isoformat() if last_date else "",
           "days_since_last": round(days_since, 2) if days_since is not None else None,
           "post_count": len(dated), "eligible_posts": len(observed_views),
           "youtube": youtube[0] if youtube else "", "instagram": instagram[0] if instagram else "",
           "external_links": " | ".join(links), "sales_links": " | ".join(sales),
           "author_contact": contacts[0] if contacts else "", "contact_candidates": " | ".join(contacts),
           "contact_status": "Кандидат из описания; проверить роль" if contacts else "Не установлен",
           "producer_status": "Не установлено", "audited_at": now.isoformat(),
           "error_code": "", "error": "", "pitch": "",
           "about_text": about.get_text(" ", strip=True)[:3000] if about else "",
           "about_links": _links([about]), "avg_views_exact": mean_views, "view_ratio_exact": exact_ratio,
           "content_samples": samples,
           'post_observations': [{'id': p['id'], 'date': p['date'].isoformat(), 'views': p['views'],
                                  'observed_at': now.isoformat(), 'subs_at_observation': subs} for p in dated]}
    failures, uncertain = assess_post_views(row, settings)
    if subs is None:
        uncertain.append("Не распознано число подписчиков")
    elif not settings.min_subs <= subs <= settings.max_subs:
        failures.append(f"Подписчики вне диапазона {settings.min_subs}–{settings.max_subs}")
    if days_since is not None and days_since > settings.abandoned_days:
        failures.append(f"Заброшен: более {settings.abandoned_days} дней без постов")
    if len(observed_views) < settings.min_posts:
        uncertain.append(f"Мало постов с просмотрами: {len(observed_views)}/{settings.min_posts}")
    else:
        if mean_views < settings.min_avg_views:
            failures.append(f"Средние просмотры поста ниже {settings.min_avg_views}")
    if days_since is None:
        uncertain.append("Не распознана дата последнего поста")
    elif settings.max_gap_days < days_since <= settings.abandoned_days:
        uncertain.append(f"Последний пост старше {settings.max_gap_days:g} дней")
    if interval is None:
        uncertain.append("Недостаточно дат для частоты публикаций")
    elif interval > settings.max_gap_days:
        uncertain.append(f"Публикации реже раза в {settings.max_gap_days:g} дней")
    if len(dated) != len([p for p in dated if p["date"] <= now]):
        uncertain.append("В ленте обнаружены даты из будущего")
    if failures:
        status, verdict = "rejected", "🚩 Не прошёл критерии"
        if any("Заброшен" in f for f in failures):
            verdict = "🚩 Заброшен"
    elif uncertain:
        status, verdict = "review", "🟡 Требует проверки"
    elif youtube or instagram:
        status, verdict = "top", "🟢 ТОП (Ссылки YouTube/Insta)"
    else:
        status, verdict = "recommended", "🟢 РЕКОМЕНДОВАН"
    row.update(status=status, verdict=verdict, reasons="; ".join(failures + uncertain) or "Метрики соответствуют критериям")
    return row


def reassess_metrics(row, settings, now=None):
    """Reapply changed filters to cached metrics without another HTTP request."""
    if row.get("metric_status", row.get("status")) == "error":
        return row
    row = dict(row)
    failures, uncertain = assess_post_views(row, settings)
    subs = row.get("subs")
    mean_views = row.get("avg_views_exact", row.get("avg_views"))
    norm = float(settings.min_post_view_ratio)
    row["norm"] = norm
    last = utc_date(row.get("last_post"))
    days = ((now or datetime.now(timezone.utc)) - last).total_seconds() / 86400 if last else None
    if days is not None:
        row["days_since_last"] = round(days, 2)
    if subs is None:
        uncertain.append("Не распознано число подписчиков")
    elif not settings.min_subs <= subs <= settings.max_subs:
        failures.append("Подписчики вне выбранного диапазона")
    if row.get("eligible_posts", 0) < settings.min_posts or mean_views is None:
        uncertain.append("Мало постов с просмотрами для расчёта среднего")
    else:
        if mean_views < settings.min_avg_views:
            failures.append("Средние просмотры ниже выбранного порога")
    if days is None or days < 0:
        uncertain.append("Дата последнего поста не подтверждена")
    elif days > settings.abandoned_days:
        failures.append("Заброшен: более 14 дней без постов")
    elif days > settings.max_gap_days:
        uncertain.append("Последний пост старше порога регулярности")
    if row.get("avg_gap_days") is None or row["avg_gap_days"] > settings.max_gap_days:
        uncertain.append("Частота публикаций не соответствует критерию")
    row["metric_status"] = "rejected" if failures else "review" if uncertain else "top" if row.get("youtube") or row.get("instagram") else "recommended"
    row["metric_reasons"] = "; ".join(failures + uncertain) or "Метрики соответствуют критериям"
    return row


class RateLimiter:
    """One shared rate and cooldown for the whole batch, including retries."""
    def __init__(self, rate, stop):
        self.interval = 1 / rate
        self.stop = stop
        self.lock = threading.Lock()
        self.next_at = 0.0

    def acquire(self):
        while not self.stop.is_set():
            with self.lock:
                delay = self.next_at - time.monotonic()
                if delay <= 0:
                    self.next_at = time.monotonic() + self.interval
                    return
            self.stop.wait(min(delay, .25))
        raise Cancelled()

    def pause(self, seconds):
        with self.lock:
            self.next_at = max(self.next_at, time.monotonic() + seconds)


def retry_after_seconds(value):
    try:
        return max(0, float(value))
    except (TypeError, ValueError):
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 2.0


class WebAuditor:
    def __init__(self, settings=None, stop=None):
        self.settings = settings or Settings()
        self.stop = stop or threading.Event()
        self.limiter = RateLimiter(self.settings.requests_per_second, self.stop)
        self.local = threading.local()
        self.sessions, self.lock = [], threading.Lock()
        self.blocked_until = 0.0

    def _session(self):
        if not hasattr(self.local, "session"):
            session = requests.Session()
            session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36"})
            session.mount("https://", HTTPAdapter(pool_connections=2, pool_maxsize=2, max_retries=0))
            self.local.session = session
            with self.lock:
                self.sessions.append(session)
        return self.local.session

    def close(self):
        for session in self.sessions:
            session.close()

    def fetch(self, username):
        settings = self.settings
        for attempt in range(settings.retries + 1):
            if time.monotonic() < self.blocked_until:
                raise AuditError("http_429", "Telegram запросил длительную паузу; повторите аудит позже")
            self.limiter.acquire()
            if time.monotonic() < self.blocked_until:
                raise AuditError("http_429", "Telegram запросил длительную паузу; повторите аудит позже")
            try:
                with self._session().get(f"https://t.me/s/{username}", stream=True,
                                         timeout=(settings.connect_timeout, settings.read_timeout)) as response:
                    if response.status_code == 429:
                        delay = retry_after_seconds(response.headers.get("Retry-After"))
                        # Avoid occupying every worker for a long server cooldown.
                        if delay > 60:
                            self.blocked_until = time.monotonic() + delay
                        else:
                            self.limiter.pause(delay)
                        if delay > 60 or attempt == settings.retries:
                            raise AuditError("http_429", f"Telegram ограничил запросы; повтор через {delay:.0f} с")
                        continue
                    if response.status_code in (500, 502, 503, 504) and attempt < settings.retries:
                        self.limiter.pause(2 ** attempt)
                        continue
                    if response.status_code != 200:
                        raise AuditError(f"http_{response.status_code}", f"HTTP {response.status_code}")
                    if (urlsplit(response.url).hostname or "").lower() not in ("t.me", "www.t.me"):
                        raise AuditError("redirect", "Неожиданное перенаправление превью")
                    chunks, size = [], 0
                    deadline = time.monotonic() + 30
                    for chunk in response.iter_content(65536):
                        if self.stop.is_set():
                            raise Cancelled()
                        size += len(chunk)
                        if size > 3_000_000 or time.monotonic() > deadline:
                            raise AuditError("response_limit", "Превышен размер или время загрузки превью")
                        chunks.append(chunk)
                    return b"".join(chunks).decode("utf-8", errors="replace")
            except requests.RequestException as exc:
                if attempt == settings.retries:
                    raise AuditError("network", f"Сетевая ошибка: {type(exc).__name__}") from exc
                self.limiter.pause(2 ** attempt)
        raise AuditError("retry_exhausted", "Исчерпаны попытки")

    def audit(self, username):
        username = normalize_username(username)
        try:
            row = parse_channel_html(self.fetch(username), username, self.settings)
            row["audited_at"] = datetime.now(timezone.utc).isoformat()
            return row
        except Cancelled:
            raise
        except AuditError as exc:
            return error_row(username, exc.code, str(exc))
        except Exception as exc:
            return error_row(username, "parse_error", f"Ошибка разбора: {type(exc).__name__}: {exc}")


def audit_channel_by_methodology(username, min_subs=3000, max_subs=30000, **kwargs):
    auditor = WebAuditor(Settings(min_subs=min_subs, max_subs=max_subs, **kwargs))
    try:
        return auditor.audit(username)
    finally:
        auditor.close()


audit_channel_via_web = audit_channel_by_methodology
