from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

AI_BASE_URL = 'https://gemini-api.osante.xyz/v1'
AI_MODEL = 'gemini-4-argon-thinking'


@dataclass(frozen=True)
class Settings:
    min_subs: int = 3000
    max_subs: int = 30000
    min_daily_reach: int = 300  # TGStat filter; not measured by t.me previews.
    min_avg_views: int = 300
    target_count: int = 30
    posts_limit: int = 15
    min_posts: int = 5
    mature_hours: int = 24
    max_gap_days: float = 4.0
    abandoned_days: int = 14
    workers: int = 4
    requests_per_second: float = 2.0
    retries: int = 2
    connect_timeout: float = 5.0
    read_timeout: float = 12.0
    login_wait_seconds: int = 0
    max_audits: int = 0  # 0 means 10 times target_count.
    max_minutes: float = 60.0
    new_only: bool = True
    niche_id: str = ""
    micro_id: str = ""
    # Historical checkpoints without these fields retain keyword mode only for resume.
    search_mode: str = 'keywords'
    category_ids: tuple[str, ...] = ()
    country_ids: tuple[str, ...] = ()
    language_ids: tuple[str, ...] = ('3',)  # Русский, actual TGStat option ID.
    channel_type: str = ''  # TGStat: any; private channels may have no public preview.
    min_age_months: int = 12
    max_age_months: int | None = None  # Slider's 120+ endpoint means unbounded.
    min_err: int = 10
    max_err: int | None = None  # Slider's 100+ endpoint means unbounded.
    min_er: float = 0.0
    min_male: int = 0
    min_female: int = 0
    tgstat_min_avg_reach: int | None = None
    tgstat_max_avg_reach: int | None = None
    max_daily_reach: int | None = None
    min_ci: float | None = None
    max_ci: float | None = None
    exclude_dead: bool = True
    exclude_scam: bool = True
    exclude_red_label: bool = True
    dev_mode: bool = False
    keep_browser_on_error: bool = True
    browser_wait_seconds: int = 300
    ai_enabled: bool = False
    ai_base_url: str = AI_BASE_URL
    ai_model: str = AI_MODEL
    ai_max_audits: int = 5
    ai_timeout: int = 600
    ai_prompt_bytes: int = 96000

    def __post_init__(self):
        endpoint = urlsplit(self.ai_base_url)
        if (endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username or endpoint.password
                or endpoint.query or endpoint.fragment):
            raise ValueError('Base URL AI: нужен HTTPS-адрес без ключа, параметров или пароля')
        if type(self.ai_enabled) is not bool or type(self.ai_max_audits) is not int or not 1 <= self.ai_max_audits <= 5:
            raise ValueError('AI: включение — флаг; лимит — от 1 до 5 запросов за запуск')
        if (not isinstance(self.ai_model, str) or not self.ai_model.strip() or len(self.ai_model) > 160
                or type(self.ai_timeout) is not int or not 10 <= self.ai_timeout <= 900):
            raise ValueError('AI: укажите Model ID и общий таймаут 10–900 секунд')
        if type(self.ai_prompt_bytes) is not int or not 16000 <= self.ai_prompt_bytes <= 120000:
            raise ValueError('Размер текста AI-запроса: 16000–120000 байт UTF-8')
        if self.search_mode not in ('categories', 'keywords', 'manual'):
            raise ValueError('Неизвестный способ поиска')
        if type(self.browser_wait_seconds) is not int or not 0 <= self.browser_wait_seconds <= 1800:
            raise ValueError('Ожидание ручного входа: от 0 до 1800 секунд')
        if (not isinstance(self.category_ids, (tuple, list)) or len(self.category_ids) > 20 or
                any(not isinstance(v, str) or not v.isascii() or not v.isdigit() for v in self.category_ids)):
            raise ValueError('Выберите до 20 тематик TGStat')
        object.__setattr__(self, 'category_ids', tuple(dict.fromkeys(self.category_ids)))
        if self.search_mode == 'categories' and not self.category_ids:
            raise ValueError('Выберите хотя бы одну тематику TGStat')
        for field in ('country_ids', 'language_ids'):
            values = getattr(self, field)
            if (not isinstance(values, (list, tuple)) or len(values) > 100 or
                    any(not isinstance(v, str) or not v.isascii() or not v.isdigit() for v in values)):
                raise ValueError('Неверный выбор страны или языка')
            object.__setattr__(self, field, tuple(dict.fromkeys(values)))
        if self.channel_type not in ('', 'public', 'private'):
            raise ValueError('Неверный тип канала')
        if (type(self.min_age_months) is not int or not 0 <= self.min_age_months <= 120 or
                self.max_age_months is not None and (type(self.max_age_months) is not int or not self.min_age_months <= self.max_age_months < 120)):
            raise ValueError('Возраст: 0–120+ месяцев; верхняя граница 120+ задаётся без ограничения')
        if (type(self.min_err) is not int or not 0 <= self.min_err <= 100 or
                self.max_err is not None and (type(self.max_err) is not int or not self.min_err <= self.max_err < 100)):
            raise ValueError('ERR: 0–100+%; верхняя граница 100+ задаётся без ограничения')
        if not 0 <= self.min_er <= 10 or self.min_er * 2 != int(self.min_er * 2):
            raise ValueError('ER: от 0 до 10+% с шагом 0.5')
        if any(type(v) is not int or not 0 <= v <= 90 or v % 5 for v in (self.min_male, self.min_female)):
            raise ValueError('Доля аудитории: от 0 до 90+% с шагом 5')
        for lower, upper in ((self.tgstat_min_avg_reach, self.tgstat_max_avg_reach),
                             (self.min_daily_reach, self.max_daily_reach), (self.min_ci, self.max_ci)):
            if ((lower is not None and lower < 0) or (upper is not None and upper < 0) or
                    lower is not None and upper is not None and lower > upper):
                raise ValueError('Проверьте нижнюю и верхнюю границы охвата / ИЦ')
        if any(type(v) is not bool for v in (self.exclude_dead, self.exclude_scam, self.exclude_red_label)):
            raise ValueError('Фильтры исключения должны быть логическими')
        if not 1 <= self.min_subs <= self.max_subs:
            raise ValueError("Проверьте диапазон подписчиков")
        if not 1 <= self.target_count <= 1000:
            raise ValueError("Количество каналов: от 1 до 1000")
        if self.min_daily_reach < 0 or self.min_avg_views < 0:
            raise ValueError("Охват и просмотры должны быть неотрицательными")
        if not 2 <= self.min_posts <= self.posts_limit <= 50:
            raise ValueError("Нужно 2–50 постов; минимальная выборка не больше лимита")
        if not 1 <= self.workers <= 8 or not 0.1 <= self.requests_per_second <= 5:
            raise ValueError("Допустимо 1–8 потоков и 0.1–5 запросов/с")
        if not 0 <= self.retries <= 3 or min(self.connect_timeout, self.read_timeout) <= 0:
            raise ValueError("Проверьте повторы и таймауты")
        if not 0 <= self.mature_hours <= 168 or not 0 < self.max_gap_days <= self.abandoned_days:
            raise ValueError("Проверьте возраст постов и пороги активности")
        if not 0 <= self.login_wait_seconds <= 180:
            raise ValueError("Пауза для входа: от 0 до 180 секунд")
        if not 0 <= self.max_audits <= 10000 or not 0 < self.max_minutes <= 1440:
            raise ValueError("Бюджет: до 10000 аудитов, время: до 1440 минут")

    @property
    def audit_budget(self):
        return self.max_audits or self.target_count * 10

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_saved(cls, data):
        """Resume old searches with their original filters, not today's default preset."""
        data = dict(data)
        if 'min_age_months' not in data:
            data.update(min_age_months=0, min_err=0, language_ids=(), channel_type='public')
        return cls(**data)
