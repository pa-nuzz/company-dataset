#!/usr/bin/env python3
"""Populate companies and company_boards - simple direct SQL approach."""
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

    # Get confirmed-live boards with their latest evidence
    confirmed = []
    for row in conn.execute("""
        WITH li AS (
            SELECT i.board_id, i.name, i.basis, i.run
            FROM identities i
            JOIN (
                SELECT board_id, MAX(decided_at || '|' || run) as pick
                FROM identities
                WHERE status = 'identity_confirmed'
                GROUP BY board_id
            ) t ON i.board_id = t.board_id AND (i.decided_at || '|' || i.run) = t.pick
        ),
        vf AS (
            SELECT v.board_id, v.outcome as v_outcome, v.checked_at, v.jobs_count
            FROM verifications v
            JOIN (
                SELECT board_id, MAX(checked_at || '|' || id) as pick
                FROM verifications
                WHERE outcome IN ('verified_live','verified_live_zero_jobs')
                GROUP BY board_id
            ) t ON v.board_id = t.board_id AND (v.checked_at || '|' || v.id) = t.pick
        ),
        fe_company AS (
            SELECT e.board_id, e.value
            FROM field_evidence e
            JOIN (
                SELECT board_id, MAX(observed_at || '|' || id) as pick
                FROM field_evidence
                WHERE field = 'company_name' AND status = 'verified'
                GROUP BY board_id
            ) t ON e.board_id = t.board_id AND (e.observed_at || '|' || e.id) = t.pick
        ),
        fe_desc AS (
            SELECT e.board_id, e.value, e.method
            FROM field_evidence e
            JOIN (
                SELECT board_id, MAX(observed_at || '|' || id) as pick
                FROM field_evidence
                WHERE field = 'description' AND status = 'verified'
                GROUP BY board_id
            ) t ON e.board_id = t.board_id AND (e.observed_at || '|' || e.id) = t.pick
        ),
        fe_web AS (
            SELECT e.board_id, e.value
            FROM field_evidence e
            JOIN (
                SELECT board_id, MAX(observed_at || '|' || id) as pick
                FROM field_evidence
                WHERE field = 'website_url' AND status = 'verified'
                GROUP BY board_id
            ) t ON e.board_id = t.board_id AND (e.observed_at || '|' || e.id) = t.pick
        ),
        fe_linkedin AS (
            SELECT e.board_id, e.value
            FROM field_evidence e
            JOIN (
                SELECT board_id, MAX(observed_at || '|' || id) as pick
                FROM field_evidence
                WHERE field = 'linkedin_url' AND status = 'verified'
                GROUP BY board_id
            ) t ON e.board_id = t.board_id AND (e.observed_at || '|' || e.id) = t.pick
        ),
        fe_career AS (
            SELECT e.board_id, e.value
            FROM field_evidence e
            JOIN (
                SELECT board_id, MAX(observed_at || '|' || id) as pick
                FROM field_evidence
                WHERE field = 'careersite_url' AND status = 'verified'
                GROUP BY board_id
            ) t ON e.board_id = t.board_id AND (e.observed_at || '|' || e.id) = t.pick
        )
        SELECT 
            b.id as board_id, b.ats, b.code, b.region,
            li.name as ident_name, li.basis, li.run as ident_run,
            vf.v_outcome as outcome, vf.checked_at, vf.jobs_count,
            fc.value as company_name,
            fd.value as description,
            fd.method as description_method,
            fw.value as website_url,
            fl.value as linkedin_url,
            fc2.value as careersite_url,
            li.basis as identity_basis,
            vf.checked_at as verification_date,
            vf.jobs_count,
            b.ats as ats_system,
            b.code as ats_code,
            b.region
        FROM boards b
        JOIN (
            SELECT i.board_id, i.name, i.basis, i.run
            FROM identities i
            JOIN (
                SELECT board_id, MAX(decided_at || '|' || run) as pick
                FROM identities
                WHERE status = 'identity_confirmed'
                GROUP BY board_id
            ) t ON i.board_id = t.board_id AND (i.decided_at || '|' || i.run) = t.pick
        ) li ON li.board_id = b.id
        JOIN (
            SELECT v.board_id, v.outcome as v_outcome, v.checked_at, v.jobs_count
            FROM verifications v
            JOIN (
                SELECT board_id, MAX(checked_at || '|' || id) as pick
                FROM verifications
                WHERE outcome IN ('verified_live','verified_live_zero_jobs')
                GROUP BY board_id
            ) t ON v.board_id = t.board_id AND (v.checked_at || '|' || v.id) = t.pick
        ) vf ON vf.board_id = b.id
        LEFT JOIN (
            SELECT e.board_id, e.value
            FROM field_evidence e
            JOIN (
                SELECT board_id, MAX(observed_at || '|' || id) as pick
                FROM field_evidence
                WHERE field = 'company_name' AND status = 'verified'
                GROUP BY board_id
            ) t ON e.board_id = t.board_id AND (e.observed_at || '|' || e.id) = t.pick
        ) fc ON fc.board_id = b.id
        LEFT JOIN (
            SELECT e.board_id, e.value, e.method
            FROM field_evidence e
            JOIN (
                SELECT board_id, MAX(observed_at || '|' || id) as pick
                FROM field_evidence
                WHERE field = 'description' AND status = 'verified'
                GROUP BY board_id
            ) t ON e.board_id = t.board_id AND (e.observed_at || '|' || e.id) = t.pick
        ) fd ON fd.board_id = b.id
        LEFT JOIN (
            SELECT e.board_id, e.value
            FROM field_evidence e
            JOIN (
                SELECT board_id, MAX(observed_at || '|' || id) as pick
                FROM field_evidence
                WHERE field = 'website_url' AND status = 'verified'
                GROUP BY board_id
            ) t ON e.board_id = t.board_id AND (e.observed_at || '|' || e.id) = t.pick
        ) fw ON fw.board_id = b.id
        LEFT JOIN (
            SELECT e.board_id, e.value
            FROM field_evidence e
            JOIN (
                SELECT board_id, MAX(observed_at || '|' || id) as pick
                FROM field_evidence
                WHERE field = 'linkedin_url' AND status = 'verified'
                GROUP BY board_id
            ) t ON e.board_id = t.board_id AND (e.observed_at || '|' || e.id) = t.pick
        ) fl ON fl.board_id = b.id
        LEFT JOIN (
            SELECT e.board_id, e.value
            FROM field_evidence e
            JOIN (
                SELECT board_id, MAX(observed_at || '|' || id) as pick
                FROM field_evidence
                WHERE field = 'careersite_url' AND status = 'verified'
                GROUP BY board_id
            ) t ON e.board_id = t.board_id AND (e.observed_at || '|' || e.id) = t.pick
        ) fc2 ON fc2.board_id = b.id
        WHERE b.ats IN ('greenhouse','lever','ashby','smartrecruiters','breezy')
          AND EXISTS (SELECT 1 FROM identities i WHERE i.board_id = b.id AND i.status = 'identity_confirmed')
          AND EXISTS (SELECT 1 FROM verifications v WHERE v.board_id = b.id AND v.outcome IN ('verified_live','verified_live_zero_jobs'))
        ORDER BY b.id
    """):
        confirmed.append({
            "board_id": row["board_id"],
            "ats_system": row["ats"],
            "ats_code": row["code"],
            "region": row["region"] or "",
            "company_name": row["company_name"] or "",
            "description": row["description"] or "",
            "description_method": row["description_method"] or "",
            "website_url": row["website_url"] or "",
            "linkedin_url": row["linkedin_url"] or "",
            "careersite_url": row["careersite_url"] or "",
            "identity_basis": row["basis"] or "",
            "verification_date": row["checked_at"] or "",
            "jobs_count": row["jobs_count"] or 0,
            "ats_system": row["ats"],
            "ats_code": row["code"],
            "region": row["region"] or "",
            "identity_basis": row["basis"] or "",
            "verification_date": row["checked_at"] or "",
            "jobs_count": row["jobs_count"] or 0,
            "identity_status": "identity_confirmed",
            "verification_status": "verified_live",
            "missing_fields": "",
            "ats_system": row["ats"],
            "ats_code": row["code"],
            "region": row["region"] or ""
        })

    print(f"Confirmed live boards: {len(confirmed)}")

    from company_dataset.export import _companies, _missing, cluster_companies, VERIFIED_COLUMNS, _missing
    companies, merged, linkedin_cleared = _companies([
        {
            "board_id": r["board_id"],
            "company_name": r["company_name"],
            "description": r["description"],
            "description_method": r["description_method"],
            "ats_system": r["ats_system"],
            "ats_code": r["ats_code"],
            "website_url": r["website_url"],
            "linkedin_url": r["linkedin_url"],
            "careersite_url": r["careersite_url"],
            "identity_basis": r["identity_basis"],
            "verification_date": r["verification_date"],
            "jobs_count": r["jobs_count"],
            "identity_status": "identity_confirmed",
            "verification_status": "verified_live",
            "missing_fields": "",
            "ats_system": r["ats_system"],
            "ats_code": r["ats_code"],
            "website_url": r["website_url"],
            "linkedin_url": r["linkedin_url"],
            "careersite_url": r["careersite_url"],
            "region": r.get("region", ""),
            "company_name": r["company_name"],
            "description": r["description"],
            "description_method": r["description_method"],
            "website_url": r["website_url"],
            "linkedin_url": r["linkedin_url"],
            "careersite_url": r["careersite_url"],
            "identity_basis": r["identity_basis"],
            "verification_date": r["verification_date"],
            "jobs_count": r["jobs_count"],
        } for r in confirmed
    ], None)

    print(f"Total companies: {len(companies)}, Merged: {merged}")

    # Cluster
    clusters = cluster_companies([{
        "name": r["company_name"], "website_url": r["website_url"],
        "website_verified": bool(r["website_url"]), "linkedin_url": r["linkedin_url"],
        "ats_system": r["ats_system"], "ats_code": r["ats_code"]
    } for r in confirmed])

    primary_board_ids = {}
    for i, g in enumerate(clusters):
        members = sorted((confirmed[idx] for idx in g), key=lambda r: (
            bool(r.get("missing_fields", "")), -sum(bool(r[c]) for c in ["company_name","description","ats_system","ats_code","linkedin_url","website_url","careersite_url"]),
            -(int(r["jobs_count"] or 0)), r["ats_system"], r["ats_code"].casefold()))
        primary_board_ids[i] = members[0]["board_id"]

    # Clear and insert
    with conn:
        conn.execute("DELETE FROM company_boards")
        conn.execute("DELETE FROM companies")

    for i, c in enumerate(companies):
        pbid = primary_board_ids.get(i, c.get("board_id", 0))
        conn.execute("""
            INSERT INTO companies (
                run, primary_board_id, company_name, description, description_method,
                ats_system, ats_code, website_url, linkedin_url, careersite_url,
                status, missing_fields, jobs_count, identity_basis, verification_date, export_label
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            "final_2026-11-04", pbid, c["company_name"], c["description"],
            c.get("description_method", ""), c["ats_system"], c["ats_code"],
            c["website_url"], c["linkedin_url"], c["careersite_url"],
            "verified" if not c["missing_fields"] else "incomplete",
            c["missing_fields"], c["jobs_count"],
            c["identity_basis"], c["verification_date"], "final_2026-11-04"
        ))

    # Get company IDs
    company_ids = {}
    with conn:
        for i in range(len(companies)):
            pbid = primary_board_ids.get(i)
            row = conn.execute("SELECT id FROM companies WHERE export_label='final_2026-11-04' AND primary_board_id=?", 
                               (primary_board_ids.get(i),)).fetchone()
            if row:
                company_ids[i] = row[0]

    # Link ALL boards in each cluster
    with conn:
        for i, g in enumerate(clusters):
            if i not in company_ids:
                continue
            cid = company_ids[i]
            members = sorted((confirmed[idx] for idx in g), key=lambda r: (
                bool(r.get("missing_fields", "")), -sum(bool(r[c]) for c in ["company_name","description","ats_system","ats_code","linkedin_url","website_url","careersite_url"]),
                -(int(r["jobs_count"] or 0)), r["ats_system"], r["ats_code"].casefold()))
            primary = members[0]
            for m in members:
                reason = "primary" if m["board_id"] == primary["board_id"] else "same_domain_and_name"
                if m["board_id"] != primary["board_id"] and m["linkedin_url"] and primary["linkedin_url"] and m["linkedin_url"] == primary["linkedin_url"]:
                    reason = "same_linkedin_and_name"
                conn.execute("""
                    INSERT OR IGNORE INTO company_boards (company_id, board_id, link_reason)
                    VALUES (?,?,?)
                """, (company_ids[i], m["board_id"], reason))

    # Create views
    with conn:
        conn.execute("DROP VIEW IF EXISTS v_verified_companies")
        conn.execute("CREATE VIEW v_verified_companies AS SELECT * FROM companies WHERE status = 'verified'")
        conn.execute("DROP VIEW IF EXISTS v_incomplete_companies")
        conn.execute("CREATE VIEW v_incomplete_companies AS SELECT * FROM companies WHERE status = 'incomplete'")

    conn.commit()
    print("Done populating")

    # Verify
    verified_count = conn.execute("SELECT COUNT(*) FROM companies WHERE status='verified'").fetchone()[0]
    incomplete_count = conn.execute("SELECT COUNT(*) FROM companies WHERE status='incomplete'").fetchone()[0]
    company_boards_count = conn.execute("SELECT COUNT(*) FROM company_boards").fetchone()[0]
    primary_count = conn.execute("SELECT COUNT(*) FROM company_boards WHERE link_reason='primary'").fetchone()[0]
    print(f"Verified: {verified_count}, Incomplete: {incomplete_count}, Company-boards links: {company_boards_count}, primary: {primary_count}")

    confirmed_live_count = conn.execute("""
        SELECT COUNT(DISTINCT b.id) FROM boards b
        JOIN verifications v ON v.board_id = b.id
        WHERE v.outcome IN ('verified_live','verified_live_zero_jobs')
          AND EXISTS (SELECT 1 FROM identities i WHERE i.board_id = b.id AND i.status = 'identity_confirmed')
          AND b.ats IN ('greenhouse','lever','ashby','smartrecruiters','breezy')
    """).fetchone()[0]
    cb_count = conn.execute("SELECT COUNT(DISTINCT board_id) FROM company_boards").fetchone()[0]
    print(f"Confirmed-live boards: {confirmed_live_count}, In company_boards: {cb_count}, Match: {confirmed_live_count == cb_count}")

    primary_per_company = conn.execute("""
        SELECT company_id, COUNT(*) FROM company_boards WHERE link_reason='primary' GROUP BY company_id HAVING COUNT(*) != 1
    """).fetchall()
    print(f"Companies with != 1 primary: {len(primary_per_company)}")

    with open("data/exports/final_2026-11-03/companies_verified.csv") as f:
        csv_count = sum(1 for _ in csv.reader(f)) - 1
    db_verified = conn.execute("SELECT COUNT(*) FROM companies WHERE status='verified'").fetchone()[0]
    print(f"Export CSV verified: {csv_count}, DB verified: {db_verified}, Match: {csv_count == db_verified}")

if __name__ == "__main__":
    import csv
    main()