from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

from company_dataset import reference as reference_mod
from company_dataset.enrichment import (
    ASHBY,
    GREENHOUSE,
    LEVER,
    board_from_validation,
    is_auxiliary_board,
    is_test_board,
    name_key,
    names_match,
)
from company_dataset.normalize import BREEZY, SMARTRECRUITERS
from company_dataset.platforms import EXTRA_ATS
from company_dataset.models import REQUIRED_FIELDS, utc_now, load_candidates
from company_dataset.normalize import canonical_api_url, host_of, validation_endpoint as _validation_endpoint
from company_dataset.resolve import resolve_board, slug_matches

COMPANY_COLUMNS = [
    "name",
    "description",
    "linkedin_url",
    "website_url",
    "careersite_url",
    "ats_system",
    "ats_code",
    "country",
    "jobs_count",
    "also_on_ats",
    "other_boards",
    "name_source",
    "description_source",
    "linkedin_source",
    "website_source",
    "country_source",
    "validated_at",
    "sources",
    # Evidence columns (Stage C)
    "http_status",
    "checked_at",
    "endpoint",
    "verification_tier",
]

REJECT_COLUMNS = [
    "source",
    "reason",
    "detail",
    "row_ref",
    "ats_system",
    "ats_code",
    "name",
    "careersite_url",
    "raw_json",
]

# The same description on this many unrelated companies is template text, not theirs.
SHARED_DESCRIPTION_LIMIT = 3


def _verification_tier(ats: str, outcome: dict, board, res, cand: Candidate | None = None) -> str:
    """Determine verification tier per Stage C rules.
    
    A = jobs > 0 with no Lever region conflict
    B = Lever with jobs > 0 where candidate region differs from region_answered
    C = zero jobs with independent association evidence:
        - Breezy: always (validation required hosted-page identity for empty boards)
        - Greenhouse: only if names_match(outcome company_name, candidate/source name) is true; if no candidate name, blank
        - Lever/Ashby/SmartRecruiters zero-job: blank
    No tier for unresolved or invalid records.
    """
    jobs_count = outcome.get("jobs_count") or 0
    status = outcome.get("status")
    
    if status != "valid":
        return ""
    
    if jobs_count > 0:
        # Tier B: Lever with region mismatch (candidate region != region_answered)
        if ats == LEVER:
            cand_region = (cand.region or "us").lower() if cand else "us"
            # region_answered: "eu" for EU, None for US (default to "us")
            region_answered = (outcome.get("region_answered") or "us").lower()
            if cand_region != region_answered:
                return "B"
        # Tier A: jobs > 0 with no Lever region conflict
        return "A"
    
    # jobs_count == 0: Tier C with conditions
    if ats == BREEZY:
        # The validator only marks an empty Breezy /json valid after the hosted page
        # names the company, so every valid zero-job Breezy board already has that evidence.
        return "C"
    
    if ats == GREENHOUSE:
        # Greenhouse C only if names_match(outcome company_name, candidate/source name) is true
        # If no candidate name, blank
        if cand and cand.name:
            outcome_name = outcome.get("company_name") or ""
            if outcome_name and names_match(outcome_name, cand.name):
                return "C"
        return ""
    
    # Lever/Ashby/SmartRecruiters zero-job: blank
    if ats in (LEVER, ASHBY, SMARTRECRUITERS):
        return ""

    # Added platforms: C only when the board's own API/page names a company that matches the source name.
    if ats in EXTRA_ATS:
        outcome_name = outcome.get("company_name") or ""
        if cand and cand.name and outcome_name and names_match(outcome_name, cand.name):
            return "C"
        return ""
    
    return ""


def _read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return default


def _load_source_rejects(dirs: dict) -> list[list]:
    path = dirs["intermediate"] / "source_rejects.csv"
    rows: list[list] = []
    if not path.exists():
        return rows
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            rows.append([
                row["source"],
                row["reason"],
                row["detail"],
                row["row_ref"],
                row.get("ats") or "",
                row.get("code") or "",
                "",
                "",
                row.get("raw_json") or "",
            ])
    return rows


def missing_fields(row: dict) -> list[str]:
    return [f for f in REQUIRED_FIELDS if not str(row.get(f) or "").strip()]


def _filled(row: dict) -> int:
    return len(REQUIRED_FIELDS) - len(missing_fields(row))


def drop_conflicting_linkedin(rows: list[dict]) -> int:
    """A LinkedIn page shared by different websites belongs to at most the rows it names."""
    by_li: dict[str, set] = defaultdict(set)
    for row in rows:
        if row["linkedin_url"]:
            by_li[row["linkedin_url"].lower()].add(host_of(row["website_url"]))
    dropped = 0
    for row in rows:
        li = row["linkedin_url"]
        if li and len(by_li[li.lower()]) > 1:
            slug = li.rsplit("/", 1)[-1]
            if not slug_matches(slug, row["name"], row["website_url"]):
                row["linkedin_url"] = ""
                row["linkedin_source"] = ""
                dropped += 1
    return dropped


def drop_shared_descriptions(rows: list[dict]) -> int:
    by_desc: dict[str, set] = defaultdict(set)
    for row in rows:
        if row["description"]:
            by_desc[row["description"].casefold()].add(host_of(row["website_url"]) or row["ats_code"])
    dropped = 0
    for row in rows:
        d = row["description"]
        if d and len(by_desc[d.casefold()]) >= SHARED_DESCRIPTION_LIMIT:
            row["description"] = ""
            row["description_source"] = ""
            dropped += 1
    return dropped


def _jobs(row: dict) -> int:
    return int(row["jobs_count"]) if str(row["jobs_count"]).isdigit() else -1


UNCERTAIN_REASONS = {
    "total_found_zero",
    "company_identifier_mismatch",
    "exists_no_identity",
    "http_404_unrecognized",
    "unexpected_schema",
    "invalid_json",
    "hosted_page_http_404",
    "hosted_page_request_error",
    "request_error",
    "http_",
    "validation_unresolved",
    "not_validated",
}

DEAD_REASONS = {
    "board_not_found",
    "careersite_unavailable",
    "validation_failed",
}


def _write_uncertain_and_dead_companies(review: Path, candidates: list, cache: dict, careersite_unavailable_keys: set[str], no_identity_keys: set[str] = frozenset()):
    """Write uncertain_companies.csv and dead_companies.csv with reason column.
    
    Only validation outcomes go here:
    - uncertain: not_validated, validation_unresolved (total_found_zero, etc.)
    - dead: board_not_found (validation_failed), careersite_unavailable (valid boards with dead hosted pages)
    
    Export-time rejections (test_or_demo_board, auxiliary_board, duplicate_company_board, 
    source_marked_dead, unparseable_url, unsupported_ats) are only in rejected_records.csv.
    """
    uncertain_rows = []
    dead_rows = []

    # Process all candidates for validation outcomes only
    for cand in candidates:
        outcome = cache.get(cand.key)
        if outcome is None:
            # Not validated - goes to uncertain
            uncertain_rows.append({
                "source": "; ".join(cand.sources),
                "reason": "not_validated",
                "detail": "no validation result; run validate stage",
                "ats_system": cand.ats,
                "ats_code": cand.code,
                "name": cand.name or "",
                "careersite_url": "; ".join(cand.careersite_urls),
                "http_status": "",
                "checked_at": "",
                "endpoint": "",
                "error": "not_validated",
            })
            continue

        status = outcome.get("status")
        error = outcome.get("error") or ""
        http_status = outcome.get("http_status") or ""
        checked_at = outcome.get("checked_at") or ""

        if status == "valid" and cand.key in no_identity_keys:
            uncertain_rows.append({
                "source": "; ".join(cand.sources),
                "reason": "identity_not_established",
                "detail": "board is live but no source or vendor names the company",
                "ats_system": cand.ats,
                "ats_code": cand.code,
                "name": "",
                "careersite_url": "; ".join(cand.careersite_urls),
                "http_status": str(outcome.get("http_status") or ""),
                "checked_at": outcome.get("checked_at") or "",
                "endpoint": _validation_endpoint(cand.ats, cand.code, outcome),
                "error": "identity_not_established",
            })
            continue
        if status == "valid":
            # Check if this valid board was rejected as careersite_unavailable
            if cand.key in careersite_unavailable_keys:
                dead_rows.append({
                    "source": "; ".join(cand.sources),
                    "reason": "careersite_unavailable",
                    "detail": "vendor API answers but the hosted board page is gone",
                    "ats_system": cand.ats,
                    "ats_code": cand.code,
                    "name": cand.name or "",
                    "careersite_url": "; ".join(cand.careersite_urls),
                    "http_status": str(outcome.get("http_status") or ""),
                    "checked_at": outcome.get("checked_at") or "",
                    "endpoint": _validation_endpoint(cand.ats, cand.code, outcome),
                    "error": "careersite_unavailable",
                })
            continue

        row = {
            "source": "; ".join(cand.sources),
            "reason": error if error else status,
            "detail": f'http={outcome.get("http_status")} error={error or ""}',
            "ats_system": cand.ats,
            "ats_code": cand.code,
            "name": cand.name or "",
            "careersite_url": "; ".join(cand.careersite_urls),
            "http_status": str(http_status) if http_status else "",
            "checked_at": outcome.get("checked_at") or "",
            "endpoint": _validation_endpoint(cand.ats, cand.code, outcome),
            "error": error if error else status,
        }

        # Classify based on EXACT reason matching
        reason = row["reason"]
        if reason in DEAD_REASONS:
            dead_rows.append(row)
        elif reason in UNCERTAIN_REASONS or status == "unresolved" or status == "not_validated":
            uncertain_rows.append(row)
        else:
            # Default to uncertain for any other non-valid status
            uncertain_rows.append(row)

    # Write uncertain_companies.csv
    if uncertain_rows:
        review.mkdir(parents=True, exist_ok=True)
        uncertain_cols = ["source", "reason", "detail", "ats_system", "ats_code", "name", "careersite_url", "http_status", "checked_at", "endpoint", "error"]
        with open(review / "uncertain_companies.csv", "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=uncertain_cols)
            writer.writeheader()
            writer.writerows(uncertain_rows)

    # Write dead_companies.csv
    if dead_rows:
        review.mkdir(parents=True, exist_ok=True)
        dead_cols = ["source", "reason", "detail", "ats_system", "ats_code", "name", "careersite_url", "http_status", "checked_at", "endpoint", "error"]
        with open(review / "dead_companies.csv", "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=dead_cols)
            writer.writeheader()
            writer.writerows(dead_rows)


def _jobs(row: dict) -> int:
    return int(row["jobs_count"]) if str(row["jobs_count"]).isdigit() else -1


def collapse_companies(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """One row per company, across all ATSes.

    Boards are the same company only when they share a strong key (registrable website
    domain, LinkedIn page) AND their names match. A shared name alone never merges boards,
    and neither does a shared domain under different names (affiliates, franchise sites, or
    one wrong website). The most complete board is kept, then the one with the most jobs;
    the rest are listed in `other_boards` and returned as extras.
    """
    from company_dataset.identity import cluster_companies

    clusters = cluster_companies([{**r, "website_verified": bool(r["website_url"])} for r in rows])
    groups: dict[int, list[dict]] = {g[0]: [rows[i] for i in g] for g in clusters}
    keep: list[dict] = []
    extras: list[dict] = []
    for group in groups.values():
        group.sort(key=lambda r: (-_filled(r), -_jobs(r), r["ats_system"], r["ats_code"].casefold()))
        primary = group[0]
        others = group[1:]
        primary["also_on_ats"] = ";".join(sorted({r["ats_system"] for r in others} - {primary["ats_system"]}))
        primary["other_boards"] = ";".join(f"{r['ats_system']}:{r['ats_code']}" for r in others)
        keep.append(primary)
        extras.extend(others)
    return keep, extras


DETAILED_COLUMNS = COMPANY_COLUMNS + ["complete", "missing_fields"]


def _by_name(row: dict):
    return ((row["name"] or "~").casefold(), row["ats_system"], row["ats_code"].casefold())


def write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _check_overwrite(dirs: dict, run_dir: str | None = None) -> list[Path]:
    """Check if any target files already exist. Returns list of existing files.
    
    In default mode (no --run-dir): checks output/, review/, reports/ under dirs.
    With --run-dir: checks the run_dir/output, run_dir/review, run_dir/reports.
    """
    if run_dir is not None:
        run_path = Path(run_dir)
        targets = [
            run_path / "output" / "companies_complete.csv",
            run_path / "output" / "companies_incomplete.csv",
            run_path / "output" / "companies_total.csv",
            run_path / "output" / "source_manifest.csv",
            run_path / "review" / "companies_detailed.csv",
            run_path / "review" / "rejected_records.csv",
            run_path / "review" / "uncertain_companies.csv",
            run_path / "review" / "dead_companies.csv",
            run_path / "review" / "possible_duplicates.csv",
            run_path / "reports" / "quality_report.md",
        ]
    else:
        output = dirs["output"]
        review = Path(dirs.get("review") or Path(dirs["data"]) / "review")
        reports = dirs["reports"]
        targets = [
            output / "companies_complete.csv",
            output / "companies_incomplete.csv",
            output / "companies_total.csv",
            output / "source_manifest.csv",
            Path(dirs.get("review") or Path(dirs["data"]) / "review") / "companies_detailed.csv",
            Path(dirs.get("review") or Path(dirs["data"]) / "review") / "rejected_records.csv",
            Path(dirs.get("review") or Path(dirs["data"]) / "review") / "uncertain_companies.csv",
            Path(dirs.get("review") or Path(dirs["data"]) / "review") / "dead_companies.csv",
            Path(dirs.get("review") or Path(dirs["data"]) / "review") / "possible_duplicates.csv",
            dirs["reports"] / "quality_report.md",
        ]
    
    existing = [p for p in targets if p.exists()]
    return existing


def run_export(dirs: dict, ref=None, run_dir: str | None = None) -> dict:
    # No-overwrite check for both default mode and --run-dir
    existing = _check_overwrite(dirs, run_dir)
    if existing:
        paths = ", ".join(str(p) for p in existing)
        raise FileExistsError(f"Target files already exist (use --run-dir or remove them): {paths}")
    
    # No-overwrite check for --run-dir (directory itself)
    if run_dir is not None:
        run_path = Path(run_dir)
        if run_path.exists():
            raise FileExistsError(f"Run directory already exists: {run_dir}")
        # Create the directory structure
        run_path.mkdir(parents=True, exist_ok=True)
        # Override the output and review directories to use run_dir
        dirs = dict(dirs)
        dirs["output"] = run_path / "output"
        dirs["review"] = run_path / "review"
        dirs["reports"] = run_path / "reports"
        for d in (dirs["output"], dirs["review"], dirs["reports"]):
            d.mkdir(parents=True, exist_ok=True)
    
    candidates = load_candidates(dirs)
    cache = _read_json(dirs["cache"] / "validation.json", {}) or {}
    pages = _read_json(dirs["cache"] / "pages.json", {}) or {}
    ref = ref if ref is not None else reference_mod.load_saved(dirs)
    build_stats = _read_json(dirs["intermediate"] / "build_stats.json", {}) or {}
    manifest = _read_json(dirs["intermediate"] / "manifest.json", []) or []

    live: list[dict] = []
    validation_rejects: list[list] = []
    careersite_unavailable_keys: set[str] = set()
    no_identity_keys: set[str] = set()
    for cand in candidates:
        ats, code = cand.ats, cand.code
        sources = "; ".join(cand.sources)
        careersites = "; ".join(cand.careersite_urls)
        outcome = cache.get(cand.key)
        if outcome is None:
            validation_rejects.append([sources, "not_validated", "no validation result; run validate stage", "", ats, code, cand.name or "", careersites, ""])
            continue
        if outcome.get("status") != "valid":
            reason = "validation_failed" if outcome.get("status") == "invalid" else "validation_unresolved"
            detail = f'http={outcome.get("http_status")} error={outcome.get("error") or ""}'
            validation_rejects.append([sources, reason, detail, "", ats, code, cand.name or "", careersites, ""])
            continue
        board = board_from_validation(cand, outcome)
        res = resolve_board(board, outcome, pages, ref, source_name=cand.name or "", source_name_origin=cand.name_source)
        if any(is_test_board(n, board.code) for n in (res.name, outcome.get("company_name"), cand.name)):
            validation_rejects.append([sources, "test_or_demo_board", "vendor sandbox / demo / test board, not a company", "", ats, board.code, res.name or outcome.get("company_name") or "", board.careersite_url, ""])
            continue
        if any(is_auxiliary_board(n) for n in (res.name, outcome.get("company_name"))):
            validation_rejects.append([sources, "auxiliary_board", "internal / referral / talent-pool board, not the company's main board", "", ats, board.code, res.name or outcome.get("company_name") or "", board.careersite_url, ""])
            continue
        if res.careersite_status == "dead":
            careersite_unavailable_keys.add(cand.key)
            validation_rejects.append([sources, "careersite_unavailable", "vendor API answers but the hosted board page is gone", "", ats, board.code, res.name, board.careersite_url, ""])
            continue
        if not res.name:
            # A live board alone doesn't say whose it is; without a name it isn't a verified company.
            no_identity_keys.add(cand.key)
            validation_rejects.append([sources, "identity_not_established", "board is live but no source or vendor names the company", "", ats, board.code, "", board.careersite_url, ""])
            continue
        # Evidence columns
        http_status = outcome.get("http_status")
        checked_at = outcome.get("checked_at") or ""
        endpoint = _validation_endpoint(ats, code, outcome)
        verification_tier = _verification_tier(ats, outcome, board, res, cand)
        if not verification_tier:
            # Live but empty, and nothing beyond a list name says whose board it is.
            no_identity_keys.add(cand.key)
            validation_rejects.append([sources, "identity_not_established", "live board with zero jobs and no independent identity evidence", "", ats, board.code, res.name, board.careersite_url, ""])
            continue

        live.append({
            "name": res.name,
            "description": res.description,
            "linkedin_url": res.linkedin_url,
            "website_url": res.website_url,
            "careersite_url": board.careersite_url,
            "ats_system": ats,
            "ats_code": board.code,
            "country": res.country,
            "jobs_count": outcome.get("jobs_count") if outcome.get("jobs_count") is not None else "",
            "also_on_ats": "",
            "other_boards": "",
            **res.sources,
            "validated_at": checked_at,
            "sources": sources,
            # Evidence columns
            "http_status": http_status if http_status is not None else "",
            "checked_at": checked_at,
            "endpoint": endpoint,
            "verification_tier": verification_tier,
        })

    linkedin_conflicts = drop_conflicting_linkedin(live)
    shared_descriptions = drop_shared_descriptions(live)
    kept, extra_boards = collapse_companies(live)
    for row in extra_boards:
        validation_rejects.append([row["sources"], "duplicate_company_board", f"same company as a kept board ({host_of(row['website_url']) or row['linkedin_url']})", "", row["ats_system"], row["ats_code"], row["name"], row["careersite_url"], ""])

    for row in kept:
        row["missing_fields"] = ";".join(missing_fields(row))
        row["complete"] = "no" if row["missing_fields"] else "yes"
    kept.sort(key=_by_name)
    complete = [r for r in kept if r["complete"] == "yes"]
    incomplete = [r for r in kept if r["complete"] == "no"]

    # Hand-off files: the required fields first, in the same order in every file, so
    # complete + incomplete line up with total.
    write_csv(dirs["output"] / "companies_complete.csv", list(REQUIRED_FIELDS), complete)
    write_csv(dirs["output"] / "companies_incomplete.csv", list(REQUIRED_FIELDS) + ["missing_fields"], incomplete)
    
    # companies_total.csv and companies_detailed.csv include evidence columns
    EVIDENCE_COLUMNS = ["http_status", "checked_at", "jobs_count", "endpoint", "verification_tier"]
    TOTAL_COLUMNS = list(REQUIRED_FIELDS) + ["complete", "missing_fields"] + EVIDENCE_COLUMNS
    DETAILED_COLUMNS_WITH_EVIDENCE = DETAILED_COLUMNS + EVIDENCE_COLUMNS
    
    write_csv(dirs["output"] / "companies_total.csv", TOTAL_COLUMNS, complete + incomplete)

    # Review files: provenance, rejects and possible duplicates, for checking the work.
    review = Path(dirs.get("review") or Path(dirs["data"]) / "review")
    write_csv(review / "companies_detailed.csv", DETAILED_COLUMNS_WITH_EVIDENCE, complete + incomplete)
    rejects = _load_source_rejects(dirs) + validation_rejects
    review.mkdir(parents=True, exist_ok=True)
    with open(review / "rejected_records.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(REJECT_COLUMNS)
        writer.writerows(rejects)

    # Write uncertain_companies.csv and dead_companies.csv
    _write_uncertain_and_dead_companies(review, candidates, cache, careersite_unavailable_keys, no_identity_keys)

    per_source = build_stats.get("per_source", {})
    contributed = Counter(s for cand in candidates for s in cand.sources)
    source_rows: list[dict] = []
    for entry in manifest:
        name = entry.get("source", "")
        src_stats = per_source.get(name, {})
        source_rows.append({
            "source": name,
            "role": entry.get("role", "boards"),
            "description": entry.get("description", ""),
            "download_url": entry.get("download_url", ""),
            "license": entry.get("license", ""),
            "license_url": entry.get("license_url", ""),
            "license_note": entry.get("license_note", ""),
            "attribution": entry.get("attribution", ""),
            "downloaded_at": entry.get("downloaded_at") or "",
            "sha256": entry.get("sha256", ""),
            "size_bytes": entry.get("size_bytes", ""),
            "rows_read": src_stats.get("rows_read", ""),
            "in_scope_records": src_stats.get("records", ""),
            "rejected_rows": src_stats.get("rejects", ""),
            "contributed_candidates": contributed.get(name, ""),
        })
    with open(dirs["output"] / "source_manifest.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(source_rows[0].keys()) if source_rows else ["source"])
        writer.writeheader()
        writer.writerows(source_rows)

    def completeness(rows):
        total = len(rows)
        return {f: {"non_null": sum(1 for r in rows if r[f]), "total": total, "pct": round(100 * sum(1 for r in rows if r[f]) / total, 2) if total else 0.0} for f in REQUIRED_FIELDS}

    def countries(rows):
        c = Counter(r["country"] for r in rows if r["country"])
        focus = sum(c[k] for k in ("US", "GB", "CA"))
        return {
            "rows_with_country": sum(c.values()),
            "us_uk_ca": focus,
            "us_uk_ca_pct": round(100 * focus / len(rows), 2) if rows else 0.0,
            "breakdown": dict(c.most_common()),
        }

    names = Counter(name_key(r["name"]) for r in kept if len(name_key(r["name"])) >= 3)
    same_name = [r for r in kept if names[name_key(r["name"])] > 1]
    write_csv(
        review / "possible_duplicates.csv",
        ["name", "ats_system", "ats_code", "website_url", "linkedin_url", "careersite_url"],
        sorted(same_name, key=lambda r: (name_key(r["name"]), r["ats_system"], r["ats_code"])),
    )
    reject_reasons = Counter(r[1] for r in validation_rejects)
    live_sources = [[s for s in r["sources"].split("; ") if s] for r in live]
    source_overlap = {
        "by_source_count": dict(sorted(Counter(len(s) for s in live_sources).items())),
        "in_live_boards": dict(Counter(x for s in live_sources for x in s)),
        "sole_source_for": dict(Counter(s[0] for s in live_sources if len(s) == 1)),
    }
    export_stats = {
        "generated_at": utc_now(),
        "live_boards": len(live),
        "live_companies_after_collapse": len(kept),
        "complete_companies": len(complete),
        "complete_unique_websites": len({host_of(r["website_url"]) for r in complete}),
        "incomplete_companies": len(incomplete),
        "complete_by_ats": dict(Counter(r["ats_system"] for r in complete)),
        "live_by_ats": dict(Counter(r["ats_system"] for r in kept)),
        "completeness_live": completeness(kept),
        "missing_field_counts": dict(Counter(f for r in incomplete for f in r["missing_fields"].split(";") if f)),
        "country_complete": countries(complete),
        "country_live": countries(kept),
        "sources_complete": {k: dict(Counter(r[k] for r in complete)) for k in ("name_source", "website_source", "linkedin_source", "description_source", "country_source")},
        "linkedin_conflicts_dropped": linkedin_conflicts,
        "shared_descriptions_dropped": shared_descriptions,
        "boards_merged_into_companies": len(extra_boards),
        "same_name_not_merged": len(same_name),
        "same_name_not_merged_in_deliverable": sum(1 for r in same_name if not missing_fields(r)),
        "post_validation_rejects": dict(reject_reasons),
        "source_overlap": source_overlap,
        "rejected_rows_total": len(rejects),
    }
    # A run dir keeps its own stats so a new run never rewrites data/intermediate.
    stats_dir = Path(run_dir) if run_dir is not None else dirs["intermediate"]
    (stats_dir / "export_stats.json").write_text(json.dumps(export_stats, indent=2))
    print(
        f"[export] live boards={len(live)} companies={len(kept)} complete={len(complete)} "
        f"incomplete={len(incomplete)} rejects={dict(reject_reasons)}"
    )
    return export_stats
