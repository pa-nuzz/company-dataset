#!/usr/bin/env python3
"""Quality filters (append-only: flags stored, demotions applied at populate, no evidence deleted).
Rules: (a) description is job-posting text, (b) description announces a different entity,
(c) SmartRecruiters suffix duplicates -> review CSV, no auto-merge."""
import csv
import sqlite3
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, "src")
from company_dataset import store

OUT = Path("data/review")
RUN = "quality_filter_2026-11-04"

JOB_TEXT = re.compile(
    r"(\bapply by\b|\bdeadline\b|\brequisition\b|\breq[ .#]\d|\bwe are hiring\b|\bjoin our team\b|\bapply now\b"
    r"|\bopen position\b|\bjob description\b|\bJD\b|\byears of experience\b|\bexperience required\b"
    r"|\bresponsibilities\b|\bqualified candidates\b|\bbenefits package\b|\$[\d,]{4,})", re.I)

ABOUT_ENTITY = re.compile(r"^[\W\d_]*(?:about| introduction)[\s:]+(?:us\b|the company\b)?([A-Z][A-Za-z&'’\.\- ]{2,50})", re.I)

def main():
    conn = store.connect("data/store.db")
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT id, primary_board_id, company_name, description, ats_system, ats_code, status FROM companies")]
    verified = [r for r in rows if r["status"] == "verified" and (r["description"] or "").strip()]

    job_flag, entity_flag = [], []
    for r in verified:
        d = (r["description"] or "").strip()
        if JOB_TEXT.search(d):
            job_flag.append(r)
        m = ABOUT_ENTITY.match(d)
        if m and m.group(1):
            cand = m.group(1).strip().lower()
            name = (r["company_name"] or "").lower()
            cn = re.sub(r"[^a-z0-9]", "", name)
            cd = re.sub(r"[^a-z0-9]", "", cand)
            if cn and cd and cn not in cd and cd not in cn and not cd.startswith(cn[:6]):
                entity_flag.append(r)

    OUT.mkdir(parents=True, exist_ok=True)
    for label, rowsf in (("job_posting_text", job_flag), ("foreign_entity", entity_flag)):
        print(f"[filter {label}] detected: {len(rowsf)}")
        for r in rowsf[:20]:
            print(f"   {r['ats_system']}:{r['ats_code']} | {r['company_name'][:38]} | {(r['description'] or '')[:130]}")

    flags = {}
    for r in job_flag + entity_flag:
        flags[str(r["primary_board_id"])] = r["id"]
    (OUT / "filter_flags_2026-11-04.json").write_text(json.dumps(flags, indent=1))
    demote_ids = sorted({r["id"] for r in job_flag + entity_flag if r["status"] == "verified"})
    # demote at exporter level: newest verified evidence row for description with empty value
    # (evidence rows are append-only; nothing is deleted, the filter run is traceable)
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with conn:
        for r in job_flag + entity_flag:
            if r["status"] != "verified":
                continue
            conn.execute("""INSERT OR IGNORE INTO field_evidence
                (board_id, field, value, method, evidence_url, evidence_text, status, observed_at, run)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (r["primary_board_id"], "description", "", "quality_filter_jobposting" if r in job_flag else "quality_filter_foreign_entity",
                 "", "demoted by quality filter (see demote_rows_filter_2026-11-04.csv)", "verified", now, RUN))
    with (OUT / "demote_rows_filter_2026-11-04.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["company_id", "primary_board_id", "company_name", "ats_system", "ats_code", "rule"])
        for r in job_flag:
            w.writerow([r["id"], r["primary_board_id"], r["company_name"], r["ats_system"], r["ats_code"], "job_posting_text"])
        for r in entity_flag:
            w.writerow([r["id"], r["primary_board_id"], r["company_name"], r["ats_system"], r["ats_code"], "foreign_entity"])
    print(f"[filter] to demote: {len(demote_ids)} (flag file + demote csv written)")

    # SR suffix duplicates: ats_code pairs where one extends the other by digits ("IFS" vs "IFS1")
    sr = [(r["company_name"], r["ats_code"], r["id"]) for r in verified if r["ats_system"] == "smartrecruiters"]
    suffix_pairs = []
    by_base_code = {}
    for name, code, cid in sr:
        base_code = re.sub(r"\d+$", "", code)
        if not base_code or base_code == code:
            continue
        by_base_code.setdefault(base_code, {}).setdefault(code, []).append((name, cid))
    for base_code, variants in by_base_code.items():
        root = variants.get(base_code)
        if not root or len(variants) < 2:
            continue
        for code, (name, cid) in variants.items():
            if code != base_code:
                suffix_pairs.append((root[0][0], base_code, name, code))
    with (OUT / "possible_duplicates_sr_suffix.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["base_name", "base_ats_code", "suffix_name", "suffix_ats_code", "note"])
        for base, base_code, name, code in suffix_pairs:
            w.writerow([base, base_code, name, code, "flag_for_review_no_auto_merge"])
    print(f"[filter] SR suffix duplicate pairs: {len(suffix_pairs)} -> data/review/possible_duplicates_sr_suffix.csv (no auto-merge)")

if __name__ == "__main__":
    main()
