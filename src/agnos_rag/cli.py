"""Small CLI; JSON summaries can be read by people, scripts or cron."""

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

from agnos_rag.scraper import ForumClient, scrape
from agnos_rag.storage import Store


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def interval_seconds(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 1:
        raise argparse.ArgumentTypeError("must be at least 1 second")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agnos forum scraper and local retrieval pipeline")
    commands = parser.add_subparsers(dest="command", required=True)
    crawl = commands.add_parser("scrape", help="fetch listing data and upsert it into SQLite")
    crawl.add_argument("--db", type=Path, default=Path("data/agnos.sqlite3"))
    scope = crawl.add_mutually_exclusive_group()
    scope.add_argument("--limit", type=positive_int, help="maximum unique records to try (default: 10)")
    scope.add_argument("--all", action="store_true", help="traverse the entire listing")
    crawl.add_argument("--max-pages", type=positive_int, help="optional page cap")
    crawl.add_argument("--interval", type=interval_seconds, default=1.0, help="request spacing in seconds")
    stats = commands.add_parser("stats", help="show local counts and latest scrape run")
    stats.add_argument("--db", type=Path, default=Path("data/agnos.sqlite3"))
    args = parser.parse_args(argv)
    if args.command == "stats" and not args.db.is_file():
        parser.error(f"Database not found: {args.db}. Run scrape first.")
    try:
        with Store(args.db) as store:
            if args.command == "stats":
                result = store.stats()
            else:
                with ForumClient(interval=args.interval) as client:
                    result = scrape(
                        client,
                        store,
                        limit=None if args.all else (args.limit or 10),
                        max_pages=args.max_pages,
                    )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return int(args.command == "scrape" and result["status"] != "completed")
    except KeyboardInterrupt:
        print("Scrape interrupted; committed rows and the run log are preserved.", file=sys.stderr)
        return 130
    except (sqlite3.Error, OSError, ValueError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
