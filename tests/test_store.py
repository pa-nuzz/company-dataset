import json
import sqlite3
from types import SimpleNamespace

import pytest

from company_dataset import ingest as store_import, store
from company_dataset.models import Reject, SourceMeta, SourceRecord


def _source(name, records, rejects=()):
    meta = SourceMeta(name=name, description="", download_url=f"https://example.org/{name}", filename="f.csv", license="MIT")
    return SimpleNamespace(META=meta, parse=lambda path: (list(records), list(rejects)))


def _raw(tmp_path, name, content="x"):
    d = tmp_path / "raw" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "f.csv").write_text(content)
    return tmp_path / "raw"


def test_every_source_row_kept_in_order_and_boards_are_casefolded(tmp_path):
    raw = _raw(tmp_path, "a")
    mod = _source("a", [
        SourceRecord("a", "greenhouse", "Acme", row_ref="line 2", name="Acme"),
        SourceRecord("a", "greenhouse", "acme", row_ref="line 3"),  # same board, different spelling: still its own source row
        SourceRecord("a", "lever", "acme", region="eu", row_ref="line 4"),
    ], [Reject("a", "unsupported_ats", "ADP", "line 5")])
    conn = store.connect(tmp_path / "s.db")
    rep = store_import.import_sources(conn, raw, "2026-10-31T00:00:00Z", modules=[mod])
    assert rep["a"]["records"] == 3 and rep["a"]["rejects"] == 1
    assert conn.execute("SELECT count(*) FROM source_rows").fetchone()[0] == 4
    assert conn.execute("SELECT count(*) FROM boards").fetchone()[0] == 2
    assert conn.execute("SELECT region FROM boards WHERE ats='lever'").fetchone()[0] == "eu"
    assert conn.execute("SELECT reject_reason FROM source_rows WHERE board_id IS NULL").fetchone()[0] == "unsupported_ats"


def test_reimport_is_idempotent_and_changed_file_is_refused(tmp_path):
    raw = _raw(tmp_path, "a")
    mod = _source("a", [SourceRecord("a", "greenhouse", "acme", row_ref="1")])
    conn = store.connect(tmp_path / "s.db")
    store_import.import_sources(conn, raw, "t", modules=[mod])
    assert store_import.import_sources(conn, raw, "t", modules=[mod])["a"]["status"] == "already_imported"
    assert conn.execute("SELECT count(*) FROM source_rows").fetchone()[0] == 1
    (raw / "a" / "f.csv").write_text("changed")
    with pytest.raises(RuntimeError, match="different file"):
        store_import.import_sources(conn, raw, "t", modules=[mod])


def _cache(tmp_path, name, data):
    p = tmp_path / f"{name}.json"
    p.write_text(json.dumps(data))
    return p


def test_cache_import_dedupes_copied_checks_and_counts_orphans(tmp_path):
    raw = _raw(tmp_path, "a")
    conn = store.connect(tmp_path / "s.db")
    store_import.import_sources(conn, raw, "t", modules=[_source("a", [SourceRecord("a", "greenhouse", "acme", row_ref="1")])])
    rec = {"ats": "greenhouse", "code": "acme", "status": "valid", "http_status": 200, "jobs_count": 3, "company_name": "Acme", "checked_at": "2026-10-31T10:00:00Z"}
    orphan = {**rec, "code": "gone"}
    s1 = store_import.import_validation_cache(conn, _cache(tmp_path, "c1", {"greenhouse:acme": rec, "greenhouse:gone": orphan}), "c1")
    s2 = store_import.import_validation_cache(conn, _cache(tmp_path, "c2", {"greenhouse:acme": rec}), "c2")
    assert s1 == {"entries": 2, "inserted": 1, "no_source_row": 1}
    assert s2 == {"entries": 1, "already_present": 1}
    row = conn.execute("SELECT outcome, origin, endpoint FROM verifications").fetchone()
    assert tuple(row) == ("verified_live", "c1", "https://boards-api.greenhouse.io/v1/boards/acme/jobs")


def test_verifications_are_append_only(tmp_path):
    raw = _raw(tmp_path, "a")
    conn = store.connect(tmp_path / "s.db")
    store_import.import_sources(conn, raw, "t", modules=[_source("a", [SourceRecord("a", "greenhouse", "acme", row_ref="1")])])
    rec = {"status": "invalid", "http_status": 404, "error": "board_not_found", "checked_at": "2026-10-31T10:00:00Z"}
    store_import.import_validation_cache(conn, _cache(tmp_path, "c", {"greenhouse:acme": rec}), "c")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE verifications SET outcome='verified_live'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM verifications")


def test_later_rate_limit_does_not_hide_earlier_conclusive_answer(tmp_path):
    raw = _raw(tmp_path, "a")
    conn = store.connect(tmp_path / "s.db")
    store_import.import_sources(conn, raw, "t", modules=[_source("a", [
        SourceRecord("a", "workable", "acme", row_ref="1"), SourceRecord("a", "workable", "gone", row_ref="2")])])
    data = {
        "workable:acme": {"status": "valid", "http_status": 200, "jobs_count": 2, "checked_at": "2026-10-31T10:00:00Z"},
        "workable:gone": {"status": "valid", "http_status": 200, "jobs_count": 2, "checked_at": "2026-10-31T10:00:00Z"},
    }
    later = {
        "workable:acme": {"status": "unresolved", "http_status": 429, "error": "http_429", "checked_at": "2026-10-31T12:00:00Z"},
        "workable:gone": {"status": "invalid", "http_status": 404, "error": "board_not_found", "checked_at": "2026-10-31T12:00:00Z"},
    }
    store_import.import_validation_cache(conn, _cache(tmp_path, "c1", data), "c1")
    store_import.import_validation_cache(conn, _cache(tmp_path, "c2", later), "c2")
    ids = {r["code_key"]: r["id"] for r in conn.execute("SELECT id, code_key FROM boards")}
    state = store.latest_verifications(conn)
    assert state[ids["acme"]]["outcome"] == "verified_live"   # 429 later: earlier answer stands
    assert state[ids["gone"]]["outcome"] == "dead_removed"    # newer conclusive answer wins
    assert conn.execute("SELECT count(*) FROM verifications").fetchone()[0] == 4  # history kept


def test_backup_is_a_consistent_copy_and_never_overwrites(tmp_path):
    conn = store.connect(tmp_path / "s.db")
    store.put_page(conn, "https://acme.com", {"status": "ok", "final_url": "https://acme.com/", "fetched_at": "t"})
    conn.commit()
    store.backup(conn, tmp_path / "b.db")
    copy = sqlite3.connect(tmp_path / "b.db")
    assert copy.execute("SELECT status FROM pages").fetchone()[0] == "ok"
    with pytest.raises(FileExistsError):
        store.backup(conn, tmp_path / "b.db")


def test_wal_and_foreign_keys_enabled(tmp_path):
    conn = store.connect(tmp_path / "s.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO verifications(board_id, checked_at, origin, outcome) VALUES (999, 't', 'x', 'verified_live')")
