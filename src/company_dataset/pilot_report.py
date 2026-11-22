"""Pilot report: measured per-ATS yields with 95% intervals, and what they imply for the pool.

    python -m company_dataset.pilot_report --db data/store.db --pilot data/pilots/pilot1 --export data/exports/pilot1

Projections are estimates from a random sample, labelled as such; they verify nothing.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
from collections import Counter
from pathlib import Path

from company_dataset import store
from company_dataset.enrichment import names_match
from company_dataset.normalize import host_of
from company_dataset.resolve import slug_matches

GEO = {"US": "US", "GB": "UK", "UK": "UK", "CA": "CA"}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - r) / d, (c + r) / d)


def consistency_flags(r: dict) -> list[str]:
    """Cheap cross-checks on a complete row; a flag means "look at this", not "wrong"."""
    flags = []
    label = host_of(r["website_url"]).removeprefix("www.").split(".")[0]
    if not (names_match(label, r["name"]) or slug_matches(label, r["name"], None)):
        flags.append("domain_label_unlike_name")
    slug = r["linkedin_url"].rstrip("/").rsplit("/", 1)[-1]
    if not slug.isdigit() and not slug_matches(slug, r["name"], r["website_url"]):
        flags.append("linkedin_slug_unlike_name_and_domain")
    return flags


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="company_dataset.pilot_report")
    p.add_argument("--db", required=True)
    p.add_argument("--pilot", required=True)
    p.add_argument("--export", required=True)
    a = p.parse_args(argv)
    pilot, export = Path(a.pilot), Path(a.export)
    conn = store.connect(a.db)
    pool = dict(conn.execute("SELECT ats, count(*) FROM boards GROUP BY ats").fetchall())
    rows = list(csv.DictReader(open(pilot / "results.csv", newline="", encoding="utf-8")))
    rep = json.loads((pilot / "report.json").read_text())
    counts = json.loads((export / "counts.json").read_text())
    comp_by_board = {r["board_id"]: r for r in csv.DictReader(open(export / "companies_complete.csv", newline="", encoding="utf-8"))}

    L = []
    A = L.append
    meta = json.loads((pilot / "methodology.json").read_text()) if (pilot / "methodology.json").exists() else {"args": {"seed": 20261031, "run": pilot.name}}
    A(f"# Enrichment {meta['args'].get('run', pilot.name)}: measured results")
    A("")
    excl = ", excluding boards of " + ", ".join(meta["args"]["exclude_run"]) if meta["args"].get("exclude_run") else ""
    A(f"Sample: {len(rows):,} boards, up to 300 per ATS, stratified by prior verification state × first source (seed {meta['args']['seed']}{excl}). Every board was re-checked live.")
    A(f"Wall time {sum(rep['timings_seconds'].values())/60:.0f} min; requests: " + ", ".join(f"{k} {v:,}" for k, v in sorted(rep["costs"].items())) + ".")
    A("")
    A("## Funnel per ATS (sample counts)")
    A("")
    A("| ATS | pool boards | sampled | live now | identity confirmed | name | website | LinkedIn | description | careersite verified | complete | complete 95% CI | projected complete in pool (95% CI) |")
    A("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    tot_pt = tot_var = 0.0
    for ats, s in sorted(rep["by_ats"].items()):
        n, k = s["sampled"], s["complete_strict"]
        lo, hi = wilson(k, n)
        N = pool.get(ats, 0)
        tot_pt += N * k / n
        # Stratified estimator: each ATS is a stratum sampled without replacement.
        tot_var += (N ** 2) * (k / n) * (1 - k / n) / max(n - 1, 1) * (1 - n / N if N else 0)
        f = s["field_verified_of_live"]
        A(f"| {ats} | {N:,} | {n} | {s['live']} | {s['identity'].get('identity_confirmed', 0)} | {f['name']} | {f['website_url']} | {f['linkedin_url']} | {f['description']} | {f['careersite_url_verified']} | **{k}** | {lo:.1%}–{hi:.1%} | {N*k/n:,.0f} ({N*lo:,.0f}–{N*hi:,.0f}) |")
    A("")
    half = 1.96 * math.sqrt(tot_var)
    A(f"**Projected complete boards across all {sum(pool.values()):,} candidate boards: ≈{tot_pt:,.0f}** (95% CI {tot_pt - half:,.0f}–{tot_pt + half:,.0f}, stratified estimator). These are boards before cross-ATS deduplication, and an estimate from the sample, not verified records.")
    A("")
    A("## Geography of complete boards (sample)")
    A("")
    geo_all = Counter()
    A("| ATS | US | UK | CA | other | unknown |")
    A("|---|---|---|---|---|---|")
    for ats, s in sorted(rep["by_ats"].items()):
        g = s["complete_strict_by_geo"]
        geo_all.update(g)
        A(f"| {ats} | {g.get('US', 0)} | {g.get('UK', 0)} | {g.get('CA', 0)} | {g.get('other', 0)} | {g.get('unknown', 0)} |")
    A("")
    us_uk_ca_pt = sum(pool[ats] * sum(s["complete_strict_by_geo"].get(x, 0) for x in ("US", "UK", "CA")) / s["sampled"] for ats, s in rep["by_ats"].items())
    A(f"Projected complete boards in the US/UK/CA across the pool: ≈{us_uk_ca_pt:,.0f}. Country comes from BigPicture or Wikidata by verified domain, or from job locations; 'unknown' complete rows are not counted toward US/UK/CA.")
    A("")
    A("## Zero-job boards")
    A("")
    A("| ATS | live with jobs | live, zero jobs | complete with jobs | complete, zero jobs |")
    A("|---|---|---|---|---|")
    for ats, s in sorted(rep["by_ats"].items()):
        A(f"| {ats} | {s['live_with_jobs']} | {s['live'] - s['live_with_jobs']} | {s['complete_strict_with_jobs']} | {s['complete_strict'] - s['complete_strict_with_jobs']} |")
    A("")
    A("## Identity outcomes among live boards (sample)")
    A("")
    for ats, s in sorted(rep["by_ats"].items()):
        A(f"- {ats}: " + ", ".join(f"{k.replace('identity_', '')} {v}" for k, v in sorted(s["identity"].items())))
    A("")
    A("## How values were obtained (live boards, sample)")
    A("")
    meth = {k: Counter() for k in ("website_method", "linkedin_method", "description_method")}
    for s in rep["by_ats"].values():
        for k in meth:
            meth[k].update(s["methods"][k])
    for k, c in meth.items():
        A(f"- {k.replace('_method', '')}: " + ", ".join(f"{m} {v}" for m, v in c.most_common()))
    A("")
    A("## Possible incorrect matches")
    A("")
    flags = Counter()
    for s in rep["by_ats"].values():
        flags.update(s["flags"])
    A("Automatic cross-checks on all sampled boards: " + (", ".join(f"{k} {v}" for k, v in flags.most_common()) or "none") + ".")
    cflags = Counter()
    flagged = []
    for r in comp_by_board.values():
        fl = consistency_flags({"name": r["company_name"], "website_url": r["website_url"], "linkedin_url": r["linkedin_url"]})
        cflags.update(fl)
        if fl:
            flagged.append((r, fl))
    A(f"Consistency checks on the {len(comp_by_board)} complete companies: " + (", ".join(f"{k} {v}" for k, v in cflags.most_common()) or "none") + ". A flag means the domain or LinkedIn slug doesn't resemble the name (often legitimate, e.g. acronyms or rebrands); `review_sample.csv` lists these plus a random sample for manual review.")
    rnd = random.Random(7)
    sample = rnd.sample(list(comp_by_board.values()), min(60, len(comp_by_board)))
    with open(pilot / "review_sample.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["why", "ats_system", "ats_code", "company_name", "website_url", "linkedin_url", "description", "identity_basis", "website_method", "linkedin_method"])
        for r, fl in flagged:
            w.writerow([";".join(fl), r["ats_system"], r["ats_code"], r["company_name"], r["website_url"], r["linkedin_url"], r["description"][:160], r["identity_basis"], r["website_method"], r["linkedin_method"]])
        for r in sample:
            w.writerow(["random", r["ats_system"], r["ats_code"], r["company_name"], r["website_url"], r["linkedin_url"], r["description"][:160], r["identity_basis"], r["website_method"], r["linkedin_method"]])
    A("")
    A("## Export from the store (this pilot run)")
    A("")
    for k in ("candidate_boards_in_run", "live_boards", "identity_confirmed_live_boards", "unique_companies", "complete_companies"):
        A(f"- {k.replace('_', ' ')}: {counts[k]:,}")
    (pilot / "pilot_report.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
