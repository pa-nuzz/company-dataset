#!/usr/bin/env python3
"""Populate companies/company_boards from export's own logic (decided_rows + _companies + clusters).
Idempotent: deletes and re-inserts. Statuses are EXACTLY the export CSV statuses (missing_fields)."""
import sqlite3
from collections import defaultdict

from company_dataset import store
from company_dataset.export import decided_rows, _companies, _missing
from company_dataset.identity import CONFIRMED, cluster_companies
from company_dataset.verification import LIVE

DB = "data/store.db"
RUNS = ["fullrun","disc1","sr_fix_test","sr_existence_2026-11-03","identity_fix_2026-11-03","recovery_reverify_2026-11-03",
        "desc_cached_2026-11-03","desc_network_2026-11-03","reenrich_2026-11-03","descriptions_fallback_2026-11-03",
        "discovery_2026-11-03","discovery2_2026-11-03","discovery3_2026-11-03","discovery4_2026-11-03",
        "retry_2026-11-03","ownership_2026-11-03","desc_non_english_2026-11-03","desc_cascade_2026-11-03",
        "vendor_promote_2026-11-03","org_promote_2026-11-03","desc_promoted_cached_2026-11-03","desc_promoted_network_2026-11-03",
        "desc_shadow_fix_2026-11-03","ownership_cycle2_2026-11-03","reresolve_cycle2_2026-11-03","reresolve_cycle2b_2026-11-03",
        "cycle2_enrich_2026-11-03","cycle3_enrich_2026-11-03","cycle3b_enrich_2026-11-03","reresolve_cycle3_2026-11-03",
        "desc_cycle3_2026-11-03","ownership_final_2026-11-03","final_enrich_2026-11-04","reruns_description_2026-11-03",
        "reresolve_final_2026-11-04","reresolve_cycle2_sources_2026-11-04","reverify_all_2026-11-04","reresolve_360_2026-11-04","harvest_2026-11-04","quality_filter_2026-11-04",
        "jobad_about_2026-11-05","audit_providers_2026-11-05","ownership_2026-11-05","reresolve_backlog_2026-11-05",
        "desc_cached2_2026-11-05","desc_network2_2026-11-05","desc_cached3_2026-11-05","desc_cached4_2026-11-05",
        "desc_network3_2026-11-05","desc_network4_2026-11-05",
        "website_content_aggressive","website_content_aggressive2","website_content_aggressive3",
        "website_content_aggressive4","website_content_aggressive5","website_content_aggressive6",
        "website_content_aggressive7",
        "surt_hosts_2026-11-05","pages_js_2026-11-05"]

def main():
    conn = store.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = decided_rows(conn, RUNS)
    confirmed = [r for r in rows if r["identity_status"] == CONFIRMED and r["verification_status"] in LIVE]
    companies, merged, linkedin_cleared = _companies(rows, None)
    verified = [r for r in companies if not r["missing_fields"]]
    incomplete = [r for r in companies if r["missing_fields"]]
    unresolved = [r for r in rows if r["identity_status"] in ("identity_unresolved", "identity_mismatch") and r["verification_status"] in LIVE]
    print(f"rows={len(rows)} confirmed={len(confirmed)} verified={len(verified)} incomplete={len(incomplete)} unresolved={len(unresolved)}")

    # cluster members (mirror _companies' clustering + member ordering)
    idx_clusters = cluster_companies([{"name": r["company_name"], "website_url": r["website_url"], "website_verified": bool(r["website_url"]),
                                       "linkedin_url": r["linkedin_url"], "ats_system": r["ats_system"], "ats_code": r["ats_code"]} for r in confirmed])
    member_map = {}  # primary board_id -> members
    for g in idx_clusters:
        members = sorted((confirmed[i] for i in g), key=lambda r: (bool(_missing(r, None)), -sum(bool(r[c]) for c in
                         ["company_name","description","linkedin_url","website_url","careersite_url"]), -(int(r["jobs_count"] or 0)), r["ats_system"], r["ats_code"].casefold()))
        member_map[members[0]["board_id"]] = members

    with conn:
        conn.execute("DELETE FROM company_boards")
        conn.execute("DELETE FROM companies")
        for i, r in enumerate(sorted(companies, key=lambda r: r["company_name"].casefold())):
            missing = set(r["missing_fields"].split(";")) if r["missing_fields"] else set()
            if missing == {""}:
                missing = set()
            conn.execute("""INSERT INTO companies (id, run, primary_board_id, company_name, description, description_method,
                          ats_system, ats_code, website_url, linkedin_url, careersite_url, status, missing_fields,
                          jobs_count, identity_basis, verification_date, export_label)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (i + 1, "populate_final7", r["board_id"], r["company_name"], r["description"], r["description_method"],
                           r["ats_system"], r["ats_code"], r["website_url"], r["linkedin_url"], r["careersite_url"],
                           "verified" if not missing else "incomplete", r["missing_fields"], r["jobs_count"],
                           r["identity_basis"], r["verification_date"], "final_2026-11-04"))
            cid = i + 1
            for m in member_map.get(r["board_id"], [r]):
                if m["board_id"] == r["board_id"]:
                    reason = "primary"
                elif m["linkedin_url"] and r["linkedin_url"] and m["linkedin_url"] == r["linkedin_url"]:
                    reason = "same_linkedin_and_name"
                else:
                    reason = "same_domain_and_name"
                conn.execute("INSERT OR IGNORE INTO company_boards (company_id, board_id, link_reason) VALUES (?,?,?)",
                             (cid, m["board_id"], reason))
        conn.execute("DROP VIEW IF EXISTS v_verified_companies")
        conn.execute("DROP VIEW IF EXISTS v_incomplete_companies")
        conn.execute("CREATE VIEW v_verified_companies AS SELECT * FROM companies WHERE status='verified'")
        conn.execute("CREATE VIEW v_incomplete_companies AS SELECT * FROM companies WHERE status='incomplete'")
    vc = conn.execute("SELECT COUNT(*) FROM companies WHERE status='verified'").fetchone()[0]
    ic = conn.execute("SELECT COUNT(*) FROM companies WHERE status='incomplete'").fetchone()[0]
    cb = conn.execute("SELECT COUNT(*) FROM company_boards").fetchone()[0]
    print(f"DB verified={vc} incomplete={ic} company_boards={cb} (export: {len(verified)}/{len(incomplete)}) match={vc == len(verified) and ic == len(incomplete)}")

if __name__ == "__main__":
    main()
