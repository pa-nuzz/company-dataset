#!/usr/bin/env python3
"""Populate companies and company_boards tables from verified data.
Every confirmed-live board (verified live + any identity_confirmed) appears exactly once."""
from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from company_dataset import store
from company_dataset.export import (
    decided_rows, CONFIRMED, LIVE, CORE_ATS, VERIFIED_COLUMNS,
    cluster_companies, _missing, _companies
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

    # Clear existing data
    with conn:
        conn.execute("DELETE FROM company_boards")
        conn.execute("DELETE FROM companies")
    print("Cleared companies and company_boards tables")

    # Get ALL boards that are verified live AND have at least one identity_confirmed row
    # Use the latest evidence for clustering
    confirmed_rows = conn.execute("""
        SELECT 
            b.id as board_id,
            b.ats,
            b.code,
            b.region,
            COALESCE(ev_company_name.value, '') as company_name,
            COALESCE(ev_description.value, '') as description,
            COALESCE(ev_description.method, '') as description_method,
            COALESCE(ev_website.value, '') as website_url,
            COALESCE(ev_linkedin.value, '') as linkedin_url,
            COALESCE(ev_careersite.value, '') as careersite_url,
            i.basis as identity_basis,
            v.checked_at as verification_date,
            v.jobs_count
        FROM boards b
        JOIN verifications v ON v.board_id = b.id
        JOIN identities i ON i.board_id = b.id
        LEFT JOIN field_evidence ev_company_name ON ev_company_name.board_id = b.id AND ev_company_name.field = 'company_name' AND ev_company_name.status = 'verified'
        LEFT JOIN field_evidence ev_description ON ev_description.board_id = b.id AND ev_description.field = 'description' AND ev_description.status = 'verified'
        LEFT JOIN field_evidence ev_website ON ev_website.board_id = b.id AND ev_website.field = 'website_url' AND ev_website.status = 'verified'
        LEFT JOIN field_evidence ev_linkedin ON ev_linkedin.board_id = b.id AND ev_linkedin.field = 'linkedin_url' AND ev_linkedin.status = 'verified'
        LEFT JOIN field_evidence ev_careersite ON ev_careersite.board_id = b.id AND ev_careersite.field = 'careersite_url' AND ev_careersite.status = 'verified'
        WHERE v.outcome IN ('verified_live', 'verified_live_zero_jobs')
          AND b.ats IN ('greenhouse','lever','ashby','smartrecruiters','breezy')
          AND EXISTS (
              SELECT 1 FROM identities i2 
              WHERE i2.board_id = b.id AND i2.status = 'identity_confirmed'
          )
        ORDER BY b.id
    """).fetchall()
    
    print(f"Confirmed live boards (any identity_confirmed): {len(confirmed_rows)}")

    # Convert to list of dicts for clustering
    confirmed_rows = [dict(r) for r in confirmed_rows]
    for r in confirmed_rows:
        r["company_name"] = r["company_name"] or ""
        r["description"] = r["description"] or ""
        r["description_method"] = r["description_method"] or ""
        r["website_url"] = r["website_url"] or ""
        r["linkedin_url"] = r["linkedin_url"] or ""
        r["careersite_url"] = r["careersite_url"] or ""
        r["identity_basis"] = r["identity_basis"] or ""
        r["verification_date"] = r["verification_date"] or ""
        r["jobs_count"] = r["jobs_count"] or 0

    # Cluster companies
    clusters = cluster_companies([{
        "name": r["company_name"], "website_url": r["website_url"],
        "website_verified": bool(r["website_url"]), "linkedin_url": r["linkedin_url"],
        "ats_system": r["ats_system"], "ats_code": r["ats_code"]
    } for r in confirmed_rows])

    # Map cluster index -> primary board_id
    primary_board_ids = {}
    for i, g in enumerate(clusters):
        members = sorted((confirmed_rows[idx] for idx in g), key=lambda r: (
            bool(r.get("missing_fields", "")), -sum(bool(r[c]) for c in ["company_name","description","ats_system","ats_code","linkedin_url","website_url","careersite_url"]),
            -(int(r["jobs_count"] or 0)), r["ats_system"], r["ats_code"].casefold()))
        primary_board_ids[i] = members[0]["board_id"]

    # Insert companies (use the export logic for company-level deduplication)
    # We need to group by the export's company definition
    # Use the same clustering as export but on our confirmed_rows
    # Build companies using export logic
    rows_for_export = []
    for r in confirmed_rows:
        rows_for_export.append({
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
        })

    # Use _companies to get the export-style companies
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

    # Re-run clustering to get board_id mapping
    clusters = cluster_companies([{
        "name": r["company_name"], "website_url": r["website_url"],
        "website_verified": bool(r["website_url"]), "linkedin_url": r["linkedin_url"],
        "ats_system": r["ats_system"], "ats_code": r["ats_code"]
    } for r in confirmed_rows])

    primary_board_ids = {}
    for i, g in enumerate(clusters):
        members = sorted((confirmed_rows[idx] for idx in g), key=lambda r: (
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
    with conn:
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