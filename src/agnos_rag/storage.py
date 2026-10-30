"""SQLite source of truth, content-change detection and crawl audit log."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from agnos_rag.schemas import ForumPost

Outcome = Literal["new", "updated", "unchanged"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS scrape_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('running','completed','partial','failed','interrupted')),
    requested_limit INTEGER,
    pages_fetched INTEGER NOT NULL DEFAULT 0,
    new_count INTEGER NOT NULL DEFAULT 0,
    updated_count INTEGER NOT NULL DEFAULT 0,
    unchanged_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL,
    question TEXT NOT NULL,
    published_at TEXT,
    age INTEGER,
    gender TEXT,
    disease_key TEXT,
    disease TEXT,
    tags_json TEXT NOT NULL,
    doctor_answers_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('answered','pending')),
    content_hash TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen_run_id INTEGER NOT NULL REFERENCES scrape_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);
CREATE TABLE IF NOT EXISTS scrape_errors (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES scrape_runs(id),
    page INTEGER,
    forum_id INTEGER,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);
PRAGMA user_version = 1;
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


class Store:
    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.connection.close()
            raise ValueError(f"Unsupported database schema version: {version}")
        self.connection.executescript(SCHEMA)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.connection.close()

    def start_run(self, limit: int | None) -> int:
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO scrape_runs(started_at,status,requested_limit) VALUES (?, 'running', ?)",
                (utc_now(), limit),
            )
        return cursor.lastrowid

    def upsert(self, post: ForumPost, run_id: int) -> Outcome:
        now = utc_now()
        tags = json.dumps([tag.model_dump() for tag in post.tags], ensure_ascii=False)
        answers = json.dumps([answer.model_dump() for answer in post.doctor_answers], ensure_ascii=False)
        with self.connection:
            existing = self.connection.execute(
                "SELECT content_hash FROM posts WHERE id = ?", (post.id,)
            ).fetchone()
            outcome: Outcome = (
                "new"
                if existing is None
                else "unchanged"
                if existing["content_hash"] == post.content_hash
                else "updated"
            )
            self.connection.execute(
                """INSERT INTO posts (
                    id,url,question,published_at,age,gender,disease_key,disease,tags_json,
                    doctor_answers_json,status,content_hash,first_seen_at,last_seen_at,updated_at,
                    last_seen_run_id
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    url=excluded.url, question=excluded.question, published_at=excluded.published_at,
                    age=excluded.age, gender=excluded.gender, disease_key=excluded.disease_key,
                    disease=excluded.disease, tags_json=excluded.tags_json,
                    doctor_answers_json=excluded.doctor_answers_json, status=excluded.status,
                    content_hash=excluded.content_hash, last_seen_at=excluded.last_seen_at,
                    updated_at=CASE WHEN posts.content_hash != excluded.content_hash
                        THEN excluded.updated_at ELSE posts.updated_at END,
                    last_seen_run_id=excluded.last_seen_run_id""",
                (
                    post.id,
                    post.url,
                    post.question,
                    post.published_at,
                    post.age,
                    post.gender,
                    post.disease_key,
                    post.disease,
                    tags,
                    answers,
                    "answered" if post.has_answer else "pending",
                    post.content_hash,
                    now,
                    now,
                    now,
                    run_id,
                ),
            )
        return outcome

    def record_error(
        self, run_id: int, page: int, kind: str, detail: str, forum_id: int | None = None
    ) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO scrape_errors(run_id,page,forum_id,kind,detail) VALUES (?,?,?,?,?)",
                (run_id, page, forum_id, kind, detail),
            )

    def finish_run(self, run_id: int, status: str, pages: int, counts: dict[str, int]) -> dict:
        with self.connection:
            self.connection.execute(
                """UPDATE scrape_runs SET finished_at=?, status=?, pages_fetched=?,
                   new_count=?, updated_count=?, unchanged_count=?, failed_count=? WHERE id=?""",
                (
                    utc_now(),
                    status,
                    pages,
                    counts["new"],
                    counts["updated"],
                    counts["unchanged"],
                    counts["failed"],
                    run_id,
                ),
            )
        return dict(self.connection.execute("SELECT * FROM scrape_runs WHERE id=?", (run_id,)).fetchone())

    def stats(self) -> dict:
        totals = dict(
            self.connection.execute(
                """SELECT COUNT(*) AS posts,
               COALESCE(SUM(status='answered'),0) AS answered,
               COALESCE(SUM(status='pending'),0) AS pending FROM posts"""
            ).fetchone()
        )
        latest = self.connection.execute("SELECT * FROM scrape_runs ORDER BY id DESC LIMIT 1").fetchone()
        totals["latest_run"] = dict(latest) if latest else None
        totals["latest_errors"] = (
            [
                dict(row)
                for row in self.connection.execute(
                    "SELECT page,forum_id,kind,detail FROM scrape_errors WHERE run_id=? ORDER BY id LIMIT 10",
                    (latest["id"],),
                )
            ]
            if latest
            else []
        )
        return totals
