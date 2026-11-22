#!/usr/bin/env python3
"""Populate companies and company_boards - simple Python approach."""
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

def get_latest_identity_confirmed(conn):
    """Get latest identity_confirmed per board."""
    cur = conn.execute("""
        SELECT i.board_id, i.name, i.basis, i.run
        FROM identities i
        JOIN (
            SELECT board_id, MAX(decided_at || '|' || run) as pick
            FROM identities
            WHERE status = 'identity_confirmed'
            GROUP BY board_id
        ) t ON i.board_id = t.board_id AND (i.decided_at || '|' || i.run) = t.pick
    """)
    return {row[0]: {"name": row[1], "basis": row[2], "run": row[3]} for row in cur.fetchall()}

def get_latest_verification(conn):
    """Get latest verification per board."""
    cur = conn.execute("""
        SELECT v.board_id, v.outcome, v.checked_at, v.jobs_count
        FROM verifications v
        JOIN (
            SELECT board_id, MAX(checked_at || '|' || id) as pick
            FROM verifications
            WHERE outcome IN ('verified_live','verified_live_zero_jobs')
            GROUP BY board_id
        ) t ON v.board_id = t.board_id AND (v.checked_at || '|' || v.id) = t.pick
    """)
    return {row[0]: {"outcome": row[1], "checked_at": row[2], "jobs_count": row[3]} for row in cur.fetchall()}

def get_latest_field_evidence(conn, field):
    """Get latest field evidence per board."""
    cur = conn.execute(f"""
        SELECT e.board_id, e.value, e.method
        FROM field_evidence e
        JOIN (
            SELECT board_id, MAX(observed_at || '|' || id) as pick
            FROM field_evidence
            WHERE field = ? AND status = 'verified'
            GROUP BY board_id
        ) t ON e.board_id = t.board_id AND (e.observed_at || '|' || e.id) = t.pick
    """, (field,))
    return {row[0]: {"value": row[1], "method": row[2]} for row in cur.fetchall()}

def main():
    conn = store.connect(DB)
    conn.row_factory = sqlite3.Row

    # Clear
    with conn:
        conn.execute("DELETE FROM company_boards")
        conn.execute("DELETE FROM companies")
    print("Cleared tables")

    # Get all confirmed-live boards with identity_confirmed
    cur = conn.execute("""
        SELECT b.id, b.ats, b.code, b.region
        FROM boards b
        JOIN verifications v ON v.board_id = b.id
        WHERE v.outcome IN ('verified_live','verified_live_zero_jobs')
          AND b.ats IN ('greenhouse','lever','ashby','smartrecruiters','breezy')
          AND EXISTS (SELECT 1 FROM identities i WHERE i.board_id = b.id AND i.status = 'identity_confirmed')
        ORDER BY b.id
    """)
    board_ids = [row[0] for row in cur.fetchall()]
    print(f"Confirmed live boards: {len(board_ids)}")

    # Get latest data for each board
    identities = {}
    cur = conn.execute("""
        SELECT i.board_id, i.name, i.basis, i.run
        FROM identities i
        JOIN (
            SELECT board_id, MAX(decided_at || '|' || run) as pick
            FROM identities
            WHERE status = 'identity_confirmed'
            GROUP BY board_id
        ) t ON i.board_id = t.board_id AND (i.decided_at || '|' || i.run) = t.pick
    """)
    identities = {row[0]: {"name": row[1], "basis": row[2], "run": row[3]} for row in conn.execute("""
        SELECT i.board_id, i.name, i.basis, i.run
        FROM identities i
        JOIN (
            SELECT board_id, MAX(decided_at || '|' || run) as pick
            FROM identities
            WHERE status = 'identity_confirmed'
            GROUP BY board_id
        ) t ON i.board_id = t.board_id AND (i.decided_at || '|' || i.run) = t.pick
    """).fetchall()}

    verifications = {row[0]: {"outcome": row[1], "checked_at": row[2], "jobs_count": row[3]} for row in conn.execute("""
        SELECT v.board_id, v.outcome, v.checked_at, v.jobs_count
        FROM verifications v
        JOIN (
            SELECT board_id, MAX(checked_at || '|' || id) as pick
            FROM verifications
            WHERE outcome IN ('verified_live','verified_live_zero_jobs')
            GROUP BY board_id
        ) t ON v.board_id = t.board_id AND (v.checked_at || '|' || v.id) = t.pick
    """).fetchall()}

    # Get field evidence
    field_evidence = {}
    for field in ['company_name', 'description', 'website_url', 'linkedin_url', 'careersite_url']:
        cur = conn.execute(f"""
            SELECT e.board_id, e.value, e.method
            FROM field_evidence e
            JOIN (
                SELECT board_id, MAX(observed_at || '|' || id) as pick
                FROM field_evidence
                WHERE field = ? AND status = 'verified'
                GROUP BY board_id
            ) t ON e.board_id = t.board_id AND (e.observed_at || '|' || e.id) = t.pick
        """, (field,))
        for row in cur.fetchall():
            field_evidence.setdefault(row[0], {})[field] = {"value": row[1], "method": row[2]}

    # Build confirmed rows
    confirmed = []
    cur = conn.execute("""
        SELECT b.id, b.ats, b.code, b.region
        FROM boards b
        JOIN verifications v ON v.board_id = b.id
        WHERE v.outcome IN ('verified_live','verified_live_zero_jobs')
          AND b.ats IN ('greenhouse','lever','ashby','smartrecruiters','breezy')
          AND EXISTS (SELECT 1 FROM identities i WHERE i.board_id = b.id AND i.status = 'identity_confirmed')
        ORDER BY b.id
    """)
    for row in cur.fetchall():
        bid = row[0]
        ident = None
        # Find the latest identity_confirmed for this board
        # We need to query it
        pass
    
    # Simpler: just query all needed data in one go
    confirmed = []
    for row in conn.execute("""
        SELECT 
            b.id as board_id, b.ats, b.code, b.region,
            i.name as ident_name, i.basis, i.run as ident_run,
            v.outcome, v.checked_at, v.jobs_count
        FROM boards b
        JOIN verifications v ON v.board_id = b.id
        JOIN identities i ON i.board_id = b.id
        WHERE v.outcome IN ('verified_live','verified_live_zero_jobs')
          AND b.ats IN ('greenhouse','lever','ashby','smartrecruiters','breezy')
          AND i.status = 'identity_confirmed'
        ORDER BY b.id
    """):
        bid = row[0]
        # Get latest evidence for this board
        fe = {}
        for field in ['company_name', 'description', 'website_url', 'linkedin_url', 'careersite_url']:
            cur = conn.execute("""
                SELECT value, method FROM field_evidence
                WHERE board_id = ? AND field = ? AND status = 'verified'
                ORDER BY observed_at DESC LIMIT 1
            """, (bid,))
            r = cur.fetchone()
            if r:
                pass  # We'll get all fields in one query below
    
    # Simpler: just use a single query with all subqueries
    confirmed = []
    for row in conn.execute("""
        SELECT 
            b.id as board_id, b.ats, b.code, b.region,
            (SELECT i.name FROM identities i 
             JOIN (SELECT board_id, MAX(decided_at || '|' || run) as pick 
                   FROM identities WHERE status = 'identity_confirmed' GROUP BY board_id) t
                   ON i.board_id = t.board_id AND (i.decided_at || '|' || i.run) = t.pick
             WHERE i.board_id = b.id) as ident_name,
            (SELECT i.basis FROM identities i 
             JOIN (SELECT board_id, MAX(decided_at || '|' || run) as pick 
                   FROM identities WHERE status = 'identity_confirmed' GROUP BY board_id) t
                   ON i.board_id = t.board_id AND (i.decided_at || '|' || i.run) = t.pick
             WHERE i.board_id = b.id) as ident_basis,
            (SELECT i.run FROM identities i 
             JOIN (SELECT board_id, MAX(decided_at || '|' || run) as pick 
                   FROM identities WHERE status = 'identity_confirmed' GROUP BY board_id) t
                   ON i.board_id = t.board_id AND (i.decided_at || '|' || i.run) = t.pick
             WHERE i.board_id = b.id) as ident_run,
            v.outcome, v.checked_at, v.jobs_count,
            b.id, b.ats, b.code, b.region
        FROM boards b
        JOIN verifications v ON v.board_id = b.id
        WHERE v.outcome IN ('verified_live','verified_live_zero_jobs')
          AND b.ats IN ('greenhouse','lever','ashby','smartrecruiters','breezy')
          AND EXISTS (SELECT 1 FROM identities i WHERE i.board_id = b.id AND i.status = 'identity_confirmed')
        ORDER BY b.id
    """):
        bid = row[0]
        # Get field evidence for this board
        ev_company = conn.execute("""
            SELECT value FROM field_evidence 
            WHERE board_id = ? AND field = 'company_name' AND status = 'verified'
            ORDER BY observed_at DESC LIMIT 1
        """, (bid,)).fetchone()
        ev_desc = conn.execute("""
            SELECT value, method FROM field_evidence 
            WHERE board_id = ? AND field = 'description' AND status = 'verified'
            ORDER BY observed_at DESC LIMIT 1
        """, (bid,)).fetchone()
        ev_web = conn.execute("""
            SELECT value FROM field_evidence 
            WHERE board_id = ? AND field = 'website_url' AND status = 'verified'
            ORDER BY observed_at DESC LIMIT 1
        """, (bid,)).fetchone()
        ev_linkedin = conn.execute("""
            SELECT value FROM field_evidence 
            WHERE board_id = ? AND field = 'linkedin_url' AND status = 'verified'
            ORDER BY observed_at DESC LIMIT 1
        """, (bid,)).fetchone()
        ev_career = conn.execute("""
            SELECT value FROM field_evidence 
            WHERE board_id = ? AND field = 'careersite_url' AND status = 'verified'
            ORDER BY observed_at DESC LIMIT 1
        """, (bid,)).fetchone()
        
        confirmed.append({
            "board_id": row["board_id"],
            "ats_system": row["ats"],
            "ats_code": row["code"],
            "region": row["region"] or "",
            "company_name": ev_company[0] if ev_company else "",
            "description": ev_desc[0] if ev_desc else "",
            "description_method": ev_desc[1] if ev_desc else "",
            "website_url": row["website_url"] or "",
            "linkedin_url": row["linkedin_url"] or "",
            "careersite_url": row["careersite_url"] or "",
            "identity_basis": row["ident_basis"] or "",
            "verification_date": row["verification_date"] or "",
            "jobs_count": row["jobs_count"] or 0,
            "ats_system": row["ats"],
            "ats_code": row["code"],
            "region": row["region"] or "",
            "identity_basis": row["ident_basis"] or "",
            "verification_date": row["verification_date"] or "",
            "jobs_count": row["jobs_count"] or 0,
            "identity_status": "identity_confirmed",
            "verification_status": "verified_live",
            "missing_fields": "",
            "ats_system": row["ats"],
            "ats_code": row["code"],
            "region": row["region"] or ""
        })

if __name__ == "__main__":
    print("This is a partial script - need to complete")