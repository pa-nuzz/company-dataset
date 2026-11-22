#!/usr/bin/env python3
"""Populate companies and company_boards tables from verified data.
Every confirmed-live board appears exactly once with link_reason='primary' for primary board."""
from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from company_dataset import store
from company_dataset.export import (
    decided_rows, CONFIRMED, LIVE, CORE_ATS, VERIFIED_COLUMNS,
    cluster_companies, _missing, _companies, _latest_identity_per_board,
    _latest_field_evidence_per_board_field, _latest_checks, _rejected_urls,
    _PLACEHOLDER_RE, _dormant_name_guess, _usable_description, acceptable,
    clean_name, clean_website, website_rule_failure, _normalize_url,
    normalize_linkedin, smartrecruiters_generic_page, _handle_names_company,
    _parent_of, apply_linkedin_policy, SR_NAME_GUESS, REFERENCE_NAME_BASIS,
    _fold, _LEGAL_WORDS, linkedin_compatible, _companies
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
    "reverify_all_2026-11-04"
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

    # Build board rows using export.py logic
    rows = decided_rows(conn, run_list)
    companies, merged, linkedin_cleared = _companies(rows, None)
    print(f"Total companies: {len(companies)}, Merged: {merged}, LinkedIn cleared: {linkedin_cleared}")

    verified = [c for c in companies if not c["missing_fields"]]
    incomplete = [c for c in companies if c["missing_fields"]]
    print(f"Verified: {len(verified)}, Incomplete: {len(incomplete)}")

    # Get latest identity per board (deduped)
    identities = _latest_identity_per_board(conn, run_list)
    print(f"Latest identities: {len(identities)}")

    # Get latest field evidence per board per field
    field_evidence = _latest_field_evidence_per_board_field(conn, run_list)

    # Build confirmed rows with deduped data
    confirmed_rows = []
    for r in rows:
        if r["identity_status"] == CONFIRMED and r["verification_status"] in LIVE:
            bid = r["board_id"]
            ident = identities.get(bid)
            if not ident:
                continue
            # Merge identity + field evidence
            row = dict(r)
            row["identity_basis"] = ident["basis"]
            row["identity_name"] = ident["name"]
            for field in ("description", "website_url", "linkedin_url", "careersite_url", "company_name"):
                ev = field_evidence.get((bid, field))
                if ev:
                    row[field] = ev["value"]
            confirmed_rows.append(row)

    print(f"Deduped confirmed rows: {len(confirmed_rows)}")

    # Cluster companies
    clusters = cluster_companies([{
        "name": r["company_name"], "website_url": r["website_url"],
        "website_verified": bool(r["website_url"]), "linkedin_url": r["linkedin_url"],
        "ats_system": r["ats_system"], "ats_code": r["ats_code"]
    } for r in confirmed_rows])

    # Map cluster index -> primary board_id and all board_ids
    primary_board_ids = {}
    cluster_board_ids = {}
    for i, g in enumerate(clusters):
        members = sorted((confirmed_rows[idx] for idx in g), key=lambda r: (
            bool(_missing(r, None)), -sum(bool(r[c]) for c in VERIFIED_COLUMNS),
            -(int(r["jobs_count"] or 0)), r["ats_system"], r["ats_code"].casefold()))
        primary_board_ids[i] = members[0]["board_id"]
        cluster_board_ids[i] = [m["board_id"] for m in members]

    # Clear and insert companies
    with conn:
        conn.execute("DELETE FROM company_boards")
        conn.execute("DELETE FROM companies")

    for i, c in enumerate(companies):
        pbid = primary_board_ids.get(i, c.get("board_id", 0))
        with conn:
            conn.execute("""
                INSERT INTO companies (
                    run, primary_board_id, company_name, description, description_method,
                    ats_system, ats_code, website_url, linkedin_url, careersite_url,
                    status, missing_fields, jobs_count, identity_basis, verification_date, export_label
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                EXPORT_LABEL, pbid, c["company_name"], c["description"],
                c.get("description_method", ""), c["ats_system"], c["ats_code"],
                c["website_url"], c["linkedin_url"], c["careersite_url"],
                "verified" if not c["missing_fields"] else "incomplete",
                c["missing_fields"], c["jobs_count"],
                c["identity_basis"], c["verification_date"], EXPORT_LABEL
            ))

    # Get company IDs
    company_ids = {}
    with conn:
        for i in range(len(companies)):
            pbid = primary_board_ids.get(i)
            row = conn.execute("SELECT id FROM companies WHERE export_label=? AND primary_board_id=?", 
                               (EXPORT_LABEL, pbid)).fetchone()
            if row:
                company_ids[i] = row[0]

    # Link ALL boards in each cluster to the company
    with conn:
        for i, g in enumerate(clusters):
            if i not in company_ids:
                continue
            cid = company_ids[i]
            members = sorted((confirmed_rows[idx] for idx in g), key=lambda r: (
                bool(_missing(r, None)), -sum(bool(r[c]) for c in VERIFIED_COLUMNS),
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
        JOIN identities i ON i.board_id = b.id
        WHERE v.outcome IN ('verified_live','verified_live_zero_jobs')
          AND i.status = 'identity_confirmed'
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