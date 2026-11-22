"""Export: deliverable files from the store. Deterministic: same store + same runs -> byte-identical files.

    python -m company_dataset.export --db data/store.db --runs fullrun,disc1 --out data/exports/final_YYYY-MM-DD
    python -m company_dataset.export --db data/store.db --run pilot1 --out data/exports/pilot1

--run exports one run; --runs builds the deliverable, merging several runs: a board decided in more
than one keeps its newest run's decision and evidence. A company is verified when its board is live,
its identity confirmed and company_name, description, ats_system and ats_code are all present;
LinkedIn, website and careers URLs are filled only where verified. Companies are clustered with
cluster_companies (strong shared keys + matching names), never on a name alone. Quality gates run
on the output: if one fails, only a report is written, beside the output directory.

Stored websites are re-checked with the current website rules using stored pages only (no
fetches); a website that fails is dropped with the fields found through it. The rules can only
remove a verification, never add one.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path

from company_dataset import store
from company_dataset.description_rules import acceptable
from company_dataset.enrich import REFERENCE_NAME_BASIS, website_supported
from company_dataset.enrichment import clean_description, clean_name, is_test_board
from company_dataset.identity import CONFIRMED, _fold, careers_branded_domain, cluster_companies, linkedin_compatible
from company_dataset.normalize import CORE_ATS, SMARTRECRUITERS, canonical_board_url, clean_website, host_of, normalize_linkedin
from company_dataset.sources import SOURCES
from company_dataset.validate import smartrecruiters_generic_page
from company_dataset.verification import LIVE

REQUIRED = ["company_name", "description", "linkedin_url", "website_url", "careersite_url", "ats_system", "ats_code"]
RUN_EXTRA = ["country", "verification_status", "verification_date", "verification_endpoint", "jobs_count", "identity_status", "identity_basis",
         "careersite_evidence", "website_method", "linkedin_method", "description_method", "sources", "company_id", "other_boards", "board_id"]
FIELD_FOR = {"name": "company_name", "description": "description", "linkedin_url": "linkedin_url", "website_url": "website_url",
             "careersite_url": "careersite_url", "ats_system": "ats_system", "ats_code": "ats_code", "country": "country"}


def board_rows(conn, run: str) -> list[dict]:
    """One row per board decided in this run, built only from stored evidence."""
    rows, websites = {}, {}
    q = ("SELECT i.board_id, i.status, i.name, i.basis, b.ats, b.code FROM identities i JOIN boards b ON b.id=i.board_id"
         " WHERE i.run=? ORDER BY i.board_id")
    for r in conn.execute(q, (run,)):
        rows[r["board_id"]] = {"board_id": r["board_id"], "identity_status": r["status"], "identity_basis": r["basis"] or "",
                               "ats_system": r["ats"], "ats_code": r["code"], **{c: "" for c in REQUIRED[:5]},
                               "country": "", "careersite_evidence": "", "website_method": "", "linkedin_method": "", "description_method": ""}
    # Newest evidence per (board, field); a 'candidate' careersite (page blocked) is reported, not counted as verified.
    for r in conn.execute("SELECT board_id, field, value, method, evidence_url, evidence_text, status FROM field_evidence WHERE run=? ORDER BY board_id, field, observed_at, id", (run,)):
        row = rows.get(r["board_id"])
        if row is None:
            continue
        col = FIELD_FOR.get(r["field"])
        if r["field"] == "careersite_url":
            row["careersite_evidence"] = r["status"] + ":" + r["method"]
            row["careersite_url"] = r["value"]
            row["_careersite_landed"] = r["evidence_url"] or ""
        elif col and r["status"] == "verified":
            row[col] = r["value"]
            if r["field"] in ("website_url", "linkedin_url", "description"):
                row[r["field"].split("_")[0] + "_method"] = r["method"]
            if r["field"] == "website_url":
                websites[r["board_id"]] = r
            if r["field"] == "country":
                row["_country_method"] = r["method"]
    latest = {}
    for r in conn.execute("SELECT * FROM verifications WHERE origin=? ORDER BY board_id, checked_at", (run,)):
        latest[r["board_id"]] = r
    for bid, row in rows.items():
        v = latest.get(bid)
        row["verification_status"] = v["outcome"] if v else ""
        row["verification_date"] = v["checked_at"] if v else ""
        row["verification_endpoint"] = v["endpoint"] if v else ""
        row["jobs_count"] = "" if not v or v["jobs_count"] is None else v["jobs_count"]
        if row["identity_status"] != CONFIRMED:
            row["company_name"] = ""
    srcs: dict[int, list[str]] = {}
    for r in conn.execute("SELECT DISTINCT sr.board_id, s.name FROM source_rows sr JOIN sources s ON s.id=sr.source_id WHERE sr.board_id IS NOT NULL ORDER BY sr.board_id, s.name"):
        if r[0] in rows:
            srcs.setdefault(r[0], []).append(r[1])
    names = {r["board_id"]: r["name"] for r in conn.execute("SELECT board_id, name FROM identities WHERE run=?", (run,))}
    for bid, row in rows.items():
        row["sources"] = ";".join(srcs.get(bid, []))
        if bid in websites:
            row["_website_rule"] = website_rule_failure(conn, websites[bid], names.get(bid))
            if row["_website_rule"]:
                drop_website(row)
    return [rows[k] for k in sorted(rows)]


def website_rule_failure(conn, w, name: str | None) -> str:
    """Why a stored website fails the current rules ('' if it passes)."""
    names = [name] if name else []
    if not clean_website(w["value"]):
        return "careers_platform_or_non_company_host"
    if careers_branded_domain(w["value"], names):
        return "careers_branded_domain"
    basis = w["method"].split("+")[0]
    if (w["evidence_text"] or "").startswith("links_to_board") and not website_supported(
            w["value"], store.page(conn, w["evidence_url"]), names, basis, w["evidence_url"]):
        return "board_link_only:domain_and_homepage_do_not_name_company"
    return ""


def drop_website(row: dict) -> None:
    """LinkedIn is only looked for on a verified website, homepage descriptions and domain-based
    countries come from it: all go with it. Vendor descriptions and job-location countries stay."""
    row["_dropped_website"] = row["website_url"]
    row["website_url"] = row["website_method"] = ""
    row["linkedin_url"] = row["linkedin_method"] = ""
    if row["description_method"].startswith("homepage_meta"):
        row["description"] = row["description_method"] = ""
    if row.get("_country_method", "").endswith("by_verified_domain"):
        row["country"] = ""
    # A careersite counted as verified because the board redirected to the dropped site's domain.
    landed = host_of(row.get("_careersite_landed", ""))
    if row["careersite_evidence"].startswith("verified") and landed and landed != host_of(row["careersite_url"]):
        row["careersite_evidence"] = "unverified:redirected_to_dropped_website"


def is_complete(row: dict) -> bool:
    return (row["identity_status"] == CONFIRMED and row["verification_status"] in LIVE
            and row["careersite_evidence"].startswith("verified") and all(row[c] for c in REQUIRED))


def run_export(conn, run: str, out: Path) -> dict:
    if out.exists():
        raise FileExistsError(out)
    rows = board_rows(conn, run)
    confirmed = [r for r in rows if r["identity_status"] == CONFIRMED and r["verification_status"] in LIVE]
    clusters = cluster_companies([{"name": r["company_name"], "website_url": r["website_url"], "website_verified": bool(r["website_url"]),
                                   "linkedin_url": r["linkedin_url"], "ats_system": r["ats_system"], "ats_code": r["ats_code"]} for r in confirmed])
    companies, links = [], []
    for g in clusters:
        members = sorted((confirmed[i] for i in g), key=lambda r: (not is_complete(r), -sum(bool(r[c]) for c in REQUIRED),
                                                                    -(int(r["jobs_count"] or 0)), r["ats_system"], r["ats_code"].casefold()))
        primary = dict(members[0])
        cid = f"c{primary['board_id']}"
        primary["company_id"] = cid
        primary["other_boards"] = ";".join(f"{m['ats_system']}:{m['ats_code']}" for m in members[1:])
        companies.append(primary)
        for m in members:
            links.append({"company_id": cid, "board_id": m["board_id"], "ats_system": m["ats_system"], "ats_code": m["ats_code"],
                          "role": "primary" if m is members[0] else "same_company"})
    companies.sort(key=lambda r: (r["company_name"].casefold(), r["ats_system"], r["ats_code"].casefold()))
    complete = [r for r in companies if is_complete(r)]
    unresolved = [r for r in rows if not (r["identity_status"] == CONFIRMED and r["verification_status"] in LIVE)]
    rule_changes = [{"board_id": r["board_id"], "ats_system": r["ats_system"], "ats_code": r["ats_code"], "identity_status": r["identity_status"],
                     "company_name": r["company_name"], "website_url": r["_dropped_website"], "reason": r["_website_rule"]}
                    for r in rows if r.get("_website_rule")]

    out.mkdir(parents=True)
    cols = REQUIRED + RUN_EXTRA
    _write(out / "companies_complete.csv", cols, complete)
    _write(out / "companies_all_confirmed.csv", cols + ["missing_fields"], [{**r, "missing_fields": ";".join(c for c in REQUIRED if not r[c]) or ("careersite_unverified" if not is_complete(r) else "")} for r in companies])
    _write(out / "boards.csv", cols, rows)
    _write(out / "company_boards.csv", ["company_id", "board_id", "ats_system", "ats_code", "role"], links)
    _write(out / "unresolved_boards.csv", cols, unresolved)
    _write(out / "website_rule_changes.csv", ["board_id", "ats_system", "ats_code", "identity_status", "company_name", "website_url", "reason"], rule_changes)
    counts = {
        "run": run,
        "candidate_boards_in_run": len(rows),
        "live_boards": sum(1 for r in rows if r["verification_status"] in LIVE),
        "identity_confirmed_live_boards": len(confirmed),
        "unique_companies": len(companies),
        "complete_companies": len(complete),
        "boards_by_outcome": dict(sorted(Counter(r["verification_status"] for r in rows).items())),
        "identity_by_status": dict(sorted(Counter(r["identity_status"] for r in rows).items())),
        "websites_dropped_by_current_rules": dict(sorted(Counter(r["reason"] for r in rule_changes).items())),
    }
    (out / "counts.json").write_text(json.dumps(counts, indent=1, sort_keys=True) + "\n")
    digests = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.iterdir())}
    (out / "SHA256SUMS").write_text("".join(f"{h}  {n}\n" for n, h in digests.items()))
    return counts


def _write(path: Path, cols: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------------------
# The deliverable
# ---------------------------------------------------------------------------

MANDATORY = ["company_name", "description", "ats_system", "ats_code"]
OPTIONAL = ["linkedin_url", "website_url", "careersite_url"]
VERIFIED_COLUMNS = MANDATORY + OPTIONAL
INCOMPLETE_COLUMNS = VERIFIED_COLUMNS + ["missing_fields", "other_boards", "verification_status", "verification_date", "jobs_count", "identity_basis"]
UNRESOLVED_COLUMNS = ["board_id", "ats_system", "ats_code", "region", "sources", "run", "identity_status", "identity_basis", "identity_conflicts",
                      "verification_status", "verification_date", "endpoint_public_jobs"]
EVIDENCE_COLUMNS = ["board_id", "ats_system", "ats_code", "field", "status", "value", "method", "evidence_url", "evidence_text", "observed_at", "run"]
FRESH_CHECK_MISSING = "fresh_check_not_passed"
SR_NAME_GUESS = "smartrecruiters_zero_jobs_name_match_only"
_URL_FIELDS = ("website_url", "careersite_url")
_PLACEHOLDER_RE = re.compile(r"\b(lorem ipsum|placeholder|coming soon|under construction|example company|your company|test company)\b", re.I)
_LEGAL_WORDS = frozenset("inc incorporated llc llp lp ltd limited corp corporation co company plc pbc gmbh ag ab as asa oy oyj bv nv sa sas sarl srl spa kg ug se pty pte the and".split())


class ExportGateError(Exception):
    """The output failed a quality gate: nothing was delivered."""


def _normalize_url(field: str, value: str) -> str:
    if field == "website_url":
        return clean_website(value) or ""
    if field == "linkedin_url":
        return normalize_linkedin(value) or ""
    return value


def _latest_identity_per_board(conn, runs: list[str], allowed=CORE_ATS) -> dict[int, dict]:
    """Newest identity decision per board of these ATS across the given runs."""
    marks = ",".join("?" * len(runs))
    ats = ",".join("?" * len(allowed))
    q = (f"SELECT i.*, b.ats, b.code, b.region FROM identities i JOIN boards b ON b.id=i.board_id JOIN"
         f" (SELECT board_id, MAX(decided_at || '|' || run) AS pick FROM identities WHERE run IN ({marks}) GROUP BY board_id) t"
         f" ON i.board_id=t.board_id AND (i.decided_at || '|' || i.run)=t.pick WHERE b.ats IN ({ats})")
    return {r["board_id"]: r for r in conn.execute(q, (*runs, *allowed))}


def _latest_field_evidence_per_board_field(conn, runs: list[str]) -> dict[tuple[int, str], dict]:
    """Newest verified field_evidence per (board, field) across runs."""
    marks = ",".join("?" * len(runs))
    q = (f"SELECT e.* FROM field_evidence e JOIN"
         f" (SELECT board_id, field, MAX(observed_at || '|' || id) AS pick FROM field_evidence WHERE run IN ({marks}) AND status='verified' GROUP BY board_id, field) t"
         f" ON e.board_id=t.board_id AND e.field=t.field AND (e.observed_at || '|' || e.id)=t.pick")
    return {(r["board_id"], r["field"]): r for r in conn.execute(q, runs)}


def _latest_checks(conn, runs: list[str]) -> dict[int, dict]:
    """A board's newest conclusive check in these runs, else its newest: a later 429 says nothing about it."""
    marks = ",".join("?" * len(runs))
    conclusive = ",".join("?" * len(store.CONCLUSIVE))
    rows = conn.execute(
        f"SELECT * FROM (SELECT v.*, ROW_NUMBER() OVER (PARTITION BY board_id ORDER BY CASE WHEN outcome IN ({conclusive}) THEN 1 ELSE 0 END DESC, checked_at DESC, id DESC) AS rn"
        f" FROM verifications v WHERE origin IN ({marks})) WHERE rn=1", (*store.CONCLUSIVE, *runs))
    return {r["board_id"]: r for r in rows}


def _rejected_urls(conn, runs: list[str]) -> dict[tuple[int, str, str], str]:
    """(board, field, value) -> when a URL field was last rejected: a later check found it dead."""
    marks = ",".join("?" * len(runs))
    fields = ",".join("?" * len(_URL_FIELDS))
    q = (f"SELECT board_id, field, value, MAX(observed_at) AS at_ FROM field_evidence WHERE status='rejected' AND run IN ({marks}) AND field IN ({fields})"
         " GROUP BY board_id, field, value")
    return {(r["board_id"], r["field"], r["value"]): r["at_"] for r in conn.execute(q, (*runs, *_URL_FIELDS))}


def _usable_description(text: str) -> bool:
    """The current rules, applied to stored text. Only removes: a value that passes is kept as recorded."""
    return bool(clean_description(text)) and not _PLACEHOLDER_RE.search(text)


def _dormant_name_guess(row: dict) -> bool:
    """SmartRecruiters serves a page for any signed-up identifier, jobs or not. With no jobs, a website
    found only by matching the name to a domain ties that page to nobody: "cdc" is not the CDC."""
    return (row["ats_system"] == SMARTRECRUITERS and not int(row["jobs_count"] or 0)
            and row["website_method"].startswith(REFERENCE_NAME_BASIS))


def decided_rows(conn, runs: list[str], allowed=CORE_ATS) -> list[dict]:
    """One merged row per board: newest identity run + per-field newest verified evidence across all runs."""
    identities = _latest_identity_per_board(conn, runs, allowed)
    evidence = _latest_field_evidence_per_board_field(conn, runs)
    checks = _latest_checks(conn, runs)
    rejected = _rejected_urls(conn, runs)
    sources = defaultdict(list)
    for r in conn.execute("SELECT DISTINCT sr.board_id, s.name FROM source_rows sr JOIN sources s ON s.id=sr.source_id WHERE sr.board_id IS NOT NULL ORDER BY sr.board_id, s.name"):
        sources[r[0]].append(r[1])

    out = []
    for bid in sorted(identities):
        ident = identities[bid]
        row = {"board_id": bid, "identity_status": ident["status"], "identity_basis": ident["basis"] or "", "identity_conflicts": ident["conflicts"] or "",
               "run": ident["run"], "region": ident["region"] or "", "ats_system": ident["ats"], "ats_code": ident["code"],
               "company_name": "", "description": "", "linkedin_url": "", "website_url": "", "careersite_url": "", "careersite_evidence": "", "country": "",
               "website_method": "", "linkedin_method": "", "description_method": "", "verification_status": "", "verification_date": "",
               "verification_endpoint": "", "verification_origin": "", "jobs_count": "", "sources": ";".join(sources.get(bid, []))}
        v = checks.get(bid)
        if v:
            row.update(verification_status=v["outcome"], verification_date=v["checked_at"], verification_endpoint=v["endpoint"] or "",
                       verification_origin=v["origin"], jobs_count="" if v["jobs_count"] is None else v["jobs_count"])
        website = None
        if row["identity_status"] == CONFIRMED:
            for field, col in FIELD_FOR.items():
                ev = evidence.get((bid, field))
                if not ev:
                    continue
                dead_since = rejected.get((bid, field, ev["value"]))
                if dead_since and dead_since >= ev["observed_at"]:
                    continue
                row[col] = ev["value"]
                if field in ("website_url", "linkedin_url", "description"):
                    row[field.split("_")[0] + "_method"] = ev["method"]
                if field == "careersite_url":
                    row["careersite_evidence"] = f"{ev['status']}:{ev['method']}"
                    row["_careersite_landed"] = ev["evidence_url"] or ""
                if field == "country":
                    row["_country_method"] = ev["method"]
                if field == "website_url":
                    website = ev
        if _dormant_name_guess(row):
            row.update(identity_status="identity_unresolved", identity_basis=SR_NAME_GUESS, company_name="", description="", linkedin_url="", website_url="", careersite_url="",
                       website_method="", linkedin_method="", description_method="")
            website = None
        if website and website_rule_failure(conn, website, ident["name"] or None):
            drop_website(row)
        row["website_url"] = _normalize_url("website_url", row["website_url"])
        row["linkedin_url"] = _normalize_url("linkedin_url", row["linkedin_url"])
        if row["description"] and not (_usable_description(row["description"]) and acceptable(row["description_method"], row["description"], row["company_name"])):
            row["description"] = row["description_method"] = ""
        if row["company_name"] and (not clean_name(row["company_name"], row["ats_code"]) or _PLACEHOLDER_RE.search(row["company_name"])):
            row["company_name"] = ""
        out.append(row)
    return out


def _handle_names_company(handle: str, name: str) -> bool:
    """Every word of the name, legal forms aside, appears in the handle: "Xebia UK" / xebia-uk, not "Xebia UK" / xebia."""
    words = [w for w in re.findall(r"[a-z0-9]+", _fold(name).lower()) if w not in _LEGAL_WORDS]
    flat = re.sub(r"[^a-z0-9]", "", _fold(handle).lower())
    return bool(words) and all(w in flat for w in words)


def _parent_of(rows: list[dict], handle: str) -> dict | None:
    """The row whose name's words all appear in every other row's name, when the handle fits it: "VGW" among "VGW Australia", "VGW USA"."""
    def words(r):
        return {w for w in re.findall(r"[a-z0-9]+", _fold(r["company_name"]).lower()) if w not in _LEGAL_WORDS}

    shortest = min(rows, key=lambda r: len(words(r)))
    fits = handle.isdigit() or linkedin_compatible(handle, shortest["company_name"], None)
    return shortest if fits and all(words(shortest) < words(r) for r in rows if r is not shortest) else None


def apply_linkedin_policy(companies: list[dict]) -> int:
    """A handle shared across company rows belongs to the parent: it stays only where it names
    the row's company. A row's own website is not considered, since subsidiaries share it. Returns rows cleared."""
    groups = defaultdict(list)
    for r in companies:
        if r["linkedin_url"]:
            groups[r["linkedin_url"].lower()].append(r)
    cleared = 0
    for url, rows in groups.items():
        if len(rows) < 2:
            continue
        handle = url.rsplit("/", 1)[-1]
        parent = _parent_of(rows, handle)
        for r in rows:
            if r is not parent and not _handle_names_company(handle, r["company_name"]):
                r["linkedin_url"] = r["linkedin_method"] = ""
                cleared += 1
    return cleared


def _missing(row: dict, fresh_origin: str | None) -> list[str]:
    missing = [c for c in MANDATORY if not (row[c] or "").strip()]
    if not missing and fresh_origin and row["verification_origin"] != fresh_origin:
        missing.append(FRESH_CHECK_MISSING)
    return missing


def _evidence_index(conn, runs: list[str]) -> dict[tuple[int, str], set]:
    marks = ",".join("?" * len(runs))
    index: dict[tuple[int, str], set] = defaultdict(set)
    for r in conn.execute(f"SELECT board_id, field, value FROM field_evidence WHERE status='verified' AND run IN ({marks})", runs):
        index[(r["board_id"], r["field"])].add(_normalize_url(r["field"], r["value"]))
    return index


def quality_gates(verified: list[dict], evidence: dict, fresh_origin: str | None, allowed=CORE_ATS) -> dict:
    """Everything a delivered row must satisfy, checked on the output rather than assumed from how it was built."""
    failures: dict[str, list[str]] = defaultdict(list)
    seen = set()
    fields = {"name": "company_name", "description": "description", "ats_system": "ats_system", "ats_code": "ats_code",
              "linkedin_url": "linkedin_url", "website_url": "website_url", "careersite_url": "careersite_url"}
    for r in verified:
        ref = f"{r['ats_system']}:{r['ats_code']}"
        if any(not (r[c] or "").strip() for c in MANDATORY):
            failures["mandatory_fields"].append(ref)
        if r["ats_system"] not in allowed:
            failures["ats_scope"].append(ref)
        if r["identity_status"] != CONFIRMED or r["verification_status"] not in LIVE:
            failures["confirmed_and_live"].append(ref)
        if fresh_origin and r["verification_origin"] != fresh_origin:
            failures["fresh_check"].append(ref)
        for field, col in fields.items():
            if r[col] and r[col] not in evidence.get((r["board_id"], field), ()):
                failures["evidence"].append(f"{ref}:{field}")
        if r["website_url"] and clean_website(r["website_url"]) != r["website_url"]:
            failures["url_format"].append(f"{ref}:website_url={r['website_url']}")
        if r["linkedin_url"] and normalize_linkedin(r["linkedin_url"]) != r["linkedin_url"]:
            failures["url_format"].append(f"{ref}:linkedin_url={r['linkedin_url']}")
        if r["careersite_url"] and (not r["careersite_url"].startswith(("https://", "http://")) or smartrecruiters_generic_page(r["careersite_url"])):
            failures["url_format"].append(f"{ref}:careersite_url={r['careersite_url']}")
        key = (r["ats_system"], r["ats_code"].casefold())
        if key in seen:
            failures["unique_boards"].append(ref)
        seen.add(key)
        if is_test_board(r["company_name"], "") or _PLACEHOLDER_RE.search(r["company_name"]) or _PLACEHOLDER_RE.search(r["description"]):
            failures["placeholder"].append(ref)
    return {"passed": not failures, "failure_counts": {k: len(v) for k, v in sorted(failures.items())},
            "failures": {k: sorted(v)[:25] for k, v in sorted(failures.items())}}


def _source_summaries(conn, verified: list[dict], incomplete: list[dict]) -> list[dict]:
    attribution = {m.META.name: m.META for m in SOURCES}
    counts = Counter()
    only = Counter()
    for r in verified:
        names = [s for s in r["sources"].split(";") if s]
        counts.update(names)
        if len(names) == 1:
            only[names[0]] += 1
    out = []
    for s in conn.execute("SELECT id, name, url, license, rows_read, imported_at FROM sources ORDER BY name").fetchall():
        rows = conn.execute("SELECT COUNT(*), COUNT(board_id), COUNT(DISTINCT board_id) FROM source_rows WHERE source_id=?", (s["id"],)).fetchone()
        meta = attribution.get(s["name"])
        out.append({"source": s["name"], "url": s["url"] or "", "license": s["license"] or "", "attribution": meta.attribution if meta else "",
                    "rows_read": s["rows_read"], "rejected_rows": rows[0] - rows[1], "boards": rows[2], "imported_at": s["imported_at"],
                    "companies_verified": counts[s["name"]], "companies_verified_only_from_this_source": only[s["name"]]})
    return out


def _evidence_rows(conn, runs: list[str], delivered: list[dict]) -> list[dict]:
    marks = ",".join("?" * len(runs))
    ats = ",".join("?" * len(CORE_ATS))
    q = (f"SELECT e.board_id, b.ats AS ats_system, b.code AS ats_code, e.field, e.status, e.value, e.method, e.evidence_url, e.evidence_text, e.observed_at, e.run"
         f" FROM field_evidence e JOIN boards b ON b.id=e.board_id WHERE e.run IN ({marks}) AND b.ats IN ({ats})")
    rows = [dict(r) for r in conn.execute(q, (*runs, *CORE_ATS))]
    for r in delivered:
        if r["jobs_count"] != "":
            rows.append({"board_id": r["board_id"], "ats_system": r["ats_system"], "ats_code": r["ats_code"], "field": "jobs_count", "status": "verified", "value": str(r["jobs_count"]),
                         "method": "provider_api", "evidence_url": r["verification_endpoint"], "evidence_text": "", "observed_at": r["verification_date"], "run": r["verification_origin"]})
    rows.sort(key=lambda r: (r["board_id"], r["field"], r["observed_at"] or "", r["run"] or "", r["method"] or "", r["value"] or ""))
    return rows


def _companies(rows: list[dict], fresh_origin: str | None) -> tuple[list[dict], int, int]:
    """(one row per company with missing_fields set, companies merged from several boards, LinkedIn handles cleared)."""
    confirmed = [r for r in rows if r["identity_status"] == CONFIRMED and r["verification_status"] in LIVE]
    clusters = cluster_companies([{"name": r["company_name"], "website_url": r["website_url"], "website_verified": bool(r["website_url"]),
                                   "linkedin_url": r["linkedin_url"], "ats_system": r["ats_system"], "ats_code": r["ats_code"]} for r in confirmed])
    companies, merged = [], 0
    for g in clusters:
        members = sorted((confirmed[i] for i in g), key=lambda r: (bool(_missing(r, fresh_origin)), -sum(bool(r[c]) for c in VERIFIED_COLUMNS),
                                                                   -(int(r["jobs_count"] or 0)), r["ats_system"], r["ats_code"].casefold()))
        primary = dict(members[0])
        primary["other_boards"] = ";".join(f"{m['ats_system']}:{m['ats_code']}" for m in members[1:])
        companies.append(primary)
        merged += len(members) > 1
    companies.sort(key=lambda r: (r["company_name"].casefold(), r["ats_system"], r["ats_code"].casefold()))
    linkedin_cleared = apply_linkedin_policy(companies)
    for r in companies:
        r["missing_fields"] = ";".join(_missing(r, fresh_origin))
    return companies, merged, linkedin_cleared


def build_export(conn, runs: list[str], out_dir: Path, fresh_origin: str | None = None, extra_ats: tuple = ()) -> dict:
    """fresh_origin: the run whose provider re-check every verified row must have passed. Without it the
    report says so. extra_ats: ATS outside the target five, each delivered only in its own <ats>_companies.csv."""
    out_dir = Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise FileExistsError(out_dir)
    rows = decided_rows(conn, runs)
    confirmed = [r for r in rows if r["identity_status"] == CONFIRMED and r["verification_status"] in LIVE]
    companies, merged, linkedin_cleared = _companies(rows, fresh_origin)
    verified = [r for r in companies if not r["missing_fields"]]
    incomplete = [r for r in companies if r["missing_fields"]]
    unresolved = [{**r, "endpoint_public_jobs": canonical_board_url(r["ats_system"], r["ats_code"], r["region"])} for r in rows
                  if r["identity_status"] in ("identity_unresolved", "identity_mismatch") and r["verification_status"] in LIVE]
    evidence_index = _evidence_index(conn, runs)
    gates = quality_gates(verified, evidence_index, fresh_origin)
    extra: dict[str, list[dict]] = {}
    extra_report: dict[str, dict] = {}
    for ats in extra_ats:
        extra_rows = decided_rows(conn, runs, allowed=(ats,))
        extra_companies, extra_merged, _ = _companies(extra_rows, fresh_origin)
        extra[ats] = [r for r in extra_companies if not r["missing_fields"]]
        extra_gates = quality_gates(extra[ats], evidence_index, fresh_origin, allowed=(ats,))
        extra_report[ats] = {"boards_decided": len(extra_rows), "live_boards": sum(1 for r in extra_rows if r["verification_status"] in LIVE),
                             "companies_verified": len(extra[ats]), "companies_incomplete": len(extra_companies) - len(extra[ats]), "merged_companies": extra_merged,
                             "unresolved_live_boards": sum(1 for r in extra_rows if r["identity_status"] in ("identity_unresolved", "identity_mismatch") and r["verification_status"] in LIVE),
                             "optional_fields_in_verified": {c: sum(1 for r in extra[ats] if r[c]) for c in OPTIONAL}, "gates": extra_gates}
        if not extra_gates["passed"]:
            gates = {"passed": False, "failure_counts": {**gates.get("failure_counts", {}), **{f"{ats}:{k}": v for k, v in extra_gates["failure_counts"].items()}},
                     "failures": {**gates.get("failures", {}), **{f"{ats}:{k}": v for k, v in extra_gates["failures"].items()}}}
    descriptions = Counter(r["description"].strip().casefold() for r in verified)
    report = {
        "runs": runs, "fresh_origin": fresh_origin, "gates": gates,
        "fresh_checks": {"run": bool(fresh_origin), "note": "" if fresh_origin else "Not run for this export: rows carry the date of their last provider check, listed here. The zero-dead-endpoints gate was not applied.",
                         "last_checked_by_date": dict(sorted(Counter((r["verification_date"] or "")[:10] for r in verified).items()))},
        "extra_ats": extra_report,
        "boards_decided": len(rows),
        "live_boards": sum(1 for r in rows if r["verification_status"] in LIVE),
        "identity_confirmed_live_boards": len(confirmed),
        "unique_companies": len(companies), "companies_verified": len(verified), "companies_incomplete": len(incomplete),
        "unresolved_live_boards": len(unresolved), "merged_companies": merged, "linkedin_handles_cleared_from_subsidiaries": linkedin_cleared,
        "optional_fields_in_verified": {c: sum(1 for r in verified if r[c]) for c in OPTIONAL},
        "missing_fields_in_incomplete": dict(sorted(Counter(m for r in incomplete for m in r["missing_fields"].split(";")).items())),
        "zero_job_companies_in_verified": sum(1 for r in verified if str(r["jobs_count"]) == "0"),
        "repeated_descriptions_for_review": [{"description": d[:200], "companies": n} for d, n in sorted(descriptions.items(), key=lambda x: (-x[1], x[0])) if n > 1][:100],
        "boards_by_outcome": dict(sorted(Counter(r["verification_status"] for r in rows).items())),
        "identity_by_status": dict(sorted(Counter(r["identity_status"] for r in rows).items())),
        "by_ats": {ats: {"boards": sum(1 for r in rows if r["ats_system"] == ats),
                         "live": sum(1 for r in rows if r["ats_system"] == ats and r["verification_status"] in LIVE),
                         "unique_companies": sum(1 for r in companies if r["ats_system"] == ats),
                         "companies_verified": sum(1 for r in verified if r["ats_system"] == ats),
                         "companies_incomplete": sum(1 for r in incomplete if r["ats_system"] == ats)} for ats in sorted(CORE_ATS)},
    }
    if not gates["passed"]:
        failed = out_dir.with_name(out_dir.name + ".failed")
        shutil.rmtree(failed, ignore_errors=True)
        failed.mkdir(parents=True)
        (failed / "qa_report.json").write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
        raise ExportGateError(f"quality gates failed, see {failed / 'qa_report.json'}: {gates['failure_counts']}")
    out_dir.mkdir(parents=True, exist_ok=True)
    _write(out_dir / "companies_verified.csv", VERIFIED_COLUMNS, verified)
    _write(out_dir / "companies_incomplete.csv", INCOMPLETE_COLUMNS, incomplete)
    _write(out_dir / "unresolved_boards.csv", UNRESOLVED_COLUMNS, unresolved)
    for ats, ats_rows in extra.items():
        _write(out_dir / f"{ats}_companies.csv", VERIFIED_COLUMNS, ats_rows)
    _write(out_dir / "evidence.csv", EVIDENCE_COLUMNS, _evidence_rows(conn, runs, companies))
    (out_dir / "sources.json").write_text(json.dumps(_source_summaries(conn, verified, incomplete), indent=1, sort_keys=True) + "\n")
    (out_dir / "qa_report.json").write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
    digests = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out_dir.iterdir())}
    (out_dir / "SHA256SUMS").write_text("".join(f"{h}  {n}\n" for n, h in digests.items()))
    return report


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="company_dataset.export")
    p.add_argument("--db", required=True)
    which = p.add_mutually_exclusive_group(required=True)
    which.add_argument("--run", help="export one run")
    which.add_argument("--runs", help="comma-separated run labels to merge")
    p.add_argument("--out", required=True)
    p.add_argument("--fresh-origin", help="--runs: the run whose provider re-check every verified row must have passed")
    p.add_argument("--extra-ats", default="", help="--runs: comma-separated ATS delivered only in their own <ats>_companies.csv")
    a = p.parse_args(argv)
    conn = store.connect(a.db)
    counts = run_export(conn, a.run, Path(a.out)) if a.run else build_export(conn, [r for r in a.runs.split(",") if r], Path(a.out), a.fresh_origin, tuple(x for x in a.extra_ats.split(",") if x))
    print(json.dumps(counts, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
