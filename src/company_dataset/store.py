"""SQLite store: source provenance, boards, append-only verification history, field evidence.

One writer connection per process; workers hand results to the writer. Everything is keyed
so re-running an import or a stage inserts nothing twice.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  url TEXT, license TEXT, file_path TEXT, file_sha256 TEXT,
  rows_read INTEGER, imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS boards (
  id INTEGER PRIMARY KEY,
  ats TEXT NOT NULL,
  code_key TEXT NOT NULL,            -- casefolded canonical code: the board's identity
  code TEXT NOT NULL,                -- first-seen spelling
  region TEXT,
  UNIQUE (ats, code_key)
);

-- One row per source row (accepted or rejected). Rejected rows have no board.
CREATE TABLE IF NOT EXISTS source_rows (
  id INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES sources(id),
  seq INTEGER NOT NULL,              -- position in the adapter's output: rows are never collapsed
  row_ref TEXT NOT NULL,
  board_id INTEGER REFERENCES boards(id),
  ats TEXT, code TEXT, region TEXT, name TEXT, careersite_url TEXT,
  reject_reason TEXT, reject_detail TEXT,
  UNIQUE (source_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_source_rows_board ON source_rows(board_id);

-- Append-only: one row per provider check. Older evidence is never rewritten.
CREATE TABLE IF NOT EXISTS verifications (
  id INTEGER PRIMARY KEY,
  board_id INTEGER NOT NULL REFERENCES boards(id),
  checked_at TEXT NOT NULL,
  origin TEXT NOT NULL,              -- which run/cache produced it
  endpoint TEXT,
  http_status INTEGER,
  outcome TEXT NOT NULL,             -- see verification.OUTCOMES
  legacy_status TEXT, error TEXT,
  jobs_count INTEGER,
  code_validated TEXT, region_answered TEXT,
  provider_name TEXT, provider_identifier TEXT, provider_website TEXT, provider_description TEXT,
  provider_country TEXT,
  -- The same check copied into several caches is one check.
  UNIQUE (board_id, checked_at)
);
CREATE INDEX IF NOT EXISTS idx_verifications_board ON verifications(board_id, checked_at);
CREATE TRIGGER IF NOT EXISTS verifications_no_update BEFORE UPDATE ON verifications
  BEGIN SELECT RAISE(ABORT, 'verifications are append-only'); END;
CREATE TRIGGER IF NOT EXISTS verifications_no_delete BEFORE DELETE ON verifications
  BEGIN SELECT RAISE(ABORT, 'verifications are append-only'); END;

CREATE TABLE IF NOT EXISTS pages (
  url TEXT PRIMARY KEY,
  status TEXT NOT NULL, final_url TEXT, fetched_at TEXT,
  summary TEXT NOT NULL              -- JSON summary as produced by enrichment.summarize_page
);

-- Every enriched value with how it was obtained. A value without a row here is not delivered.
CREATE TABLE IF NOT EXISTS field_evidence (
  id INTEGER PRIMARY KEY,
  board_id INTEGER NOT NULL REFERENCES boards(id),
  field TEXT NOT NULL,
  value TEXT NOT NULL,
  method TEXT NOT NULL,
  evidence_url TEXT, evidence_text TEXT,
  status TEXT NOT NULL,              -- verified | candidate | rejected
  observed_at TEXT NOT NULL,
  run TEXT NOT NULL,
  UNIQUE (board_id, field, value, method, run)
);
CREATE INDEX IF NOT EXISTS idx_field_evidence_board ON field_evidence(board_id, field);

-- Identity decision per board per run, with the evidence classes it rests on.
CREATE TABLE IF NOT EXISTS identities (
  board_id INTEGER NOT NULL REFERENCES boards(id),
  run TEXT NOT NULL,
  status TEXT NOT NULL,              -- identity_confirmed | identity_unresolved | identity_mismatch | not_live
  name TEXT, basis TEXT, conflicts TEXT,
  decided_at TEXT NOT NULL,
  PRIMARY KEY (board_id, run)
);

CREATE TABLE IF NOT EXISTS companies (
  id INTEGER PRIMARY KEY,
  run TEXT NOT NULL,
  primary_board_id INTEGER NOT NULL REFERENCES boards(id),
  UNIQUE (run, primary_board_id)
);
CREATE TABLE IF NOT EXISTS company_boards (
  company_id INTEGER NOT NULL REFERENCES companies(id),
  board_id INTEGER NOT NULL REFERENCES boards(id),
  link_reason TEXT NOT NULL,         -- primary | same_domain_and_name | same_linkedin_and_name | ...
  PRIMARY KEY (company_id, board_id)
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    conn.commit()
    return conn


def backup(conn: sqlite3.Connection, dest: str | Path) -> None:
    """Consistent online copy (safe while WAL writers are active)."""
    dest = Path(dest)
    if dest.exists():
        raise FileExistsError(dest)
    out = sqlite3.connect(str(dest))
    with out:
        conn.backup(out)
    out.close()


def board_id(conn: sqlite3.Connection, ats: str, code: str, region: str | None = None) -> int:
    key = code.casefold()
    row = conn.execute("SELECT id FROM boards WHERE ats=? AND code_key=?", (ats, key)).fetchone()
    if row:
        return row[0]
    cur = conn.execute("INSERT INTO boards(ats, code_key, code, region) VALUES (?,?,?,?)", (ats, key, code, region))
    return cur.lastrowid


CONCLUSIVE = ("verified_live", "verified_live_zero_jobs", "dead_removed", "invalid_identifier", "identity_mismatch")


def latest_verifications(conn: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    """The board's current state: its newest conclusive check, else its newest check.

    A later 429 or timeout says nothing about the board, so it must not hide an earlier
    live/dead answer; it stays in the history.
    """
    marks = ",".join("?" * len(CONCLUSIVE))
    rank = f"CASE WHEN outcome IN ({marks}) THEN 1 ELSE 0 END"
    rows = conn.execute(
        f"SELECT * FROM (SELECT v.*, ROW_NUMBER() OVER (PARTITION BY board_id ORDER BY {rank} DESC, checked_at DESC, id DESC) AS rn"
        " FROM verifications v) WHERE rn=1",
        CONCLUSIVE,
    ).fetchall()
    return {r["board_id"]: r for r in rows}


def page(conn: sqlite3.Connection, url: str) -> dict | None:
    row = conn.execute("SELECT summary FROM pages WHERE url=?", (url,)).fetchone()
    return json.loads(row[0]) if row else None


def put_page(conn: sqlite3.Connection, url: str, summary: dict) -> None:
    conn.execute(
        "INSERT INTO pages(url, status, final_url, fetched_at, summary) VALUES (?,?,?,?,?)"
        " ON CONFLICT(url) DO UPDATE SET status=excluded.status, final_url=excluded.final_url,"
        " fetched_at=excluded.fetched_at, summary=excluded.summary",
        (url, summary.get("status") or "", summary.get("final_url"), summary.get("fetched_at"), json.dumps(summary, ensure_ascii=False)),
    )
