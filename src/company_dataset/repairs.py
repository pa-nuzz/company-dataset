"""One-off corrections to decisions already in the store, each recorded under its own run label.

Nothing is rewritten: new verification, identity and evidence rows are appended, and an export
picks a run up by naming it. Each step commits as one transaction.

    python -m company_dataset.repairs sr-existence --db data/store.db --run sr_existence_2026-11-03
    python -m company_dataset.repairs reresolve --db data/store.db --run identity_fix_2026-11-03 --from-runs fullrun,disc1,sr_fix_test
    python -m company_dataset.repairs reverify-recovery --db data/store.db --run recovery_reverify_2026-11-03 --from-run recovery1
    python -m company_dataset.repairs descriptions-cached --db data/store.db --run desc_cached_2026-11-03 --from-runs fullrun,...
    python -m company_dataset.repairs descriptions-network --db data/store.db --run desc_network_2026-11-03 --from-runs fullrun,...
    python -m company_dataset.repairs promote-vendor --db data/store.db --run vendor_promote_2026-11-03 --from-runs fullrun,...
    python -m company_dataset.repairs promote-job-board-orgs --db data/store.db --run org_promote_2026-11-03 --from-runs fullrun,...
    python -m company_dataset.repairs final-checks --db data/store.db --run final_qa_2026-11-03 --from-runs fullrun,...
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from company_dataset import store
from company_dataset.enrichment import (
    board_from_validation, clean_description, clean_name, english_variant_urls, fetch_page, is_auxiliary_board, is_test_board, is_english, landing_site, linkedin_url,
)
from company_dataset.http import HttpClient
from company_dataset.config import FAST_PER_HOST_INTERVAL, FAST_RATE_LIMITS, FAST_WORKERS, resolve_rate_limits
from company_dataset.enrich import NON_MAIN_LABELS, PARKED_RE, board_ownership, homepage_self_naming, official_landing, verify_homepage, website_supported
from company_dataset.export import _latest_checks, _latest_field_evidence_per_board_field, _latest_identity_per_board, _PLACEHOLDER_RE, decided_rows
from company_dataset.identity import (
    CONFIRMED, UNRESOLVED, VENDOR_API, VENDOR_PAGE, _code_shaped, careers_branded_domain, domain_compatible, linkedin_compatible, name_claims, registrable_domain, resolve_identity,
)
from company_dataset.ingest import load_boards
from company_dataset.models import Candidate, utc_now
from company_dataset.normalize import ASHBY, CORE_ATS, LEVER, SMARTRECRUITERS, canonical_board_url, clean_website, host_of, normalize_linkedin, validation_endpoint
from company_dataset.validate import SR_NO_SUCH_COMPANY, AtsRateLimiter, BoardValidator, smartrecruiters_generic_page
from company_dataset.verification import INVALID_IDENTIFIER, LIVE, VERIFIED_LIVE_ZERO_JOBS, classify

# Identity bases whose stored name is the vendor's own: the hosted page it came from is not stored.
_VENDOR_BASES = ("vendor_only", "vendor_not_corroborated", "vendor")
_FINAL_PAGE_STATUSES = ("ok", "http_404", "http_410")
ATS_REGISTRABLE_DOMAINS = frozenset(("greenhouse.io", "lever.co", "ashbyhq.com", "smartrecruiters.com", "breezy.hr", "workable.com"))


def backfill_smartrecruiters_existence(conn, run: str, now: str) -> dict:
    """Boards counted live on a totalFound=0 answer whose stored careers page fell back to generic search."""
    latest = store.latest_verifications(conn)
    invalidated = 0
    with conn:
        for r in conn.execute("SELECT id, code FROM boards WHERE ats=?", (SMARTRECRUITERS,)).fetchall():
            v = latest.get(r["id"])
            if not v or v["outcome"] != VERIFIED_LIVE_ZERO_JOBS:
                continue
            url = canonical_board_url(SMARTRECRUITERS, r["code"])
            page = store.page(conn, url)
            if not page or not smartrecruiters_generic_page(page.get("final_url") or ""):
                continue
            conn.execute("INSERT INTO verifications(board_id, checked_at, origin, endpoint, http_status, outcome, legacy_status, error, jobs_count)"
                         " VALUES (?,?,?,?,?,?,?,?,?)", (r["id"], now, run, url, 200, INVALID_IDENTIFIER, "invalid", SR_NO_SUCH_COMPANY, None))
            conn.execute("INSERT OR REPLACE INTO identities(board_id, run, status, name, basis, conflicts, decided_at) VALUES (?,?,?,?,?,?,?)",
                         (r["id"], run, "not_live", None, SR_NO_SUCH_COMPANY, "", now))
            invalidated += 1
    return {"invalidated": invalidated}


def _validation(v) -> dict:
    return {"status": v["legacy_status"], "http_status": v["http_status"], "error": v["error"], "jobs_count": v["jobs_count"],
            "code_validated": v["code_validated"], "region_answered": v["region_answered"], "company_name": v["provider_name"],
            "company_identifier": v["provider_identifier"], "website_url": v["provider_website"], "description": v["provider_description"],
            "country": v["provider_country"], "checked_at": v["checked_at"]}


def _result(conn, b: dict, v, stored_vendor_name: str | None) -> dict:
    """The shape name_claims reads, rebuilt from the store. Raw HTML is gone, so a hosted-page
    name survives only as the name its earlier decision recorded."""
    validation = _validation(v)
    board = board_from_validation(Candidate(ats=b["ats"], codes=b["codes"] or [b["code"]], region=b["region"]), validation)
    jobad_about = [(row["value"], row["method"]) for row in conn.execute(
        """SELECT value, method FROM field_evidence
           WHERE board_id=? AND field='description' AND status='verified' AND value != ''
           AND method IN ('gh_content_about','lever_jobad_about','ashby_jobad_about','breezy_jobad_about','sr_jobad_about')""",
        (b["id"],)).fetchall()]
    return {"board": b, "validation": validation, "careersite_url": board.careersite_url, "board_page": store.page(conn, board.careersite_url),
            "board_extract": {"names": [(stored_vendor_name, "stored_identity_name")]} if stored_vendor_name else None,
            "jobad_about": jobad_about}


def _claims_text(claims) -> str:
    return "; ".join(f"{c.kind}:{c.origin}={c.name}" for c in claims)[:500]


def _record_identity(conn, run, board_id, ident, now):
    conn.execute("INSERT OR REPLACE INTO identities(board_id, run, status, name, basis, conflicts, decided_at) VALUES (?,?,?,?,?,?,?)",
                 (board_id, run, ident.status, ident.name, "+".join(ident.basis), " | ".join(ident.conflicts), now))


def _record_evidence(conn, run, board_id, field, value, method, url, text, now):
    conn.execute("INSERT OR IGNORE INTO field_evidence(board_id, field, value, method, evidence_url, evidence_text, status, observed_at, run)"
                 " VALUES (?,?,?,?,?,?,'verified',?,?)", (board_id, field, value, method, url, (text or "")[:500], now, run))


def reresolve_identities(conn, run: str, from_runs: list[str], now: str) -> dict:
    """Decide open boards again with the current rules. Only a move to confirmed is recorded, so
    confirmed boards and everything that stays open keep the decision they already have."""
    latest = store.latest_verifications(conn)
    marks = ",".join("?" * len(from_runs))
    open_boards = {r["board_id"]: r for r in conn.execute(
        f"SELECT i.* FROM identities i JOIN (SELECT board_id, MAX(decided_at || '|' || run) AS pick FROM identities WHERE run IN ({marks}) GROUP BY board_id) t"
        " ON i.board_id=t.board_id AND (i.decided_at || '|' || i.run)=t.pick WHERE i.status IN ('identity_unresolved', 'identity_mismatch')", from_runs)}
    stats = Counter()
    by_ats = Counter()
    with conn:
        for b in load_boards(conn):
            prior = open_boards.get(b["id"])
            v = latest.get(b["id"])
            if not prior or b["ats"] not in CORE_ATS or not v or v["outcome"] not in LIVE:
                continue
            stats["considered"] += 1
            res = _result(conn, b, v, prior["name"] if prior["basis"] in _VENDOR_BASES else None)
            claims = name_claims(res, homepage=None)
            ident = resolve_identity(claims, b["code"])
            if ident.status != CONFIRMED:
                continue
            _record_identity(conn, run, b["id"], ident, now)
            _record_evidence(conn, run, b["id"], "name", ident.name, "identity:" + "+".join(ident.basis), v["endpoint"], _claims_text(claims), now)
            stats["confirmed"] += 1
            by_ats[b["ats"]] += 1
    return {"considered": stats["considered"], "confirmed": stats["confirmed"], "by_ats": dict(sorted(by_ats.items()))}


def _slug_agrees(site: str, code: str) -> bool:
    label = re.sub(r"[^a-z0-9]", "", registrable_domain(site).split(".")[0])
    slug = re.sub(r"[^a-z0-9]", "", code.lower())
    return bool(label) and (label == slug or (len(label) >= 4 and slug.startswith(label)))


def _site_failure(site: str, page: dict | None, b: dict, names: list[str], method: str) -> tuple[str, str | None, str, bool]:
    """(reason it fails or '', landed site, evidence text, landed on a careers host)."""
    evidence = verify_homepage(page, b, names)
    if not evidence:
        return "homepage_does_not_name_company", None, "", False
    landed, on_careers = official_landing(site, landing_site(site, page))
    if not landed or careers_branded_domain(landed, names):
        return "careers_or_non_company_host", None, "", False
    if evidence.startswith("links_to_board"):
        support = website_supported(landed, page, names, method, site)
        if not support:
            return "board_link_only", None, "", False
        return "", landed, f"{evidence}; {support}", on_careers
    # A name join can land on a namesake: a site calling itself "Apollo" proves nothing about a
    # board named Apollo unless its domain or the board's slug also point at it.
    if method != "provider_website" and not any(domain_compatible(landed, n) for n in names) and not _slug_agrees(landed, b["code"]):
        return "possible_namesake", None, "", False
    return "", landed, evidence, on_careers


def reverify_recovery(conn, run: str, from_run: str, client: HttpClient, now: str, workers: int = 12, refetch: bool = False) -> dict:
    """Hold another run's reference-sourced websites to the homepage rules every other row meets.
    Boards that pass are recorded under `run`, with values re-derived from the fetched homepage.
    refetch: ignore stored pages, for when the summaries were written by an older parser."""
    rows = conn.execute(
        "SELECT i.board_id, i.name AS identity_name, e.value AS website, e.method FROM identities i JOIN field_evidence e"
        " ON e.board_id=i.board_id AND e.run=i.run AND e.field='website_url' AND e.status='verified'"
        " WHERE i.run=? AND i.status=? ORDER BY i.board_id", (from_run, CONFIRMED)).fetchall()
    sites = {r["board_id"]: clean_website(r["website"]) for r in rows}
    stale = sorted({s for s in sites.values() if s and (refetch or (store.page(conn, s) or {}).get("status") not in _FINAL_PAGE_STATUSES)})
    with ThreadPoolExecutor(max_workers=workers) as ex:
        fetched = dict(zip(stale, ex.map(lambda u: fetch_page(client, u), stale)))
    boards = {b["id"]: b for b in load_boards(conn)}
    latest = store.latest_verifications(conn)
    stats = Counter()
    dropped = Counter()
    with conn:
        for url, summary in fetched.items():
            store.put_page(conn, url, summary)
        for r in rows:
            b, v, site = boards[r["board_id"]], latest.get(r["board_id"]), sites[r["board_id"]]
            if not v or v["outcome"] not in LIVE or not site:
                dropped["not_live_or_no_site"] += 1
                continue
            res = _result(conn, b, v, None)
            names = list(dict.fromkeys(n for n in (v["provider_name"], r["identity_name"]) if n))
            page = store.page(conn, site)
            reason, landed, evidence, on_careers = _site_failure(site, page, b, names, r["method"])
            if reason:
                dropped[reason] += 1
                continue
            owned = board_ownership(evidence, "board_link", page)
            claims = name_claims(res, page, homepage_independent=True)
            ident = resolve_identity(claims, b["code"], board_ownership=owned)
            if ident.status != CONFIRMED:
                dropped["identity_not_confirmed"] += 1
                continue
            _record_identity(conn, run, b["id"], ident, now)
            _record_evidence(conn, run, b["id"], "name", ident.name, "identity:" + "+".join(ident.basis), v["endpoint"], _claims_text(claims), now)
            _record_evidence(conn, run, b["id"], "website_url", landed, f"{r['method']}+homepage_names_company", site, evidence, now)
            _carry_description(conn, run, b["id"], page, on_careers, v, now, site)
            _carry_linkedin(conn, run, b["id"], from_run, ident.name, landed, page, now, site)
            stats["kept"] += 1
    return {"considered": len(rows), "fetched": len(fetched), "kept": stats["kept"], "dropped": dict(sorted(dropped.items()))}


def _carry_description(conn, run, board_id, page, on_careers, v, now, site):
    candidates = [] if on_careers else [(page.get("description"), "homepage_meta", site)]
    candidates.append((v["provider_description"], "provider_description", v["endpoint"]))
    for text, method, url in candidates:
        d = clean_description(text)
        if d:
            _record_evidence(conn, run, board_id, "description", d, method, url, d[:200], now)
            return


def _carry_linkedin(conn, run, board_id, from_run, name, website, page, now, site):
    earlier = [r["value"] for r in conn.execute("SELECT value FROM field_evidence WHERE board_id=? AND run=? AND field='linkedin_url' AND status='verified'", (board_id, from_run))]
    options = [(linkedin_url(s), "homepage_link") for s in page.get("linkedin_slugs") or []] + [(u, "earlier_run_reference") for u in earlier]
    for url, method in options:
        clean = normalize_linkedin(url)
        if clean and linkedin_compatible(clean.rsplit("/", 1)[-1], name, website):
            _record_evidence(conn, run, board_id, "linkedin_url", clean, method, site, "", now)
            return


# ---------------------------------------------------------------------------
# Vendor-name promotion (Option 1) and job-board org extraction (Option 3)
#
# A live board is delivered when its vendor-recorded name and four mandatory
# fields are present, with no external URLs required. These steps find the
# vendor's own name for boards whose latest decision is unresolved.
# ---------------------------------------------------------------------------

# SmartRecruiters' title tag, Greenhouse's board name, the Breezy account header
# and Lever hosted-page titles are vendor self-reports. Job-board app payloads
# name the organization the vendor serves the job ads for.
_PROMO_VENDOR_BASES = _VENDOR_BASES
_PROMO_JUNK_RE = re.compile(r"\b(test|testing|demo|sandbox|staging|sample|specimen|example)\b", re.I)
_DEFAULT_SAMPLE_SIZE = 100


def _vendor_promotion_candidates(conn, from_runs: list[str]) -> dict[int, dict]:
    """Unresolved live boards with the newest vendor-asserted name available: the
    stored identity name when that decision itself rested on the vendor's record,
    else the company name the newest live provider check returned."""
    idents = _latest_identity_per_board(conn, from_runs, CORE_ATS)
    checks = _latest_checks(conn, from_runs)
    out: dict[int, dict] = {}
    for bid in sorted(idents):
        ident = idents[bid]
        if ident["status"] != UNRESOLVED:
            continue
        v = checks.get(bid)
        if not v or v["outcome"] not in LIVE:
            continue
        name, detail = None, None
        if (ident["basis"] or "") in _PROMO_VENDOR_BASES and ident["name"]:
            name, detail = ident["name"], f"identity:{ident['basis']}"
        elif v["provider_name"]:
            name, detail = v["provider_name"], "provider_name"
        out[bid] = {"name": name, "detail": detail, "ident": ident, "check": v}
    return out


def _promo_name_failure(raw_name, code: str, *, page_derived: bool) -> str:
    """Why a candidate name cannot be promoted ('' when it can)."""
    n = clean_name(raw_name, code)
    if not n:
        return "not_a_name"
    if _PROMO_JUNK_RE.search(n) or is_test_board(n, code):
        return "test_board"
    if is_auxiliary_board(n):
        return "auxiliary_board"
    if _PLACEHOLDER_RE.search(n):
        return "placeholder_name"
    # A hosted page that says no more than its slug names nothing ("samsproduct" for samsproduct).
    if page_derived and _code_shaped(n, re.sub(r"[^a-z0-9]", "", (code or "").lower())):
        return "not_a_name"
    return ""


def _record_promotion(conn, run: str, bid: int, name: str, detail: str, v, now: str) -> None:
    conn.execute("INSERT OR REPLACE INTO identities(board_id, run, status, name, basis, conflicts, decided_at) VALUES (?,?,?,?,?,'',?)",
                 (bid, run, CONFIRMED, name, f"vendor_promoted:{detail}", now))
    conn.execute("INSERT OR IGNORE INTO field_evidence(board_id, field, value, method, evidence_url, evidence_text, status, observed_at, run)"
                 " VALUES (?,'name',?,?,?,?, 'verified',?,?)", (bid, name, f"vendor_promoted:{detail}", v["endpoint"], name[:200], now, run))


def promote_vendor_identities(conn, run: str, from_runs: list[str], now: str, sample_out: str | None = None, sample_size: int = _DEFAULT_SAMPLE_SIZE) -> dict:
    """Option 1: unresolved live boards whose vendor record names them get a confirmed
    identity from that name. Boards a third-party list contradicts (identity_mismatch)
    are left standing, as are boards not live. Returns counts and an optional QA sample."""
    candidates = _vendor_promotion_candidates(conn, from_runs)
    stats, promoted = Counter(), []
    by_ats = Counter()
    by_source = Counter()
    with conn:
        for bid, c in candidates.items():
            if not c["name"]:
                stats["no_vendor_name"] += 1
                continue
            reason = _promo_name_failure(c["name"], c["ident"]["code"], page_derived=False)
            if reason:
                stats[reason] += 1
                continue
            code, n = c["ident"]["code"], clean_name(c["name"], c["ident"]["code"])
            _record_promotion(conn, run, bid, n, c["detail"], c["check"], now)
            promoted.append({"board_id": bid, "ats_system": c["ident"]["ats"], "ats_code": code, "company_name": n,
                             "prior_basis": c["ident"]["basis"] or "", "name_source": c["detail"],
                             "jobs_count": "" if c["check"]["jobs_count"] is None else c["check"]["jobs_count"],
                             "region": c["ident"]["region"] or "",
                             "board_url": canonical_board_url(c["ident"]["ats"], code, c["ident"]["region"] or None)})
            by_ats[c["ident"]["ats"]] += 1
            by_source[c["detail"]] += 1
    if sample_out and promoted:
        rows = sorted(promoted, key=lambda r: (r["ats_system"], r["ats_code"].casefold()))
        size = max(1, int(sample_size or _DEFAULT_SAMPLE_SIZE))
        if len(rows) > size:
            step = (len(rows) - 1) / (size - 1)
            rows = [rows[round(i * step)] for i in range(size)]
        path = Path(sample_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]), extrasaction="ignore", lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
    return {"considered": len(candidates), "promoted": len(promoted), "by_ats": dict(sorted(by_ats.items())),
            "by_source": dict(sorted(by_source.items())), "skipped": dict(sorted(stats.items())), "sample": len(rows) if sample_out and promoted else 0}


def _job_board_page_identity(ats: str, page: dict | None) -> tuple[str | None, str | None]:
    """The organization the vendor names inside its own hosted job board: Ashby's window.__appData
    payload carries an organization record; Lever's hosted page title leads with the account."""
    if not page or page.get("status") != "ok":
        return None, None
    if ats == ASHBY:
        org = page.get("ashby_org") or {}
        return org.get("name"), "ashby_appdata_organization"
    return page.get("title"), "hosted_board_title"


def promote_job_board_orgs(conn, run: str, from_runs: list[str], now: str, ats_allowed: tuple[str, ...] = (LEVER, ASHBY)) -> dict:
    """Option 3: Lever and Ashby boards with open roles but no vendor-asserted name of their
    own read the vendor's job-board metadata from stored hosted pages. Only pages already
    cached are read; nothing is fetched for this."""
    candidates = _vendor_promotion_candidates(conn, from_runs)
    stats, by_ats = Counter(), Counter()
    option1 = 0
    with conn:
        for bid, c in candidates.items():
            ats = c["ident"]["ats"]
            if ats not in ats_allowed:
                continue
            if int(c["check"]["jobs_count"] or 0) <= 0:
                stats["no_open_jobs"] += 1
                continue
            if c["name"] and _promo_name_failure(c["name"], c["ident"]["code"], page_derived=False) == "":
                option1 += 1
                continue
            raw, detail = _job_board_page_identity(ats, store.page(conn, canonical_board_url(ats, c["ident"]["code"], c["ident"]["region"] or None)))
            if not raw:
                stats["no_page_identity"] += 1
                continue
            reason = _promo_name_failure(raw, c["ident"]["code"], page_derived=True)
            if reason:
                stats[reason] += 1
                continue
            n = clean_name(raw, c["ident"]["code"])
            _record_promotion(conn, run, bid, n, detail, c["check"], now)
            by_ats[ats] += 1
    return {"considered": len(candidates), "already_vendor_named": option1, "promoted": sum(by_ats.values()),
            "by_ats": dict(sorted(by_ats.items())), "skipped": dict(sorted(stats.items()))}


# ---------------------------------------------------------------------------
# Description recovery
# ---------------------------------------------------------------------------

# Hosted boards and careers pages describe jobs far more often than the company ("Job openings at X").
_JOB_COPY_RE = re.compile(
    r"\b(jobs?|careers?|openings?|vacanc\w*|vacantes?|positions?|hiring|hire|apply|applicants?|recruit\w*|roles?|opportunit\w*|talent|join|work (with|for|at)|employment|empleo)\b",
    re.I)
_FALLBACK_PATHS = (("/about", "about_page_meta"), ("/careers", "careers_page_meta"))


def usable_page_description(text) -> str | None:
    """A company description from a page that is not the company's homepage, or None. English
    only: the job-copy filter can't judge other languages, and this text is where job copy lives."""
    d = clean_description(text)
    return d if d and is_english(d) and not _JOB_COPY_RE.search(d) else None


def _usable_homepage_description(text) -> str | None:
    """A company's own meta description or vendor record, in any language."""
    return clean_description(text)


def _missing_descriptions(conn, runs: list[str]) -> list[dict]:
    return [r for r in decided_rows(conn, runs) if r["identity_status"] == CONFIRMED and r["verification_status"] in LIVE and not r["description"]]


def _homepage_url(conn, row: dict) -> str | None:
    return clean_website(row["website_url"]) if row["website_url"] else None


def _on_main_host(page: dict | None, site: str) -> bool:
    """The page answered from the site itself, not a redirect to another company or a careers host."""
    if not page or page.get("status") != "ok":
        return False
    final = host_of(page.get("final_url") or site)
    return registrable_domain(final) == registrable_domain(site) and final.split(".")[0] not in NON_MAIN_LABELS


def open_board_ids(conn, runs: list[str]) -> set[int]:
    """Live target boards a fresh enrichment could still improve: no confirmed identity, or
    confirmed with neither a website nor a description to build on."""
    out = set()
    for r in decided_rows(conn, runs):
        if r["verification_status"] not in LIVE:
            continue
        if r["identity_status"] in ("identity_unresolved", "identity_mismatch"):
            out.add(r["board_id"])
        elif r["identity_status"] == CONFIRMED and not r["website_url"] and not r["description"]:
            out.add(r["board_id"])
    return out


def recover_descriptions_cached(conn, run: str, from_runs: list[str], now: str) -> dict:
    """From what is already stored, most trustworthy first: the vendor's own text, the company's homepage, its hosted board."""
    missing = _missing_descriptions(conn, from_runs)
    recovered = Counter()
    with conn:
        for row in missing:
            bid = row["board_id"]
            found = None
            for r in conn.execute("SELECT provider_description, endpoint FROM verifications WHERE board_id=? AND provider_description IS NOT NULL ORDER BY checked_at DESC", (bid,)).fetchall():
                d = _usable_homepage_description(r["provider_description"])
                if d:
                    found = (d, "provider_description", r["endpoint"])
                    break
            site = _homepage_url(conn, row)
            if not found and site:
                page = store.page(conn, site)
                d = _usable_homepage_description(page.get("description")) if _on_main_host(page, site) else None
                if d:
                    found = (d, "homepage_meta", site)
            if not found:
                url = canonical_board_url(row["ats_system"], row["ats_code"], row["region"] or None)
                page = store.page(conn, url)
                d = usable_page_description(page.get("description")) if page and page.get("status") == "ok" else None
                if d:
                    found = (d, "board_page_meta", url)
            if found:
                _record_evidence(conn, run, bid, "description", found[0], found[1], found[2], found[0][:200], now)
                recovered[found[1]] += 1
    return {"missing": len(missing), "recovered": dict(sorted(recovered.items())), "still_missing": len(missing) - sum(recovered.values())}


def _fetch_description(client: HttpClient, site: str) -> tuple[str, str, str, dict] | None:
    """(description, method, url, page) from the live site: homepage, its English pages, then about and careers."""
    home = fetch_page(client, site)
    if _on_main_host(home, site):
        d = _usable_homepage_description(home.get("description"))
        if d:
            return d, "homepage_meta", site, home
        if clean_description(home.get("description")):
            for url in english_variant_urls(site):
                page = fetch_page(client, url)
                if _on_main_host(page, site):
                    d = _usable_homepage_description(page.get("description"))
                    if d:
                        return d, "homepage_meta_english_page", url, page
    elif not home or home.get("status") != "ok":
        return None
    for path, method in _FALLBACK_PATHS:
        url = site.rstrip("/") + path
        page = fetch_page(client, url)
        if page.get("status") == "ok" and registrable_domain(host_of(page.get("final_url") or url)) == registrable_domain(site):
            d = usable_page_description(page.get("description"))
            if d:
                return d, method, url, page
    return None


def recover_descriptions_network(conn, run: str, from_runs: list[str], client: HttpClient, now: str, workers: int = 12) -> dict:
    """Fetch the sites of confirmed live companies with a verified website and no description.
    Threads only fetch; the store is written on this thread, in one transaction."""
    missing = [r for r in _missing_descriptions(conn, from_runs) if r["website_url"]]
    sites = sorted({clean_website(r["website_url"]) for r in missing} - {None})
    with ThreadPoolExecutor(max_workers=workers) as ex:
        found = dict(zip(sites, ex.map(lambda s: _fetch_description(client, s), sites)))
    recovered = Counter()
    with conn:
        for row in missing:
            hit = found.get(clean_website(row["website_url"]))
            if hit:
                text, method, url, page = hit
                store.put_page(conn, url, page)
                _record_evidence(conn, run, row["board_id"], "description", text, method, url, text[:200], now)
                recovered[method] += 1
    return {"missing_with_website": len(missing), "sites_fetched": len(sites), "recovered": dict(sorted(recovered.items())),
            "still_missing": len(missing) - sum(recovered.values())}


# ---------------------------------------------------------------------------
# Ownership evidence
# ---------------------------------------------------------------------------

MAX_BOARDS_ON_OWNING_SITE = 3


def _harvest_tokens(raw_dir: Path, source: str) -> list[list]:
    path = Path(raw_dir) / source / "batches.jsonl"
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as fh:
        return [token for line in fh for token in json.loads(line)["tokens"]]


def _ownership_links(conn, raw_dir: Path) -> dict[tuple[str, str], tuple[str, str]]:
    """(ats, code) -> (how it is known, the owner's host). A page linking to many boards is a group,
    agency or portfolio site and proves nothing about any one of them."""
    links: dict[tuple[str, str], tuple[str, str]] = {}
    for ats, code, _, evidence in _harvest_tokens(raw_dir, "dns_cname"):
        if evidence.startswith("cname:"):
            links.setdefault((ats, code.casefold()), ("dns_cname", evidence.split(":", 1)[1].split("->")[0]))
    per_host: dict[str, set] = defaultdict(set)
    tokens = _harvest_tokens(raw_dir, "company_sites")
    for ats, code, _, evidence in tokens:
        per_host[registrable_domain(host_of(evidence))].add((ats, code.casefold()))
    for ats, code, _, evidence in tokens:
        host = host_of(evidence)
        if len(per_host[registrable_domain(host)]) <= MAX_BOARDS_ON_OWNING_SITE:
            links.setdefault((ats, code.casefold()), ("company_sites", host))
    for r in conn.execute("SELECT url, final_url, summary FROM pages WHERE status='ok'"):
        refs = set(json.loads(r["summary"]).get("board_refs") or [])
        if not refs or len(refs) > MAX_BOARDS_ON_OWNING_SITE:
            continue
        host = host_of(r["final_url"] or r["url"])
        if registrable_domain(host) in ATS_REGISTRABLE_DOMAINS:
            continue
        for ref in refs:
            ats, _, code = ref.partition(":")
            links.setdefault((ats, code.casefold()), ("stored_pages", host))
    return links


def apply_ownership_evidence(conn, run: str, from_runs: list[str], raw_dir: Path, client: HttpClient, now: str, workers: int = 16, ats_allowed=CORE_ATS) -> dict:
    """A company's own DNS or site pointing at a board proves whose board it is, which backs up the vendor's name.
    DNS needs nothing more: only the domain's owner can set it. A linking page must be that company's:
    its domain names the company, or, for sites from a company list, the page names itself as it."""
    links = _ownership_links(conn, raw_dir)
    latest = store.latest_verifications(conn)
    marks = ",".join("?" * len(from_runs))
    open_boards = {r["board_id"]: r for r in conn.execute(
        f"SELECT i.* FROM identities i JOIN (SELECT board_id, MAX(decided_at || '|' || run) AS pick FROM identities WHERE run IN ({marks}) GROUP BY board_id) t"
        " ON i.board_id=t.board_id AND (i.decided_at || '|' || i.run)=t.pick WHERE i.status IN ('identity_unresolved', 'identity_mismatch')", from_runs)}
    candidates = []
    for b in load_boards(conn):
        prior, v = open_boards.get(b["id"]), latest.get(b["id"])
        link = links.get((b["ats"], b["code"].casefold()))
        if not prior or not link or b["ats"] not in ats_allowed or not v or v["outcome"] not in LIVE:
            continue
        res = _result(conn, b, v, prior["name"] if prior["basis"] in _VENDOR_BASES else None)
        claims = name_claims(res, homepage=None)
        vendor = [c.name for c in claims if c.kind in (VENDOR_API, VENDOR_PAGE)]
        if vendor:
            candidates.append((b, v, link, claims, vendor))
    need_page = sorted({f"https://{c[2][1]}" for c in candidates if c[2][0] == "company_sites" and not any(domain_compatible(c[2][1], n) for n in c[4])})
    with ThreadPoolExecutor(max_workers=workers) as ex:
        fetched = dict(zip(need_page, ex.map(lambda u: fetch_page(client, u), need_page)))
    confirmed = Counter()
    with conn:
        for b, v, (source, host), claims, vendor in candidates:
            named_by_domain = any(domain_compatible(host, n) for n in vendor)
            if source == "stored_pages" and not named_by_domain:
                continue
            if source == "company_sites" and not named_by_domain and not homepage_self_naming(fetched.get(f"https://{host}"), vendor):
                continue
            ident = resolve_identity(claims, b["code"], board_ownership=True)
            if ident.status != CONFIRMED:
                continue
            _record_identity(conn, run, b["id"], ident, now)
            _record_evidence(conn, run, b["id"], "name", ident.name, "identity:" + "+".join(ident.basis), v["endpoint"], f"{source}:{host}; {_claims_text(claims)}", now)
            site = clean_website("https://" + registrable_domain(host))
            if site and any(domain_compatible(site, n) for n in vendor):
                _record_evidence(conn, run, b["id"], "website_url", site, f"{source}_links_board+domain_names_company", site, f"links_to_board:{b['ats']}:{b['code']}; {source}:{host}", now)
            confirmed[source] += 1
    return {"candidates": len(candidates), "confirmed": dict(sorted(confirmed.items()))}


# ---------------------------------------------------------------------------
# The last check before an export
# ---------------------------------------------------------------------------

_DEAD_URL_STATUSES = ("http_404", "http_410", "error")
_CHECK_BATCH = 200
_CHECK_PASSES = 3


def _check_board(validator: BoardValidator, row: dict) -> tuple[dict, dict, str]:
    cand = Candidate(ats=row["ats_system"], codes=[row["ats_code"]], region=row["region"] or None, sources=[])
    v = validator.validate_one(cand).to_dict()
    return row, v, classify(v["status"], v.get("http_status"), v.get("error"), v.get("jobs_count"))


def _dead_url_reason(client: HttpClient, url: str) -> str | None:
    """Why a URL no longer counts: gone, unreachable, parked, or fallen back to generic search.
    A 403 or 429 is a site refusing a robot, which says nothing about the page."""
    page = fetch_page(client, url)
    if page.get("status") in _DEAD_URL_STATUSES:
        return page.get("status")
    if page.get("status") != "ok":
        return None
    if PARKED_RE.search((page.get("title") or "") + " " + (page.get("description") or "")):
        return "parked_or_for_sale"
    if smartrecruiters_generic_page(page.get("final_url") or ""):
        return "generic_job_search_page"
    return None


def _record_fresh_check(conn, run: str, row: dict, v: dict, outcome: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO verifications(board_id, checked_at, origin, endpoint, http_status, outcome, legacy_status, error,"
        " jobs_count, code_validated, region_answered, provider_name, provider_identifier, provider_website, provider_description, provider_country)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (row["board_id"], v["checked_at"], run, validation_endpoint(row["ats_system"], row["ats_code"], v), v.get("http_status"), outcome, v["status"], v.get("error"),
         v.get("jobs_count"), v.get("code_validated"), v.get("region_answered"), v.get("company_name"), v.get("company_identifier"),
         v.get("website_url"), v.get("description"), v.get("country")))


def final_checks(conn, run: str, from_runs: list[str], client: HttpClient, validator: BoardValidator, now: str, workers: int = 16) -> dict:
    """Ask each provider again about every row that could be delivered, and fetch its optional URLs.
    Writes in batches and skips boards already checked under this run, so it can be restarted."""
    evidence = _latest_field_evidence_per_board_field(conn, from_runs)
    already = {r[0] for r in conn.execute(f"SELECT DISTINCT board_id FROM verifications WHERE origin=? AND outcome IN ({','.join('?' * len(store.CONCLUSIVE))})", (run, *store.CONCLUSIVE))}
    rows = [r for r in decided_rows(conn, from_runs) if r["identity_status"] == CONFIRMED and r["verification_status"] in LIVE and r["description"] and r["board_id"] not in already]
    outcomes = Counter()
    urls_rejected = Counter()
    pending = rows
    for attempt in range(_CHECK_PASSES):
        retry = []
        for start in range(0, len(pending), _CHECK_BATCH):
            batch = pending[start:start + _CHECK_BATCH]
            with ThreadPoolExecutor(max_workers=workers) as ex:
                checked = list(ex.map(lambda r: _check_board(validator, r), batch))
            live_urls = [(row["board_id"], field, evidence[(row["board_id"], field)]["value"]) for row, _, outcome in checked if outcome in LIVE
                         for field in ("website_url", "careersite_url") if (row["board_id"], field) in evidence and row[field]]
            with ThreadPoolExecutor(max_workers=workers) as ex:
                reasons = list(ex.map(lambda u: _dead_url_reason(client, clean_website(u[2]) if u[1] == "website_url" else u[2]), live_urls))
            with conn:
                for row, v, outcome in checked:
                    if outcome in store.CONCLUSIVE or attempt == _CHECK_PASSES - 1:
                        _record_fresh_check(conn, run, row, v, outcome)
                        outcomes[outcome] += 1
                    else:
                        retry.append(row)
                for (board_id, field, value), reason in zip(live_urls, reasons):
                    if reason:
                        conn.execute("INSERT OR IGNORE INTO field_evidence(board_id, field, value, method, evidence_url, evidence_text, status, observed_at, run)"
                                     " VALUES (?,?,?,'fresh_check',?,?,'rejected',?,?)", (board_id, field, value, value, reason, utc_now(), run))
                        urls_rejected[f"{field}:{reason}"] += 1
            print(f"[final-check] pass {attempt + 1}: {start + len(batch)}/{len(pending)} {dict(outcomes)}", flush=True)
        pending = retry
        if not pending:
            break
    return {"boards": len(rows), "outcomes": dict(sorted(outcomes.items())), "urls_rejected": dict(sorted(urls_rejected.items()))}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="company_dataset.repairs")
    p.add_argument("step", choices=["sr-existence", "reresolve", "reverify-recovery", "descriptions-cached", "descriptions-network", "final-checks", "ownership",
                                    "promote-vendor", "promote-job-board-orgs"])
    p.add_argument("--raw", default="data/raw")
    p.add_argument("--ats", default="", help="ownership: comma-separated ATS, default the five target ATS")
    p.add_argument("--ats-allowed", default="", help="job-board orgs: comma-separated ATS, default lever,ashby")
    p.add_argument("--db", required=True)
    p.add_argument("--run", required=True)
    p.add_argument("--from-runs", default="", help="newest decision per board is taken across these runs")
    p.add_argument("--from-run", default="recovery1", help="reverify-recovery: the run whose websites are checked")
    p.add_argument("--fast", action="store_true", help="final-checks: higher concurrency and request rates")
    p.add_argument("--refetch", action="store_true", help="reverify-recovery: fetch every site again instead of using stored pages")
    p.add_argument("--sample-out", default="", help="promote-vendor: write a QA sample CSV here")
    p.add_argument("--sample-size", type=int, default=_DEFAULT_SAMPLE_SIZE, help="promote-vendor: sample rows, default 100")
    a = p.parse_args(argv)
    conn = store.connect(a.db)
    from_runs = [r for r in a.from_runs.split(",") if r]
    if a.step == "sr-existence":
        out = backfill_smartrecruiters_existence(conn, a.run, utc_now())
    elif a.step == "reresolve":
        out = reresolve_identities(conn, a.run, from_runs, utc_now())
    elif a.step == "ownership":
        allowed = tuple(x for x in a.ats.split(",") if x) or CORE_ATS
        out = apply_ownership_evidence(conn, a.run, from_runs, Path(a.raw), HttpClient(timeout=20, per_host_interval=0.1, max_attempts=2), utc_now(), ats_allowed=allowed)
    elif a.step == "final-checks":
        client = HttpClient(timeout=20, per_host_interval=FAST_PER_HOST_INTERVAL if a.fast else 0.5, max_attempts=3)
        limits = resolve_rate_limits(overrides=FAST_RATE_LIMITS) if a.fast else resolve_rate_limits()
        out = final_checks(conn, a.run, from_runs, client, BoardValidator(client, rate_limiter=AtsRateLimiter(limits)), utc_now(), workers=FAST_WORKERS if a.fast else 16)
    elif a.step == "descriptions-cached":
        out = recover_descriptions_cached(conn, a.run, from_runs, utc_now())
    elif a.step == "descriptions-network":
        out = recover_descriptions_network(conn, a.run, from_runs, HttpClient(timeout=20, per_host_interval=0.5, max_attempts=2), utc_now())
    elif a.step == "promote-vendor":
        out = promote_vendor_identities(conn, a.run, from_runs, utc_now(), sample_out=a.sample_out or None, sample_size=a.sample_size)
    elif a.step == "promote-job-board-orgs":
        allowed = tuple(x for x in a.ats_allowed.split(",") if x) or (LEVER, ASHBY)
        out = promote_job_board_orgs(conn, a.run, from_runs, utc_now(), ats_allowed=allowed)
    else:
        out = reverify_recovery(conn, a.run, a.from_run, HttpClient(timeout=20, per_host_interval=0.5, max_attempts=2), utc_now(), refetch=a.refetch)
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
