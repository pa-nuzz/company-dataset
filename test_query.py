#!/usr/bin/env python3
"""Populate companies and company_boards using export module logic."""
import csv
import sys
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from company_dataset import store
from company_dataset.export import (
    cluster_companies, _missing, _companies, VERIFIED_COLUMNS
)

DB = "data/store.db"
EXPORT_LABEL = "final_2026-11-04"

def main():
    conn = store.connect(DB)
    conn.row_factory = sqlite3.Row

    # Clear
    with conn:
        conn.execute("DELETE FROM company_boards")
        conn.execute("DELETE FROM companies")
    print("Cleared tables")

    # Get all boards that are verified live AND have at least one identity_confirmed
    board_ids = [row[0] for row in conn.execute("""
        SELECT DISTINCT b.id
        FROM boards b
        JOIN verifications v ON v.board_id = b.id
        WHERE v.outcome IN ('verified_live','verified_live_zero_jobs')
          AND b.ats IN ('greenhouse','lever','ashby','smartrecruiters','breezy')
          AND EXISTS (SELECT 1 FROM identities i WHERE i.board_id = b.id AND i.status = 'identity_confirmed')
        ORDER BY b.id
    """).fetchall()]
    print(f"Confirmed live boards: {len(board_ids)}")

    # Get latest data for each board
    # Latest identity_confirmed per board
    identities = {}
    for row in conn.execute("""
        SELECT i.board_id, i.name, i.basis, i.run
        FROM identities i
        JOIN (
            SELECT board_id, MAX(decided_at || '|' || run) as pick
            FROM identities
            WHERE status = 'identity_confirmed'
            GROUP BY board_id
        ) t ON i.board_id = t.board_id AND (i.decided_at || '|' || i.run) = t.pick
    """).fetchall():
        identities[row[0]] = {"name": row[1], "basis": row[2], "run": row[3]}

    # Latest verification per board
    verifications = {}
    for row in conn.execute("""
        SELECT v.board_id, v.outcome, v.checked_at, v.jobs_count
        FROM verifications v
        JOIN (
            SELECT board_id, MAX(checked_at || '|' || id) as pick
            FROM verifications
            WHERE outcome IN ('verified_live','verified_live_zero_jobs')
            GROUP BY board_id
        ) t ON v.board_id = t.board_id AND (v.checked_at || '|' || v.id) = t.pick
    """).fetchall():
        verifications[row[0]] = {"outcome": row[1], "checked_at": row[2], "jobs_count": row[3]}

    # Latest field evidence per board per field
    field_evidence = {}
    for field in ['company_name', 'description', 'website_url', 'linkedin_url', 'careersite_url']:
        for row in conn.execute(f"""
            SELECT e.board_id, e.value, e.method
            FROM field_evidence e
            JOIN (
                SELECT board_id, MAX(observed_at || '|' || id) as pick
                FROM field_evidence
                WHERE field = ? AND status = 'verified'
                GROUP BY board_id
            ) t ON e.board_id = t.board_id AND (e.observed_at || '|' || e.id) = t.pick
        """, (field,)).fetchall():
            field_evidence.setdefault(row[0], {})[field] = {"value": row[1], "method": row[2]}

    # Build confirmed rows
    confirmed = []
    for bid in conn.execute("""
        SELECT b.id, b.ats, b.code, b.region
        FROM boards b
        JOIN verifications v ON v.board_id = b.id
        WHERE v.outcome IN ('verified_live','verified_live_zero_jobs')
          AND b.ats IN ('greenhouse','lever','ashby','smartrecruiters','breezy')
          AND EXISTS (SELECT 1 FROM identities i WHERE i.board_id = b.id AND i.status = 'identity_confirmed')
        ORDER BY b.id
    """).fetchall():
        bid = bid[0] if isinstance(bid, tuple) else bid
        ident = identities.get(bid)
        if not ident:
            continue
        v = verifications.get(bid)
        if not v or v["outcome"] not in ("verified_live", "verified_live_zero_jobs"):
            continue

        ev = field_evidence.get(bid, {})
        confirmed.append({
            "board_id": bid,
            "ats_system": row[1] if isinstance(row, tuple) else row["ats"],
            "ats_code": row[2] if isinstance(row, tuple) else row["code"],
            "region": row[3] if isinstance(row, tuple) else row["region"],
            "company_name": field_evidence.get(bid, {}).get("company_name", {}).get("value", ""),
            "description": field_evidence.get(bid, {}).get("description", {}).get("value", ""),
            "description_method": field_evidence.get(bid, {}).get("description", {}).get("method", ""),
            "website_url": field_evidence.get(bid, {}).get("website_url", {}).get("value", ""),
            "linkedin_url": field_evidence.get(bid, {}).get("linkedin_url", {}).get("value", ""),
            "careersite_url": field_evidence.get(bid, {}).get("careersite_url", {}).get("value", ""),
            "identity_basis": identities.get(bid, {}).get("basis", ""),
            "verification_date": verifications.get(bid, {}).get("checked_at", ""),
            "jobs_count": verifications.get(bid, {}).get("jobs_count", 0),
            "ats_system": "",
            "ats_code": "",
            "region": "",
            "identity_basis": "",
            "verification_date": "",
            "jobs_count": 0,
            "identity_status": "identity_confirmed",
            "verification_status": "verified_live",
            "missing_fields": "",
        })

    print(f"Confirmed live boards: {len(board_ids)}")
    return

if __name__ == "__main__":
    main()