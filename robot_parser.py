"""CLI entry point and backwards-compatible public audit/search imports."""
import argparse
import sys

from monetizer.audit import audit_channel_by_methodology, audit_channel_via_web
from monetizer.keywords import MICRO_NICHES
from monetizer.categories import load_categories
from monetizer.runner import JobRunner
from monetizer.search import search_tgstat_by_methodology
from monetizer.settings import Settings


def main():
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Поиск и аудит публичных Telegram-каналов")
    parser.add_argument('--categories', '--category', nargs='+', help='ID или точные названия тематик самого TGStat')
    parser.add_argument('--list-categories', action='store_true', help='Показать сохранённый список тематик TGStat')
    parser.add_argument('--niche', help=argparse.SUPPRESS)  # Do not silently reinterpret old internal IDs as TGStat IDs.
    parser.add_argument('--topic', help=argparse.SUPPRESS)
    parser.add_argument("--count", type=int, default=30)
    parser.add_argument("--micro", help="Необязательный фокус аудита по содержанию; не передаётся в поиск TGStat")
    parser.add_argument("--max-audits", type=int, default=0, help="0 = десятикратный лимит от цели")
    parser.add_argument("--max-minutes", type=float, default=60)
    parser.add_argument("--include-known", action="store_true", help="Включать ранее обработанные каналы, используя кэш")
    parser.add_argument("--channels", nargs="+", help="Аудит @username без TGStat")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument('--ai', action='store_true', help='AI-аудит Gemini через прокси; ключ из MONETIZER_AI_KEY, максимум 5 запросов')
    args = parser.parse_args()
    categories = load_categories()['categories']
    if args.list_categories:
        for category in categories:
            print(f"[{category['id']}] {category['title']}")
        return 0
    if args.niche or args.topic:
        parser.error('Новый поиск работает по тематикам TGStat: используйте --categories. Список: --list-categories')
    if args.micro and args.micro not in MICRO_NICHES:
        parser.error('Неизвестная специализация для аудита')
    runner = JobRunner()
    try:
        if args.resume:
            settings = Settings.from_saved({**runner.state.get('settings', {}), 'ai_enabled': True}) if args.ai else None
            runner.start(settings=settings, resume=True)
        else:
            selected = args.categories or []
            if not selected and not args.channels:
                print('Выберите тематики TGStat:')
                for category in categories:
                    print(f"[{category['id']}] {category['title']}")
                selected = (input('ID тематик через пробел [Enter = 34, Дизайн]: ').strip() or '34').split()
                count = input("Сколько подходящих каналов [Enter = 30]: ").strip()
                if count:
                    args.count = int(count)
            resolved = []
            for value in selected:
                category = next((c for c in categories if value == c['id'] or value.casefold() == c['title'].casefold()), None)
                if category is None:
                    raise ValueError('Неизвестная тематика TGStat: ' + value)
                if category not in resolved:
                    resolved.append(category)
            title = '; '.join(c['title'] for c in resolved) or 'Заданные каналы'
            if args.micro:
                title += ' · ' + MICRO_NICHES[args.micro]['title']
            print('Тематики TGStat: ' + title + '. Текстовые запросы не используются.')
            runner.start(Settings(target_count=args.count, max_audits=args.max_audits, max_minutes=args.max_minutes,
                                  new_only=not args.include_known, micro_id=args.micro or '',
                                  ai_enabled=args.ai,
                                  search_mode='manual' if args.channels else 'categories', category_ids=[c['id'] for c in resolved]),
                         niche=title, usernames=args.channels)
        last_log = None
        while runner.thread.is_alive():
            snapshot = runner.snapshot()
            logs = snapshot["logs"]
            start = logs.index(last_log) + 1 if last_log in logs else 0
            for line in logs[start:]:
                print(line)
            if logs:
                last_log = logs[-1]
            runner.thread.join(.5)
    except KeyboardInterrupt:
        print("Останавливаю задачу и сохраняю прогресс…")
        runner.stop()
        runner.thread.join() if runner.thread else None
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    snapshot = runner.snapshot()
    print(snapshot["stage"])
    if snapshot.get("error"):
        print(snapshot["error"], file=sys.stderr)
    for row in snapshot["rows"][:15]:
        print(f"{row['verdict']} | {row['username']} | просмотры: {row.get('avg_views')} | {row.get('reasons')}")
    for path in snapshot["paths"]:
        print(path)
    return 1 if snapshot["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
