#!/usr/bin/env python3
"""Populate companies and company_boards tables from verified data.
Every confirmed-live board (verified live + any identity_confirmed) appears exactly once."""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from company_dataset import store
from company_dataset.export import (
    CONFIRMED, LIVE, CORE_ATS, VERIFIED_COLUMNS,
    cluster_companies, _missing, _companies, _latest_identity_per_board,
    _latest_field_evidence_per_board_field, _latest_checks, _rejected_urls,
    _PLACEHOLDER_RE, _dormant_name_guess, _usable_description, acceptable,
    clean_name, clean_website, website_rule_failure, _normalize_url,
    normalize_linkedin, smartrecruiters_generic_page, _handle_names_company,
    _parent_of, apply_linkedin_policy, SR_NAME_GUESS, REFERENCE_NAME_BASIS,
    _fold, _LEGAL_WORDS, linkedin_compatible, _companies, _missing
)

DB = "data/store.db"
EXPORT_LABEL = "final_2026-11-04"
RUNS = (
    "fullrun,disc1,sr_fix_test,sr_existence_2026-11-03,identity_fix_2026-11-03,"
    "recovery_reverify_2026-11-03,desc_cached_2026-11-03,desc_network_2026-11-03,"
    "reenrich_2026-11-03,descriptions_fallback_2026-11-03,discovery_2026-11-03,"
    "discovery2_2026-11-03,discovery3_2026-11-03,discovery4_2026-11-03,"
    "retry_2026-11-03,ownership_2026-11-03,desc_non_english_2026-11-03,"
    "desc_cascade_2026-11-03,vendor_promote_2026-11-03,org_promote_2026-11-03,"
    "desc_promoted_cached_2026-11-03,desc_promoted_network_2026-11-03,"
    "desc_shadow_fix_2026-11-03,ownership_cycle2_2026-11-03,"
    "reresolve_cycle2_2026-11-03,reresolve_cycle2b_2026-11-03,"
    "cycle2_enrich_2026-11-03,cycle3_enrich_2026-11-03,cycle3b_enrich_2026-11-03,"
    "reresolve_cycle3_2026-11-03,desc_cycle3_2026-11-03,"
    "ownership_final_2026-11-03,final_enrich_2026-11-03,"
    "reresolve_final_2026-11-03,reresolve_cycle2_sources_2026-11-04,"
    "reverify_all_2026-11-04,pilot1,pilot2,pilot3,recovery1,sr_fix_test"
).split(",")

def main():
    conn = store.connect(DB)
    run_list = RUNS
    print(f"Using {len(run_list)} runs")

    # Get latest identity per board (any run with identity_confirmed)
    # Use a custom query to get boards that have ANY identity_confirmed
    # and are verified live
    conn.row_factory = None
    cur = conn.execute("""
        SELECT DISTINCT b.id, b.ats, b.code, b.region
        FROM boards b
        JOIN verifications v ON v.board_id = b.id
        WHERE v.outcome IN ('verified_live','verified_live_zero_jobs')
          AND b.ats IN ('greenhouse','lever','ashby','smartrecruiters','breezy')
          AND EXISTS (
              SELECT 1 FROM identities i 
              WHERE i.board_id = b.id AND i.status = 'identity_confirmed'
          )
    """)
    board_ids = [row[0] for row in cur.fetchall()]
    print(f"Confirmed live boards (any identity_confirmed): {len(board_ids)}")

    # Get latest identity per board (from any run)
    identities = {}
    # Use a query that gets the latest identity_confirmed per board
    cur = conn.execute("""
        SELECT i.board_id, i.status, i.name, i.basis, i.run, i.decided_at
        FROM identities i
        JOIN (
            SELECT board_id, MAX(decided_at || '|' || run) as pick
            FROM identities
            WHERE status = 'identity_confirmed'
            GROUP BY board_id
        ) t ON i.board_id = t.board_id AND (i.decided_at || '|' || i.run) = t.pick
        WHERE i.board_id IN ({})
    """.format(','.join('?' * len(board_ids))), board_ids)
    for row in conn.execute(cur.query, board_ids):
        identities[row[0]] = {"status": row[1], "name": row[2], "basis": row[3], "run": row[4], "decided_at": row[5]}

    # Get latest field evidence per board per field
    field_evidence = {}
    cur = conn.execute("""
        SELECT e.board_id, e.field, e.value, e.method, e.evidence_url, e.evidence_text
        FROM field_evidence e
        JOIN (
            SELECT board_id, field, MAX(observed_at || '|' || id) as pick
            FROM field_evidence
            WHERE status = 'verified'
            GROUP BY board_id, field
        ) t ON e.board_id = t.board_id AND e.field = t.field AND (e.observed_at || '|' || e.id) = t.pick
        WHERE e.board_id IN ({})
    """.format(','.join('?' * len(board_ids))), board_ids)
    for row in conn.execute(cur.query, board_ids):
        field_evidence[(row[0], row[1])] = {"value": row[2], "method": row[3], "evidence_url": row[3], "evidence_text": row[4]}

    # Get verification info
    checks = {}
    cur = conn.execute("""
        SELECT v.board_id, v.outcome, v.checked_at, v.endpoint, v.origin, v.jobs_count
        FROM verifications v
        JOIN (
            SELECT board_id, MAX(checked_at || '|' || id) as pick
            FROM verifications
            WHERE outcome IN ('verified_live','verified_live_zero_jobs')
            GROUP BY board_id
        ) t ON v.board_id = t.board_id AND (v.checked_at || '|' || v.id) = t.pick
        WHERE v.board_id IN ({})
    """.format(','.join('?' * len(board_ids))), board_ids)
    for row in conn.execute(cur.query, board_ids):
        checks[row[0]] = {"outcome": row[1], "checked_at": row[2], "endpoint": row[3], "origin": row[4], "jobs_count": row[5]}

    # Get source names
    sources = {}
    cur = conn.execute("""
        SELECT sr.board_id, s.name
        FROM source_rows sr
        JOIN sources s ON s.id = sr.source_id
        WHERE sr.board_id IN ({})
    """.format(','.join('?' * len(board_ids))), board_ids)
    for row in conn.execute(cur.query, board_ids):
        sources.setdefault(row[0], []).append(row[1])

    # Build confirmed rows
    confirmed_rows = []
    for bid in board_ids:
        ident = identities.get(bid)
        if not ident:
            continue
        v = checks.get(bid)
        if not v or v["outcome"] not in ("verified_live", "verified_live_zero_jobs"):
            continue

        # Get field evidence
        ev_company_name = field_evidence.get((bid, "company_name"))
        ev_description = field_evidence.get((bid, "description"))
        ev_website = field_evidence.get((bid, "website_url"))
        ev_linkedin = field_evidence.get((bid, "linkedin_url"))
        ev_careersite = field_evidence.get((bid, "careersite_url"))

        row = {
            "board_id": bid,
            "company_name": field_evidence.get((bid, "company_name"), {}).get("value", ""),
            "description": field_evidence.get((bid, "description"), {}).get("value", ""),
            "description_method": field_evidence.get((bid, "description"), {}).get("method", ""),
            "website_url": field_evidence.get((bid, "website_url"), {}).get("value", ""),
            "linkedin_url": field_evidence.get((bid, "linkedin_url"), {}).get("value", ""),
            "careersite_url": field_evidence.get((bid, "careersite_url"), {}).get("value", ""),
            "identity_basis": identities[bid]["basis"] or "",
            "verification_date": checks[bid]["checked_at"] if bid in checks else "",
            "jobs_count": checks[bid]["jobs_count"] if bid in checks else 0,
            "ats_system": "greenhouse",  # Will be filled from board
            "ats_code": "",
            "description_method": field_evidence.get((bid, "description"), {}).get("method", ""),
            "website_url": field_evidence.get((bid, "website_url"), {}).get("value", ""),
            "linkedin_url": field_evidence.get((bid, "linkedin_url"), {}).get("value", ""),
            "careersite_url": field_evidence.get((bid, "careersite_url"), {}).get("value", ""),
        }
        # Get ats_system and ats_code from board
        cur = conn.execute("SELECT ats, code, region FROM boards WHERE id=?", (bid,))
        b = cur.fetchone()
        if b:
            row["ats_system"] = b[0]
            row["ats_code"] = b[1]
            row["region"] = b[2] or ""
        confirmed_rows.append(row)

    print(f"Confirmed live boards: {len(confirmed_rows)}")

    # Build companies using export logic
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
            "missing_fields": ""
        } for r in confirmed_rows
    ], None)
    
    print(f"Total companies: {len(companies)}, Merged: {merged}, LinkedIn cleared: {len(linkedin_cleared) if isinstance(linkedin_cleared, list) else linkedin_cleared}")

    # Clear and insert
    with conn:
        conn.execute("DELETE FROM company_boards")
        conn.execute("DELETE FROM companies")

    # Need to cluster to get primary board mapping
    # Re-create confirmed_rows for clustering
    confirmed_for_cluster = []
    for r in confirmed_rows:
        confirmed_for_cluster.append({
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
            "region": r.get("region", "")
        })

    # Cluster
    from company_dataset.export import cluster_companies, _missing
    clusters = cluster_companies([{
        "name": r["company_name"], "website_url": r["website_url"],
        "website_verified": bool(r["website_url"]), "linkedin_url": r["linkedin_url"],
        "ats_system": r["ats_system"], "ats_code": r["ats_code"]
    } for r in confirmed_rows])

    primary_board_ids = {}
    for i, g in enumerate(clusters):
        members = sorted((dict(r) for r in [confirmed_rows[idx] for idx in g]), key=lambda r: (
            bool(r.get("missing_fields", "")), -sum(bool(r[c]) for c in ["company_name","description","ats_system","ats_code","linkedin_url","website_url","careersite_url"]),
            -(int(r["jobs_count"] or 0)), r["ats_system"], r["ats_code"].casefold()))
        primary_board_ids[i] = members[0]["board_id"]

    # Clear and insert
    with conn:
        conn.execute("DELETE FROM company_boards")
        conn.execute("DELETE FROM companies")

    # Insert companies
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
    for i in range(len(companies)):
        pbid = primary_board_ids.get(i)
        row = conn.execute("SELECT id FROM companies WHERE export_label='final_2026-11-04' AND primary_board_id=?", 
                           (pbid,)).fetchone()
        if row:
            company_ids[i] = row[0]

    # Link ALL boards in each cluster to the company
    with conn:
        for i, g in enumerate(clusters):
            if i not in company_ids:
                continue
            cid = company_ids[i]
            members = sorted((confirmed_rows[idx] for idx in g), key=lambda r: (
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
    print("Done populating companies and company_boards")

    # Verify counts
    verified_count = conn.execute("SELECT COUNT(*) FROM companies WHERE status='verified'").fetchone()[0]
    incomplete_count = conn.execute("SELECT COUNT(*) FROM companies WHERE status='incomplete'").fetchone()[0]
    company_boards_count = conn.execute("SELECT COUNT(*) FROM company_boards").fetchone()[0]
    primary_count = conn.execute("SELECT COUNT(*) FROM company_boards WHERE link_reason='primary'").fetchone()[0]
    print(f"Verified: {verified_count}, Incomplete: {incomplete_count}, Company-boards links: {company_boards_count}, primary: {primary_count}")

    # Verify every confirmed-live board appears exactly once
    confirmed_live_count = conn.execute("""
        SELECT COUNT(DISTINCT b.id) FROM boards b
        JOIN verifications v ON v.board_id = b.id
        WHERE v.outcome IN ('verified_live','verified_live_zero_jobs')
          AND EXISTS (SELECT 1 FROM identities i WHERE i.board_id = b.id AND i.status = 'identity_confirmed')
          AND b.ats IN ('greenhouse','lever','ashby','smartrecruiters','breezy')
    """).fetchone()[0]
    cb_count = conn.execute("SELECT COUNT(DISTINCT board_id) FROM company_boards").fetchone()[0]
    print(f"Confirmed-live boards: {confirmed_live_count}, In company_boards: {cb_count}")
    print(f"Match: {confirmed_live_count == cb_count}")

    # Verify every company has exactly one primary
    primary_per_company = conn.execute("""
        SELECT company_id, COUNT(*) FROM company_boards WHERE link_reason='primary' GROUP BY company_id HAVING COUNT(*) != 1
    """).fetchall()
    print(f"Companies with != 1 primary: {len(primary_per_company)}")

    # Verify companies(status=verified) vs companies_verified.csv
    with open("data/exports/final_2026-11-03/companies_verified.csv") as f:
        csv_count = sum(1 for _ in csv.reader(f)) - 1
    db_verified = conn.execute("SELECT COUNT(*) FROM companies WHERE status='verified'").fetchone()[0]
    print(f"Export CSV verified: {csv_count}, DB verified: {db_verified}, Match: {csv_count == db_verified}")

if __name__ == "__main__":
    main()