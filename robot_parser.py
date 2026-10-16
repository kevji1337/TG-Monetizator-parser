"""CLI entry point and backwards-compatible public audit/search imports."""
import argparse
import sys

from monetizer.audit import audit_channel_by_methodology, audit_channel_via_web
from monetizer.keywords import COURSE_NICHES, expand_keywords
from monetizer.runner import JobRunner
from monetizer.search import search_tgstat_by_methodology
from monetizer.settings import Settings


def main():
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Поиск и аудит публичных Telegram-каналов")
    parser.add_argument("--niche", help="ID ниши из monetizer/data/keyword_catalog.json")
    parser.add_argument("--topic", help="Своя тема")
    parser.add_argument("--count", type=int, default=30)
    parser.add_argument("--keys", type=int, default=12)
    parser.add_argument("--micro", help="ID узкого направления из каталога")
    parser.add_argument("--max-audits", type=int, default=0, help="0 = десятикратный лимит от цели")
    parser.add_argument("--max-minutes", type=float, default=60)
    parser.add_argument("--include-known", action="store_true", help="Включать ранее обработанные каналы, используя кэш")
    parser.add_argument("--channels", nargs="+", help="Аудит @username без TGStat")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    runner = JobRunner()
    try:
        if args.resume:
            runner.start(resume=True)
        else:
            niche_id, topic = args.niche, args.topic or ""
            if not niche_id and not topic and not args.channels:
                print("Выберите нишу:")
                for niche in COURSE_NICHES:
                    print(f"[{niche['id']}] {niche['title']}")
                print("[0] Своя тема")
                niche_id = input("Ниша [Enter = 1]: ").strip() or "1"
                if niche_id == "0":
                    topic = input("Тема: ").strip()
                count = input("Сколько уникальных каналов [Enter = 30]: ").strip()
                if count:
                    args.count = int(count)
            niche = next((n for n in COURSE_NICHES if n["id"] == niche_id), None)
            if niche_id and niche_id != "0" and not niche:
                raise ValueError("Неизвестный ID ниши")
            title = niche["title"] if niche else topic or "Заданные каналы"
            queries = expand_keywords(topic, niche_id, args.keys, args.micro)
            if not args.channels:
                print("Поисковые запросы: " + "; ".join(queries))
            runner.start(Settings(target_count=args.count, max_audits=args.max_audits, max_minutes=args.max_minutes,
                                  new_only=not args.include_known, niche_id=niche_id if niche else "", micro_id=args.micro or ""), queries, title, args.channels)
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
