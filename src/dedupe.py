"""SQLite deduplication and application tracking."""
import sqlite3, pathlib
from datetime import datetime

DB_PATH = pathlib.Path(__file__).parent.parent / "data" / "seen.sqlite"


def _conn():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS seen (
            url TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            source TEXT,
            first_seen TEXT,
            score REAL
        );
        CREATE TABLE IF NOT EXISTS applied (
            url TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            applied_at TEXT,
            status TEXT DEFAULT 'applied',
            notes TEXT
        );
    """)
    return conn


def filter_new(jobs: list[dict]) -> list[dict]:
    conn = _conn()
    new_jobs = []
    for job in jobs:
        url = job.get("url", "")
        if not url:
            continue
        row = conn.execute("SELECT url FROM seen WHERE url = ?", (url,)).fetchone()
        if not row:
            new_jobs.append(job)
    conn.close()
    print(f"{len(new_jobs)} new jobs (filtered {len(jobs) - len(new_jobs)} duplicates)")
    return new_jobs


def mark_seen(jobs: list[dict]):
    conn = _conn()
    now = datetime.now().isoformat()
    for job in jobs:
        url = job.get("url", "")
        if not url:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO seen (url, title, company, source, first_seen, score) VALUES (?,?,?,?,?,?)",
            (url, job.get("title", ""), job.get("company", ""), job.get("source", ""), now, job.get("score"))
        )
    conn.commit()
    conn.close()


def mark_applied(url: str, notes: str = "", title: str = "", company: str = ""):
    conn = _conn()
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO applied (url, title, company, applied_at, status, notes) VALUES (?,?,?,?,?,?)",
        (url, title, company, now, "applied", notes)
    )
    conn.commit()
    conn.close()
    print(f"Marked as applied: {url}")


def get_applied() -> list[dict]:
    conn = _conn()
    rows = conn.execute("SELECT * FROM applied ORDER BY applied_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def is_applied(url: str) -> bool:
    conn = _conn()
    row = conn.execute("SELECT url FROM applied WHERE url = ?", (url,)).fetchone()
    conn.close()
    return row is not None
