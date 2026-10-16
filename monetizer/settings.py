from dataclasses import asdict, dataclass


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

    def __post_init__(self):
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
