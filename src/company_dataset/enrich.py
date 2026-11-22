"""Enrich: fresh evidence for boards, end to end, recorded in the store.

    python -m company_dataset.enrich --db data/store.db --raw data/raw \\
        --run pilot1 --out data/pilots/pilot1 --per-ats 300 --seed 20261031

For each sampled board: fresh provider validation; the board's hosted page (names,
website links, LinkedIn links, JSON-LD); candidate homepages, accepted only when the page
names the company or links back to the board; BigPicture and Wikidata by verified domain;
identity from independent evidence classes. Every accepted value is written to
field_evidence with its method, evidence URL/text and timestamp. All DB writes happen on the
main thread.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from company_dataset import careersite, reference as reference_mod, store, wikidata
from company_dataset.config import CACHE_DIR_NAME, FAST_PER_HOST_INTERVAL, FAST_RATE_LIMITS, FAST_WORKERS, resolve_rate_limits
from company_dataset.enrichment import (
    ENGLISH_HEADERS, board_from_validation, clean_description, content_website_candidates, english_variant_urls,
    fetch_greenhouse_content, fetch_page, greenhouse_content_url, is_auxiliary_board, is_english, is_test_board, landing_site,
    linkedin_url, name_key, names_match, summarize_page, vendor_websites,
)
from company_dataset.http import HttpClient, RequestError
from company_dataset.identity import (
    CONFIRMED, NameClaim, brand_segments, careers_branded_domain, claim_names, company_names_for_homepage, domain_compatible,
    linkedin_compatible, name_claims, names_compatible, registrable_domain, resolve_identity,
)
from company_dataset.ingest import interleave, load_boards, stratified_sample
from company_dataset.models import Candidate, utc_now
from company_dataset.normalize import CORE_ATS, GREENHOUSE, SMARTRECRUITERS, clean_website, host_of, validation_endpoint
from company_dataset.platforms import ICIMS, WORKDAY
from company_dataset.reference import domain_chain
from company_dataset.validate import AtsRateLimiter, BoardValidator
from company_dataset.validate_extra import vendor_identity
from company_dataset.verification import LIVE, RATE_LIMITED, RETRYABLE, classify

# Website pointers that come from the board itself (vendor data or the hosted page), so a
# homepage reached through them is independent evidence of who runs the board.
BOARD_ATTESTED = ("careersite_redirect_domain", "board_job_ad_link", "vendor_job_org_url", "ashby_public_website", "lever_home_page_link", "greenhouse_job_url_origin", "greenhouse_job_ads",
                  "jsonld_organization_url", "board_link_home_text", "board_link")
REFERENCE_NAME_BASIS = "reference_name_slug_domain"
RATE_LIMIT_STOP_AFTER = 50
# The widget already answers with name and description; a heavy page per board only runs into the rate limit.
WIDGET_ONLY_ATS = ("workable",)
USA_UK_CA = {"US": "US", "GB": "UK", "UK": "UK", "CA": "CA"}


def methodology_fingerprint() -> dict:
    """sha256 of every module a run's decisions depend on, so a run's method is on record."""
    here = Path(__file__).parent
    names = ["ingest.py", "identity.py", "enrich.py", "export.py", "careersite.py", "enrichment.py", "normalize.py", "platforms.py", "validate.py",
             "validate_extra.py", "verification.py", "reference.py", "wikidata.py", "resolve.py", "store.py"]
    return {n: hashlib.sha256((here / n).read_bytes()).hexdigest() for n in names}


# ---------------------------------------------------------------------------
# Work done in threads (network only, no DB)
# ---------------------------------------------------------------------------

def _fetch_raw(client: HttpClient, url: str) -> tuple[dict, str]:
    try:
        resp = client.get(url, headers=ENGLISH_HEADERS)
    except RequestError as exc:
        return {"status": "error", "error": str(exc)[:200], "final_url": "", "fetched_at": utc_now()}, ""
    if resp.status != 200:
        return {"status": f"http_{resp.status}", "final_url": resp.url, "fetched_at": utc_now()}, ""
    return summarize_page("ok", resp.url, resp.text[:3_000_000]), resp.text[:3_000_000]


class Costs:
    """Request counters shared by worker threads."""

    def __init__(self):
        self.counts = Counter()
        self.lock = threading.Lock()

    def add(self, key: str, n: int = 1):
        with self.lock:
            self.counts[key] += n


def stage_board(validator: BoardValidator, client: HttpClient, b: dict, costs: "Costs") -> dict:
    cand = Candidate(ats=b["ats"], codes=b["codes"] or [b["code"]], region=b["region"],
                     name=b["names"][0][1] if b["names"] else None, sources=b["sources"])
    t0 = time.monotonic()
    v = validator.validate_one(cand).to_dict()
    costs.add("validation_calls")
    outcome = classify(v["status"], v.get("http_status"), v.get("error"), v.get("jobs_count"))
    res = {"board": b, "validation": v, "outcome": outcome}
    if outcome in LIVE:
        board = board_from_validation(cand, v)
        res["careersite_url"] = board.careersite_url
        if b["ats"] in WIDGET_ONLY_ATS:
            res["board_page"], res["board_extract"] = None, None
        else:
            summary, html = _fetch_raw(client, board.careersite_url)
            costs.add("board_page_fetches")
            res["board_page"] = summary
            res["board_extract"] = careersite.extract(html, summary.get("final_url") or board.careersite_url) if html else None
        if b["ats"] in (WORKDAY, ICIMS) and (v.get("jobs_count") or 0) > 0:
            res["vendor_identity"] = vendor_identity(validator, b["ats"], v.get("code_validated") or b["code"])
            costs.add("vendor_identity_fetches", 2)
        if b["ats"] == GREENHOUSE and (v.get("jobs_count") or 0) > 0 and not v.get("website_url"):
            res["gh_content"] = fetch_greenhouse_content(client, greenhouse_content_url(board.code))
            costs.add("gh_content_fetches")
    res["seconds"] = time.monotonic() - t0
    return res


def website_candidates(res: dict, ref) -> list[tuple[str, str]]:
    b, v = res["board"], res["validation"]
    cand = Candidate(ats=b["ats"], codes=b["codes"] or [b["code"]], region=b["region"])
    board = board_from_validation(cand, v)
    out: list[tuple[str, str]] = []
    for site, basis in vendor_websites(board, v, res.get("board_page")):
        out.append((site, basis.removesuffix("_parent")))
    vi = res.get("vendor_identity") or {}
    if vi.get("url") and clean_website(vi["url"]):
        out.append((clean_website(vi["url"]), "vendor_job_org_url"))
    if res.get("board_extract"):
        names = [n for n, _ in careersite.board_page_names(res["board_extract"], b["code"])]
        for site, basis in careersite.rank_websites(res["board_extract"]["websites"], names[0] if names else None, b["code"])[:3]:
            out.append((site, basis.removesuffix("_parent")))
    if res.get("gh_content"):
        for site, _ in content_website_candidates(board, res["gh_content"]):
            out.append((site, "greenhouse_job_ads"))
    for name in claim_names(res):
        for site in ref.name_candidates(name, b["code"]):
            out.append((site, REFERENCE_NAME_BASIS))
    final = (res.get("board_page") or {}).get("final_url") or ""
    if final and host_of(final) != host_of(res.get("careersite_url") or "") and clean_website(final):
        # The ATS sends visitors to the company's own domain: that domain is board-attested.
        out.insert(0, (clean_website("https://" + registrable_domain(final)), "careersite_redirect_domain"))
    seen, dedup = set(), []
    names = claim_names(res)
    for site, basis in out:
        site = official_site(site)
        if site and site not in seen and not careers_branded_domain(site, names):
            seen.add(site)
            dedup.append((site, basis))
    return dedup[:4]


# First labels that mark a careers, investor or app host rather than the company's main site.
NON_MAIN_LABELS = {"careers", "career", "jobs", "job", "join", "work", "talent", "apply", "recruiting", "hiring",
                   "karriere", "werkenbij", "investor", "investors", "ir", "app", "portal", "login", "shop"}


def official_landing(site: str, landed: str | None) -> tuple[str | None, bool]:
    """(official website, landed on a careers host). acme.com redirecting to careers.acme.com is
    still acme.com; its careers page's meta text describes jobs, not the company."""
    if landed and landed != site and registrable_domain(landed) == registrable_domain(site) and host_of(landed).split(".")[0] in NON_MAIN_LABELS:
        return site, True
    return landed, False


def board_ownership(evidence: str, basis: str, page: dict) -> bool:
    """The homepage links to this board AND the board points at the homepage. A page linking to
    many boards (a group, agency or job-aggregator site) proves nothing about any one of them."""
    return evidence.startswith("links_to_board") and basis in BOARD_ATTESTED and len(set(page.get("board_refs") or [])) <= 3


def official_site(site: str) -> str | None:
    """careers.acme.com / investor.acme.com -> https://acme.com (still verified by fetching it)."""
    host = host_of(site)
    if host.split(".")[0] in NON_MAIN_LABELS and host.count(".") >= 2:
        return clean_website("https://" + registrable_domain(host))
    return site


PARKED_RE = re.compile(r"\bis for sale\b|\bbuy this domain\b|\bdomain (name )?for sale\b|\bparked (free|domain)\b", re.I)


def verify_homepage(page: dict | None, b: dict, names: list[str]) -> str | None:
    """Evidence text if the homepage identifies itself as this company, else None.

    The page must link back to the board, or name itself (og:site_name or a whole title
    segment) compatibly with the company's name. A name somewhere inside a marketing title
    ("Smart home with Samsung SmartThings | Samsung US") is not the site naming itself.
    Parked / for-sale pages never verify.
    """
    if not page or page.get("status") != "ok":
        return None
    if PARKED_RE.search((page.get("title") or "") + " " + (page.get("description") or "")):
        return None
    if f"{b['ats']}:{b['code'].casefold()}" in (page.get("board_refs") or []):
        return f"links_to_board:{b['ats']}:{b['code']}"
    return homepage_self_naming(page, names)


def homepage_self_naming(page: dict | None, names: list[str]) -> str | None:
    for n in names:
        for seg in brand_segments(page or {}):
            if names_compatible(seg, n):
                return f"site names itself {seg[:120]!r}, compatible with {n!r}"
    return None


# Website fields the company itself fills in its ATS account settings.
VENDOR_DECLARED = ("ashby_public_website", "lever_home_page_link")


def website_supported(site: str, page: dict | None, names: list[str], basis: str = "", declared: str | None = None) -> str | None:
    """A homepage linking to the board proves the board is linked from that site, not that the
    site is the company's main one (a city's recruiting campaign, a parent group, a brand). It is
    the company's website only if its domain names the company, the page names itself as it, or
    the company declared that same domain as its website in its own Ashby / Lever settings."""
    if not clean_website(site) or careers_branded_domain(site, names):
        return None
    if basis in VENDOR_DECLARED and declared and registrable_domain(declared) == registrable_domain(site):
        return f"vendor_declared_website:{basis}={declared}"
    for n in names:
        if domain_compatible(site, n):
            return f"domain names the company {n!r}"
    return homepage_self_naming(page, names)


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------

@dataclass
class RunContext:
    conn: object
    run: str
    raw: Path
    workers: int
    client: HttpClient
    validator: BoardValidator
    emit: object
    costs: Costs = field(default_factory=Costs)
    timings: dict = field(default_factory=dict)

    def timed(self, key: str, started: float) -> None:
        self.timings[key] = time.monotonic() - started


def undecided_boards(conn, boards: list[dict], ats: str | None) -> list[dict]:
    """Boards with no decision, plus those whose last check only failed to get an answer."""
    decided = {r[0] for r in conn.execute("SELECT DISTINCT board_id FROM identities")}
    latest = store.latest_verifications(conn)
    retry = {board_id for board_id, v in latest.items() if v["outcome"] in RETRYABLE}
    scope = set(ats.split(",")) if ats else set(CORE_ATS)
    return [b for b in boards if b["ats"] in scope and (b["id"] not in decided or b["id"] in retry)]


def check_boards(ctx: RunContext, sample: list[dict]) -> list[dict]:
    """Stage 1: provider validation and the board's hosted page."""
    t = time.monotonic()
    results = []
    with ThreadPoolExecutor(max_workers=ctx.workers) as ex:
        # Round-robin across ATS so workers don't all queue behind one provider's rate limit.
        futs = {ex.submit(stage_board, ctx.validator, ctx.client, b, ctx.costs): b for b in interleave(sample)}
        in_a_row: Counter = Counter()
        stopped: set[str] = set()
        for i, f in enumerate(as_completed(futs), 1):
            if f.cancelled():
                continue
            r = f.result()
            results.append(r)
            ats = r["board"]["ats"]
            in_a_row[ats] = in_a_row[ats] + 1 if r["outcome"] == RATE_LIMITED else 0
            if in_a_row[ats] >= RATE_LIMIT_STOP_AFTER and ats not in stopped:
                # Asking again only prolongs the limit. Boards not reached stay undecided for a later run.
                stopped.add(ats)
                skipped = sum(other.cancel() for other, board in futs.items() if board["ats"] == ats)
                ctx.emit("rate_limit_stop", ats=ats, boards_left=skipped)
            v = r["validation"]
            with ctx.conn:
                ctx.conn.execute(
                    "INSERT OR IGNORE INTO verifications(board_id, checked_at, origin, endpoint, http_status, outcome, legacy_status, error,"
                    " jobs_count, code_validated, region_answered, provider_name, provider_identifier, provider_website, provider_description, provider_country)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (r["board"]["id"], v["checked_at"], ctx.run, validation_endpoint(r["board"]["ats"], r["board"]["code"], v), v.get("http_status"), r["outcome"],
                     v["status"], v.get("error"), v.get("jobs_count"), v.get("code_validated"), v.get("region_answered"), v.get("company_name"),
                     v.get("company_identifier"), v.get("website_url"), v.get("description"), v.get("country")),
                )
                if r.get("board_page"):
                    store.put_page(ctx.conn, r["careersite_url"], r["board_page"])
            if i % 200 == 0:
                ctx.emit("stage1_progress", done=i, of=len(sample))
    ctx.timed("validation_and_board_pages", t)
    ctx.emit("stage1_done", outcomes=dict(Counter(r["outcome"] for r in results)))
    return results


def scan_reference(ctx: RunContext, live: list[dict]):
    """Stage 2: reference index for every name and candidate domain we might use."""
    t = time.monotonic()
    names, domains = set(), set()
    for r in live:
        for n in claim_names(r):
            if len(name_key(n)) >= 3:
                names.add(name_key(n))
        for site, _, _ in (r.get("board_extract") or {}).get("websites", []):
            domains.update(domain_chain(host_of(site)))
        if r["validation"].get("website_url"):
            domains.update(domain_chain(host_of(r["validation"]["website_url"])))
        for site, _ in vendor_websites(board_from_validation(Candidate(ats=r["board"]["ats"], codes=r["board"]["codes"] or [r["board"]["code"]]), r["validation"]), r["validation"], r.get("board_page")):
            domains.update(domain_chain(host_of(site)))
    ref = reference_mod.build(ctx.raw / "bigpicture" / reference_mod.META.filename, domains, names, cache_dir=ctx.raw.parent / CACHE_DIR_NAME)
    ctx.timed("reference_scan_1", t)
    ctx.emit("reference_1", domains=len(domains), names=len(names), matched_domains=len(ref.by_domain), matched_names=len(ref.by_name))
    return ref


def _fetch_all(ctx: RunContext, urls: list[str], pages: dict) -> None:
    with ThreadPoolExecutor(max_workers=ctx.workers) as ex:
        for url, page in zip(urls, ex.map(lambda u: fetch_page(ctx.client, u), urls)):
            pages[url] = page
    with ctx.conn:
        for url in urls:
            store.put_page(ctx.conn, url, pages[url])


def fetch_homepages(ctx: RunContext, live: list[dict], ref) -> tuple[dict, dict]:
    """Stage 3: every candidate homepage, once."""
    t = time.monotonic()
    plan = {r["board"]["id"]: website_candidates(r, ref) for r in live}
    urls = sorted({s for c in plan.values() for s, _ in c})
    pages: dict = {}
    _fetch_all(ctx, urls, pages)
    ctx.costs.add("homepage_fetches", len(urls))
    ctx.timed("homepages", t)
    ctx.emit("homepages", fetched=len(urls), ok=sum(1 for p_ in pages.values() if p_.get("status") == "ok"))
    return plan, pages


def homepage_is_independent(basis: str, evidence: str, ats: str, jobs) -> bool:
    """Does the homepage say something about this board beyond repeating its name? A name-matched
    site can't for a SmartRecruiters board with no jobs: any identifier can have a page there."""
    if basis in BOARD_ATTESTED or evidence.startswith("links_to_board"):
        return True
    return basis == REFERENCE_NAME_BASIS and not (ats == SMARTRECRUITERS and not jobs)


def choose_websites(live: list[dict], plan: dict, pages: dict) -> dict:
    """Each board's first candidate whose homepage checks out."""
    chosen = {}
    for r in live:
        b = r["board"]
        nm = company_names_for_homepage(r)
        for site, basis in plan[b["id"]]:
            page = pages.get(site)
            ev = verify_homepage(page, b, nm)
            if not ev:
                continue
            owns = board_ownership(ev, basis, page)
            # Ownership names the board's company even when the owning site is not its website.
            r["board_owned"] = r.get("board_owned") or owns
            landed, on_careers = official_landing(site, landing_site(site, page))
            if not landed:
                continue
            if ev.startswith("links_to_board"):
                support = website_supported(landed, page, nm, basis, site)
                if not support:
                    continue
                ev = f"{ev}; {support}"
            elif careers_branded_domain(landed, nm) or not clean_website(landed):
                continue
            chosen[b["id"]] = {"site": landed, "basis": basis, "page": page, "evidence": ev, "url": site, "landed_on_careers": on_careers,
                               "independent": homepage_is_independent(basis, ev, b["ats"], r["validation"].get("jobs_count")),
                               "board_ownership": owns}
            break
    return chosen


def fetch_english_variants(ctx: RunContext, chosen: dict, pages: dict) -> None:
    """Stage 3b: a non-English homepage description: ask for the site's English pages."""
    t = time.monotonic()
    variant_urls = []
    for c in chosen.values():
        d = clean_description(c["page"].get("description"))
        if not (d and is_english(d)):
            c["english_urls"] = english_variant_urls(c["site"])
            variant_urls += c["english_urls"]
    variant_urls = sorted(set(variant_urls) - set(pages))
    _fetch_all(ctx, variant_urls, pages)
    ctx.costs.add("english_variant_fetches", len(variant_urls))
    for c in chosen.values():
        for url in c.get("english_urls", []):
            pg = pages.get(url) or {}
            d = clean_description(pg.get("description")) if pg.get("status") == "ok" else None
            if d and is_english(d) and host_of(pg.get("final_url") or url) == host_of(c["site"]):
                c["english_description"], c["english_url"] = d, url
                break
    ctx.timed("english_variants", t)


def lookup_verified_domains(ctx: RunContext, chosen: dict) -> tuple:
    """Stage 4: reference again for verified domains; Wikidata for verified domains."""
    t = time.monotonic()
    vdomains = set()
    for c in chosen.values():
        vdomains.update(domain_chain(host_of(c["site"])))
    ref = reference_mod.build(ctx.raw / "bigpicture" / reference_mod.META.filename, vdomains, set(), cache_dir=ctx.raw.parent / CACHE_DIR_NAME)
    ctx.timed("reference_scan_2", t)
    t = time.monotonic()
    wd = {}
    wd_client = HttpClient(timeout=60, per_host_interval=1.0, max_attempts=4)
    hosts = sorted({host_of(c["site"]).removeprefix("www.") for c in chosen.values()})
    wd_failed = 0
    for i in range(0, len(hosts), wikidata.BATCH):
        try:
            wd.update(wikidata.lookup(wd_client, hosts[i:i + wikidata.BATCH]))
        except (RuntimeError, RequestError, ValueError) as exc:
            wd_failed += 1
            ctx.emit("wikidata_batch_failed", batch=i // wikidata.BATCH, error=str(exc)[:200])
        ctx.costs.add("wikidata_queries")
    ctx.emit("wikidata", hosts=len(hosts), matched=len(wd), failed_batches=wd_failed)
    ctx.timed("wikidata", t)
    return ref, wd


def record_decisions(ctx: RunContext, results: list[dict], chosen: dict, ref, wd: dict) -> list[dict]:
    """Stage 5: resolve identity and record evidence, as one transaction."""
    t = time.monotonic()
    rows = []
    with ctx.conn:
        for r in results:
            rows.append(resolve_and_record(ctx.conn, ctx.run, r, chosen.get(r["board"]["id"]), ref, wd))
    ctx.timed("resolve", t)
    return rows


RESULT_COLUMNS = ["board_id", "ats_system", "ats_code", "prior_state", "outcome", "jobs_count", "identity_status", "identity_basis", "identity_conflicts",
                  "name", "description", "linkedin_url", "website_url", "careersite_url", "careersite_status", "country", "country_method",
                  "website_method", "linkedin_method", "description_method", "complete_strict", "complete_lenient", "flags", "first_source", "sources"]


def write_outputs(ctx: RunContext, out: Path, rows: list[dict]) -> dict:
    with open(out / "results.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=RESULT_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    report = summarize(rows, ctx.costs.counts, ctx.timings)
    (out / "report.json").write_text(json.dumps(report, indent=1))
    ctx.emit("done", **{k: report[k] for k in ("boards", "complete_strict", "complete_lenient")})
    return report


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def select_boards(conn, args, emit) -> list[dict]:
    boards = load_boards(conn)
    if args.ats:
        boards = [b for b in boards if b["ats"] in set(args.ats.split(","))]
    for run in args.exclude_run:
        taken = {r[0] for r in conn.execute("SELECT board_id FROM identities WHERE run=?", (run,))}
        if not taken:
            raise SystemExit(f"--exclude-run {run}: no boards recorded for that run")
        boards = [b for b in boards if b["id"] not in taken]
        emit("excluded_run", run=run, boards=len(taken))
    if args.only_source:
        boards = [b for b in boards if b["sources"] == [args.only_source]]
        emit("only_source", source=args.only_source, boards=len(boards))
    if args.only_undecided:
        boards = undecided_boards(conn, boards, args.ats)
        emit("only_undecided", boards=len(boards))
    if args.board_ids:
        wanted = {int(line) for line in Path(args.board_ids).read_text().split()}
        boards = [b for b in boards if b["id"] in wanted]
        emit("board_ids", boards=len(boards))
    return boards


def write_sample(out: Path, sample: list[dict]) -> None:
    with open(out / "sample.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["board_id", "ats", "code", "prior_state", "first_source", "sources", "has_source_name"])
        for b in sample:
            w.writerow([b["id"], b["ats"], b["code"], b["prior"], b["sources"][0] if b["sources"] else "", ";".join(b["sources"]), bool(b["names"])])


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="company_dataset.enrich")
    p.add_argument("--db", required=True)
    p.add_argument("--raw", required=True)
    p.add_argument("--run", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--per-ats", type=int, default=300)
    p.add_argument("--seed", type=int, default=20261031)
    p.add_argument("--workers", type=int, default=24)
    p.add_argument("--ats", default="")
    p.add_argument("--exclude-run", action="append", default=[], help="leave out boards already sampled by this run")
    p.add_argument("--only-source", default="", help="sample only boards that no other source lists (new discoveries)")
    p.add_argument("--only-undecided", action="store_true", help="only target-ATS boards that have no identity decision yet")
    p.add_argument("--board-ids", default="", help="file of board ids, whitespace separated: work on these boards only")
    p.add_argument("--fast", action="store_true", help="higher concurrency and request rates; watch the report for rate-limited boards")
    p.add_argument("--require-frozen", default="", help="methodology freeze file; refuse to run if the code differs")
    a = p.parse_args(argv)
    if a.require_frozen:
        frozen = json.loads(Path(a.require_frozen).read_text())["code_sha256"]
        changed = sorted(k for k, v in methodology_fingerprint().items() if frozen.get(k) != v)
        if changed:
            raise SystemExit(f"methodology differs from the freeze in {a.require_frozen}: {changed}")
    out = Path(a.out)
    if out.exists():
        raise SystemExit(f"refusing to overwrite {out}")
    out.mkdir(parents=True)
    conn = store.connect(a.db)
    log = open(out / "log.jsonl", "a", encoding="utf-8")

    def emit(event: str, **kw):
        log.write(json.dumps({"t": utc_now(), "event": event, **kw}) + "\n")
        log.flush()
        print(event, kw if len(str(kw)) < 300 else str(kw)[:300], flush=True)

    t = time.monotonic()
    boards = select_boards(conn, a, emit)
    (out / "methodology.json").write_text(json.dumps({"args": vars(a), "code_sha256": methodology_fingerprint()}, indent=1))
    sample = stratified_sample(boards, a.per_ats, a.seed)
    write_sample(out, sample)
    emit("sample", size=len(sample), by_ats=dict(Counter(b["ats"] for b in sample)))

    client = HttpClient(timeout=20, per_host_interval=FAST_PER_HOST_INTERVAL if a.fast else 0.5, max_attempts=3)
    limits = resolve_rate_limits(overrides=FAST_RATE_LIMITS) if a.fast else resolve_rate_limits()
    ctx = RunContext(conn=conn, run=a.run, raw=Path(a.raw), workers=FAST_WORKERS if a.fast else a.workers, client=client, emit=emit,
                     validator=BoardValidator(client, rate_limiter=AtsRateLimiter(limits)))
    ctx.timed("sample", t)

    results = check_boards(ctx, sample)
    live = [r for r in results if r["outcome"] in LIVE]
    ref = scan_reference(ctx, live)
    plan, pages = fetch_homepages(ctx, live, ref)
    chosen = choose_websites(live, plan, pages)
    fetch_english_variants(ctx, chosen, pages)
    verified_ref, wd = lookup_verified_domains(ctx, chosen)
    rows = record_decisions(ctx, results, chosen, verified_ref, wd)
    write_outputs(ctx, out, rows)
    return 0


def resolve_and_record(conn, run: str, r: dict, chosen: dict | None, ref, wd: dict) -> dict:
    b, v = r["board"], r["validation"]
    now = utc_now()
    code = v.get("code_validated") or (b["codes"][0] if b["codes"] else b["code"])
    row = {"board_id": b["id"], "ats_system": b["ats"], "ats_code": code, "prior_state": b["prior"], "outcome": r["outcome"],
           "jobs_count": v.get("jobs_count"), "first_source": b["sources"][0] if b["sources"] else "", "sources": ";".join(b["sources"]),
           "flags": []}

    def ev(field_, value, method, url=None, text=None, status="verified"):
        conn.execute("INSERT OR IGNORE INTO field_evidence(board_id, field, value, method, evidence_url, evidence_text, status, observed_at, run)"
                     " VALUES (?,?,?,?,?,?,?,?,?)", (b["id"], field_, value, method, url, (text or "")[:500], status, now, run))

    def decide(status, name=None, basis="", conflicts=""):
        conn.execute("INSERT OR REPLACE INTO identities(board_id, run, status, name, basis, conflicts, decided_at) VALUES (?,?,?,?,?,?,?)",
                     (b["id"], run, status, name, basis, conflicts, now))

    if r["outcome"] not in LIVE:
        decide("not_live")
        row.update({"identity_status": "not_live"})
        row["flags"] = ""
        row["complete_strict"] = row["complete_lenient"] = False
        return row

    endpoint = validation_endpoint(b["ats"], code, v)
    ev("ats_system", b["ats"], "provider_api", endpoint, f"http {v.get('http_status')} {r['outcome']}")
    ev("ats_code", code, "provider_api", endpoint, f"code accepted by provider: {code}")

    # careersite
    page = r.get("board_page") or {}
    final = page.get("final_url") or ""
    cs_status = "unverified"
    if page.get("status") == "ok" and (host_of(final) == host_of(r["careersite_url"]) or (chosen and host_of(final).endswith(host_of(chosen["site"]).removeprefix("www.")))):
        cs_status = "verified"
        ev("careersite_url", r["careersite_url"], "hosted_page_fetch", final, page.get("title"))
    elif page.get("status") in ("http_403", "http_405", "http_429"):
        cs_status = "blocked_by_security"
        ev("careersite_url", r["careersite_url"], "canonical_url_provider_live_page_blocked", r["careersite_url"], page.get("status"), status="candidate")
    elif page.get("status") == "ok":
        cs_status = "redirected_elsewhere"
        row["flags"].append(f"careersite_redirect:{host_of(final)}")
    else:
        cs_status = page.get("status") or "not_fetched"

    # Vendor demo / sandbox boards and a company's secondary boards are live but are not companies.
    names_seen = [c.name for c in name_claims(r, homepage=None)]
    if any(is_test_board(n, b["code"]) for n in names_seen) or any(is_auxiliary_board(n) for n in names_seen if n):
        decide("not_a_company_board", None, "test_demo_or_auxiliary_board", "; ".join(names_seen[:3]))
        row.update({"identity_status": "not_a_company_board", "careersite_url": r["careersite_url"], "careersite_status": cs_status,
                    "complete_strict": False, "complete_lenient": False, "flags": ";".join(row["flags"])})
        return row

    # website + homepage-independent identity evidence
    wd_items = wd.get(host_of(chosen["site"]).removeprefix("www."), []) if chosen else []
    wd_label = wd_items[0]["label"] if len(wd_items) == 1 else None
    ident = resolve_identity(name_claims(r, chosen["page"] if chosen else None, bool(chosen and chosen["independent"]), wd_label), b["code"],
                             board_ownership=bool(chosen and chosen.get("board_ownership")) or bool(r.get("board_owned")))
    row.update({"identity_status": ident.status, "identity_basis": "+".join(ident.basis), "identity_conflicts": " | ".join(ident.conflicts)})
    name = ident.name or ""
    decide(ident.status, ident.name, "+".join(ident.basis), " | ".join(ident.conflicts))
    if ident.status == CONFIRMED:
        claims_txt = "; ".join(f"{c.kind}:{c.origin}={c.name}" + (f" (raw={c.raw})" if c.raw and c.raw != c.name else "")
                               for c in name_claims(r, chosen["page"] if chosen else None, bool(chosen and chosen["independent"]), wd_label))
        ev("name", name, "identity:" + "+".join(ident.basis), endpoint, claims_txt[:500])

    website = ""
    if chosen:
        website = chosen["site"]
        ev("website_url", website, f"{chosen['basis']}+homepage_names_company", chosen["url"], chosen["evidence"])
        refname = ref.name(website)
        if refname and name and not names_match(refname, name):
            row["flags"].append(f"reference_name_differs:{refname}")
        if wd_label and name and not names_match(wd_label, name):
            row["flags"].append(f"wikidata_label_differs:{wd_label}")

    # LinkedIn
    linkedin, li_method = "", ""
    if website:
        hp = chosen["page"]
        home_slugs = hp.get("linkedin_slugs") or []
        board_slugs = (r.get("board_extract") or {}).get("linkedin_slugs") or []
        ref_slug = ref.linkedin_slug(website, name)
        wd_ids = sorted({x for i in wd_items for x in i["linkedin"]}) if len(wd_items) == 1 else []
        lower = lambda xs: {x.lower(): x for x in xs}
        H, B = lower(home_slugs), lower(board_slugs)
        refs = {x.lower() for x in ([ref_slug] if ref_slug else []) + wd_ids}
        # Every candidate must name this company (or its domain); agreement between sources is
        # not enough on its own, because a parent's page is often what both list.
        fits = lambda slug: linkedin_compatible(slug, name, website)
        options = [(slug, "homepage_link") for slug in home_slugs] + [(slug, "careersite_link") for slug in board_slugs]
        if ref_slug and ref_slug.lower() in {x.lower() for x in wd_ids}:
            options.append((ref_slug, "bigpicture_and_wikidata_by_domain"))
        if ref_slug:
            options.append((ref_slug, "bigpicture_by_verified_domain"))
        options += [(x, "wikidata_by_verified_domain") for x in wd_ids[:1]]
        for slug, where in options:
            if fits(slug):
                agree = "+reference_agrees" if where.endswith("_link") and slug.lower() in refs else ""
                linkedin, li_method = linkedin_url(slug), where + agree
                break
        if not linkedin and len(home_slugs) == 1 and home_slugs[0].isdigit():
            linkedin, li_method = linkedin_url(home_slugs[0]), "homepage_only_company_link_numeric"
        for slug, where in options:
            if not fits(slug) and not slug.isdigit():
                ev("linkedin_url", linkedin_url(slug), where, chosen["url"], "slug names neither the company nor its domain", status="rejected")
        if linkedin:
            ev("linkedin_url", linkedin, li_method, chosen["url"], f"home={home_slugs[:3]} board={board_slugs[:3]} ref={ref_slug} wd={wd_ids[:2]}")
            if ref_slug and li_method.startswith(("homepage", "careersite")) and ref_slug.lower() != linkedin.rsplit("/", 1)[-1].lower():
                row["flags"].append(f"linkedin_reference_differs:{ref_slug}")

    # description
    description, d_method = "", ""
    if website and chosen.get("landed_on_careers"):
        row["flags"].append("root_redirects_to_careers_host")
    if website and not chosen.get("landed_on_careers"):
        d = clean_description(chosen["page"].get("description"))
        if d and is_english(d):
            description, d_method = d, "homepage_meta"
        elif chosen.get("english_description"):
            description, d_method = chosen["english_description"], "homepage_meta_english_page"
        elif d:
            description, d_method = d, "homepage_meta_non_english"
    if not description:
        d = clean_description(v.get("description"))
        if d:
            description, d_method = d, f"{b['ats']}_vendor_description"
    if description:
        ev("description", description, d_method, {"homepage_meta": (chosen or {}).get("url"), "homepage_meta_non_english": (chosen or {}).get("url"), "homepage_meta_english_page": (chosen or {}).get("english_url")}.get(d_method, endpoint), description[:200])

    country, c_method = "", ""
    if website:
        c = ref.country(website, name)
        if c:
            country, c_method = c, "bigpicture_by_verified_domain"
        elif len(wd_items) == 1 and len(wd_items[0]["country"]) == 1:
            country, c_method = wd_items[0]["country"][0], "wikidata_by_verified_domain"
    if not country and v.get("country"):
        country, c_method = v["country"], v.get("country_basis") or "job_locations"
    if country:
        ev("country", country, c_method, None, None)

    fields_ok = ident.status == CONFIRMED and all([name, description, linkedin, website])
    row.update({"name": name, "description": description, "linkedin_url": linkedin, "website_url": website,
                "careersite_url": r["careersite_url"], "careersite_status": cs_status, "country": country, "country_method": c_method,
                "website_method": (chosen or {}).get("basis", ""), "linkedin_method": li_method, "description_method": d_method,
                "complete_strict": fields_ok and cs_status == "verified",
                "complete_lenient": fields_ok and cs_status in ("verified", "blocked_by_security"),
                "flags": ";".join(row["flags"])})
    return row


def summarize(rows: list[dict], costs: Counter, timings: dict) -> dict:
    def geo(r):
        return USA_UK_CA.get((r.get("country") or "").upper(), "other" if r.get("country") else "unknown")

    rep = {"boards": len(rows), "complete_strict": sum(1 for r in rows if r.get("complete_strict")),
           "complete_lenient": sum(1 for r in rows if r.get("complete_lenient")), "costs": dict(costs),
           "timings_seconds": {k: round(v, 1) for k, v in timings.items()}, "by_ats": {}}
    for ats in sorted({r["ats_system"] for r in rows}):
        g = [r for r in rows if r["ats_system"] == ats]
        live = [r for r in g if r["outcome"] in LIVE]
        conf = [r for r in live if r.get("identity_status") == CONFIRMED]
        rep["by_ats"][ats] = {
            "sampled": len(g),
            "outcomes": dict(Counter(r["outcome"] for r in g)),
            "live": len(live),
            "identity": dict(Counter(r.get("identity_status") for r in live)),
            "field_verified_of_live": {
                "name": len(conf),
                "website_url": sum(1 for r in live if r.get("website_url")),
                "linkedin_url": sum(1 for r in live if r.get("linkedin_url")),
                "description": sum(1 for r in live if r.get("description")),
                "careersite_url_verified": sum(1 for r in live if r.get("careersite_status") == "verified"),
                "careersite_url_blocked": sum(1 for r in live if r.get("careersite_status") == "blocked_by_security"),
                "ats_system": len(live), "ats_code": len(live),
            },
            "complete_strict": sum(1 for r in g if r.get("complete_strict")),
            "complete_lenient": sum(1 for r in g if r.get("complete_lenient")),
            "complete_strict_by_geo": dict(Counter(geo(r) for r in g if r.get("complete_strict"))),
            "live_by_geo": dict(Counter(geo(r) for r in live)),
            "live_with_jobs": sum(1 for r in live if r["outcome"] == "verified_live"),
            "complete_strict_with_jobs": sum(1 for r in g if r.get("complete_strict") and r["outcome"] == "verified_live"),
            "flags": dict(Counter(f.split(":")[0] for r in g for f in (r.get("flags") or "").split(";") if f)),
            "methods": {k: dict(Counter(r.get(k) for r in live if r.get(k))) for k in ("website_method", "linkedin_method", "description_method")},
        }
    return rep


if __name__ == "__main__":
    raise SystemExit(main())
