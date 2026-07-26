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
        CREATE TABLE IF NOT EXISTS replayed (
            replay_id TEXT NOT NULL,
            url TEXT NOT NULL,
            processed_at TEXT NOT NULL,
            PRIMARY KEY (replay_id, url)
        );
    """)
    return conn


def _key_of(job: dict) -> str:
    return job.get("dedup_key") or job.get("url", "")


def filter_new(
    jobs: list[dict],
    *,
    replay_sources: tuple[str, ...] = (),
    replay_id: str | None = None,
) -> list[dict]:
    normalized_sources = {
        str(source).strip().casefold()
        for source in replay_sources
        if str(source).strip()
    }
    replay_id = str(replay_id or "").strip() or None
    if normalized_sources and replay_id is None:
        raise ValueError("replay_id is required when replay_sources are set")
    conn = _conn()
    new_jobs = []
    for job in jobs:
        key = _key_of(job)
        if not key:
            continue
        source = str(job.get("source", "")).strip().casefold()
        if source in normalized_sources:
            replayed = conn.execute(
                "SELECT 1 FROM replayed WHERE replay_id = ? AND url = ?",
                (replay_id, key),
            ).fetchone()
            if not replayed:
                new_jobs.append(job)
            continue
        row = conn.execute("SELECT url FROM seen WHERE url = ?", (key,)).fetchone()
        if not row:
            new_jobs.append(job)
    conn.close()
    print(f"{len(new_jobs)} new jobs (filtered {len(jobs) - len(new_jobs)} duplicates)")
    return new_jobs


def mark_seen(
    jobs: list[dict],
    *,
    replay_sources: tuple[str, ...] = (),
    replay_id: str | None = None,
):
    conn = _conn()
    now = datetime.now().isoformat()
    normalized_sources = {
        str(source).strip().casefold()
        for source in replay_sources
        if str(source).strip()
    }
    replay_id = str(replay_id or "").strip() or None
    for job in jobs:
        key = _key_of(job)
        if not key:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO seen (url, title, company, source, first_seen, score) VALUES (?,?,?,?,?,?)",
            (key, job.get("title", ""), job.get("company", ""), job.get("source", ""), now, job.get("score"))
        )
        if (
            replay_id is not None
            and str(job.get("source", "")).strip().casefold()
            in normalized_sources
        ):
            conn.execute(
                "INSERT OR IGNORE INTO replayed "
                "(replay_id, url, processed_at) VALUES (?,?,?)",
                (replay_id, key, now),
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
