from __future__ import annotations

import csv
import json
from pathlib import Path

from company_dataset.models import REQUIRED_FIELDS, utc_now
from company_dataset.sources import commoncrawl

TARGET_MIN = 20_000

SOURCE_LABELS = {
    "name_source": "Name",
    "website_source": "Website",
    "linkedin_source": "LinkedIn",
    "description_source": "Description",
    "country_source": "Country",
}


def _read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return default


def _n(x) -> str:
    return f"{x:,}" if isinstance(x, int) else str(x)


def _pct(n, d) -> str:
    return f"{100 * n / d:.1f}%" if d else "0.0%"


def _check_report_overwrite(dirs: dict) -> list[Path]:
    """Check if report target file already exists."""
    report_path = dirs["reports"] / "quality_report.md"
    existing = [report_path] if report_path.exists() else []
    return existing


def run_report(dirs: dict, run_dir: str | None = None) -> dict:
    # Handle run_dir - adjust output paths
    if run_dir is not None:
        run_path = Path(run_dir)
        dirs = dict(dirs)
        dirs["reports"] = run_path / "reports"
        dirs["output"] = run_path / "output"
        dirs["review"] = run_path / "review"
        for d in (dirs["reports"], dirs["output"], dirs["review"]):
            d.mkdir(parents=True, exist_ok=True)
    
    # No-overwrite check for report
    existing = _check_report_overwrite(dirs)
    if existing:
        paths = ", ".join(str(p) for p in existing)
        raise FileExistsError(f"Report target file already exists (remove it first): {paths}")
    
    build = _read_json(dirs["intermediate"] / "build_stats.json", {}) or {}
    validation = _read_json(dirs["intermediate"] / "validation_stats.json", {}) or {}
    stats_dir = Path(run_dir) if run_dir is not None else dirs["intermediate"]
    ex = _read_json(stats_dir / "export_stats.json", {}) or {}

    complete = ex.get("complete_companies", 0)
    live = ex.get("live_companies_after_collapse", 0)
    status = validation.get("by_status", {})
    rejects = ex.get("post_validation_rejects", {})
    overlap = ex.get("source_overlap", {})

    L: list[str] = ["# Data quality report", "", f"_Generated {utc_now()}_", ""]

    L += [
        "## The short version",
        "",
        f"`companies_complete.csv` has **{_n(complete)} companies**, each with all seven required fields filled in. "
        f"Another {_n(ex.get('incomplete_companies', 0))} live companies are missing at least one field; they are in "
        f"`companies_incomplete.csv` with the gaps named. `companies_total.csv` has all {_n(live)} live companies, "
        f"one row per company.",
        "",
        f"That is {_n(max(0, TARGET_MIN - complete))} short of the 20,000 we were aiming for. "
        "The section on coverage below explains why we couldn't find more.",
        "",
        "| | Count |",
        "|---|---|",
        f"| Board slugs collected from all sources | {_n(build.get('unique_candidates', 0))} |",
        f"| Still live when checked against the vendor's API | {_n(status.get('valid', 0))} |",
        f"| Gone (404 from the vendor) | {_n(status.get('invalid', 0))} |",
        f"| API answers but the public careers page is gone | {_n(rejects.get('careersite_unavailable', 0))} |",
        f"| Vendor demo, sandbox or test boards (\"Lever Demo 2\", \"Merge API Integration Sandbox\") | {_n(rejects.get('test_or_demo_board', 0))} |",
        f"| A company's secondary board: internal, referrals, talent pool | {_n(rejects.get('auxiliary_board', 0))} |",
        f"| Extra boards folded into a company already listed | {_n(ex.get('boards_merged_into_companies', rejects.get('duplicate_company_board', 0)))} |",
        f"| **Live companies** | **{_n(live)}** |",
        f"| **Complete companies (`companies_complete.csv`)** | **{_n(complete)}** |",
        "",
    ]

    L += ["## How complete the live list is", "", "| Field | Filled | Of | |", "|---|---|---|---|"]
    for f in REQUIRED_FIELDS:
        c = ex.get("completeness_live", {}).get(f, {})
        L.append(f"| `{f}` | {_n(c.get('non_null', 0))} | {_n(c.get('total', 0))} | {c.get('pct', 0)}% |")
    missing = ex.get("missing_field_counts", {})
    if missing:
        worst = sorted(missing.items(), key=lambda kv: -kv[1])
        L += ["", "Among the incomplete rows, the missing fields are " + ", ".join(f"`{k}` ({_n(v)})" for k, v in worst) + ". "
              "LinkedIn is the hardest: if a company's homepage doesn't link to its LinkedIn page and it isn't in the reference data, we leave it blank rather than guess."]
    L.append("")

    L += ["By ATS:", "", "| ATS | Live | Complete |", "|---|---|---|"]
    ATS_LABELS = {
        "greenhouse": "Greenhouse",
        "lever": "Lever",
        "ashby": "Ashby",
        "smartrecruiters": "SmartRecruiters",
        "breezy": "Breezy HR",
        "workable": "Workable",
        "workday": "Workday",
        "bamboohr": "BambooHR",
        "recruitee": "Recruitee",
        "teamtailor": "Teamtailor",
        "personio": "Personio",
        "rippling": "Rippling",
        "icims": "iCIMS",
        "pinpoint": "Pinpoint",
        "join": "JOIN",
    }
    for ats_key, ats_label in ATS_LABELS.items():
        L.append(f"| {ats_label} | {_n(ex.get('live_by_ats', {}).get(ats_key, 0))} | {_n(ex.get('complete_by_ats', {}).get(ats_key, 0))} |")
    L.append("")

    L += [
        "## Duplicates",
        "",
        "A company often has more than one board: a second board for contractors or interns, an old board left behind "
        "after a move from Lever to Ashby, and so on. We treat boards as the same company when they share a website, or "
        "when they share a LinkedIn page *and* have matching names. Each company appears once; its other boards are listed "
        "in `other_boards` in `review/companies_detailed.csv`, and in `review/rejected_records.csv` as `duplicate_company_board`.",
        "",
        f"- Boards folded into another company's row: {_n(ex.get('boards_merged_into_companies', 0))}",
        f"- Companies that share a name with another row but couldn't be confirmed as the same company: "
        f"{_n(ex.get('same_name_not_merged', 0))} ({_n(ex.get('same_name_not_merged_in_deliverable', 0))} of them in `companies_complete.csv`). "
        "These are left separate and listed in `review/possible_duplicates.csv` for a person to check. Names alone aren't enough to merge on; "
        "there are a lot of companies called Atlas.",
        "",
        "Job postings themselves aren't part of this dataset, so the same job appearing on two boards doesn't produce two rows.",
        "",
    ]

    L += ["## Where the values come from", ""]
    for field, counts in ex.get("sources_complete", {}).items():
        label = SOURCE_LABELS.get(field, field)
        parts = ", ".join(f"{k.replace('_', ' ') or 'none'} {_n(v)}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
        L.append(f"- **{label}:** {parts}")
    L += [
        "",
        "`review/companies_detailed.csv` has the same information per row in its `*_source` columns, so any single value can be traced back.",
        "",
    ]

    L += ["## Location", ""]
    for label, key, total in (("In `companies_complete.csv`", "country_complete", complete), ("Across all live companies", "country_live", live)):
        c = ex.get(key, {})
        b = c.get("breakdown", {})
        known = c.get("rows_with_country", 0)
        L.append(
            f"- {label}: {_n(b.get('US', 0))} US, {_n(b.get('GB', 0))} UK, {_n(b.get('CA', 0))} Canada "
            f"({_pct(c.get('us_uk_ca', 0), total)} of rows), {_n(known - c.get('us_uk_ca', 0))} elsewhere, {_n(total - known)} unknown."
        )
    L += [
        "",
        "Country means headquarters country from BigPicture, matched on the company's website. If that's missing we use the "
        "country of the company's job postings, but only when every posting on the board is in the same country.",
        "",
    ]

    L += ["## Sources", "", "| Source | Used for | License | Live boards it lists | Only source for |", "|---|---|---|---|---|"]
    manifest_csv = dirs["output"] / "source_manifest.csv"
    if manifest_csv.exists():
        with open(manifest_csv, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                name = row["source"]
                role = "company details" if row.get("role") == "reference" else "board slugs"
                L.append(
                    f"| {name} | {role} | {row.get('license', '')} | {_n(overlap.get('in_live_boards', {}).get(name, '')) or '-'} | "
                    f"{_n(overlap.get('sole_source_for', {}).get(name, '')) or '-'} |"
                )
    L.append("")

    by_count = overlap.get("by_source_count", {})
    total_live = sum(by_count.values())
    multi = sum(v for k, v in by_count.items() if int(k) >= 2)
    L += [
        "## Why there aren't 20,000",
        "",
        f"The sources overlap heavily. {_pct(multi, total_live)} of live boards turn up in two or more of them, and each new source "
        "we added contributed only a few hundred boards nobody else had. Things we tried beyond the original sources:",
        "",
        f"- Common Crawl's index for the Greenhouse, Lever and Ashby board hosts, across {len(commoncrawl.CRAWLS)} crawls from "
        f"{commoncrawl.CRAWLS[-1][8:12]} to {commoncrawl.CRAWLS[0][8:12]}. (We also tried the vendors' API hosts; they added nothing.)",
        "- The Headcount Labs postings dataset (5.7 million postings), including Greenhouse boards that only appear as postings on a "
        "company's own site, which we resolved by reading the board embedded in those pages.",
        "- MIT-licensed board lists on GitHub (open-ats-feed and smaller ones).",
        "- Guessing slugs from company domains for a random sample of US, UK and Canadian companies. About 1.7% of large companies "
        "had a board, and most of those were already on our list.",
        "",
        "So the number of companies with public boards on these three ATSes that can be found in open data is about 15,000, not "
        "20,000 to 30,000. Getting closer would take either paid company data or mass slug-guessing against the vendors' APIs.",
        "",
        "## Rejected rows",
        "",
        "| Reason | Rows |",
        "|---|---|",
    ]
    for reason, n in sorted(rejects.items(), key=lambda kv: -kv[1]):
        L.append(f"| `{reason}` | {_n(n)} |")
    L += [
        "",
        f"We also removed {_n(ex.get('linkedin_conflicts_dropped', 0))} LinkedIn links that pointed at another company's page, and "
        f"{_n(ex.get('shared_descriptions_dropped', 0))} descriptions that were template text shared by three or more companies.",
        "",
    ]

    # Unresolved & Dead Reasons by ATS
    unresolved_reasons = validation.get("unresolved_reasons", {})
    if unresolved_reasons:
        L += ["## Unresolved & Dead Reasons by ATS", "", "| ATS | Reason | Count |", "|---|---|---|"]
        for ats_key, reasons in unresolved_reasons.items():
            ats_label = ATS_LABELS.get(ats_key, ats_key.title())
            for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
                L.append(f"| {ats_label} | {reason} | {_n(count)} |")
        L.append("")
    
    reports_dir = dirs["reports"]
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "quality_report.md").write_text("\n".join(L), encoding="utf-8")
    print(f"[report] deliverable={complete} live={live} -> {reports_dir / 'quality_report.md'}")
    return {"complete_companies": complete, "live_companies": live}
