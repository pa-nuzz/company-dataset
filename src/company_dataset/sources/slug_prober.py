"""Guess board identifiers from company names and domains, and ask each vendor whether they exist.

    python -m company_dataset.sources.slug_prober --raw data/raw --db data/store.db --limit-per-vendor 5000

A deliberately capped trial: each vendor gets its own thread at a hard request limit, and stops for
good if it keeps answering 429. Identifiers already in the store are never probed. A hit proves a
board exists under that identifier, not whose it is, so the guessed name is not kept as evidence.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote

import duckdb

from company_dataset.enrichment import name_key
from company_dataset.http import HttpClient, RequestError
from company_dataset.identity import registrable_domain
from company_dataset.models import SourceMeta
from company_dataset.normalize import ASHBY, BREEZY, GREENHOUSE, LEVER, SMARTRECRUITERS, canonical_board_url, host_of
from company_dataset.sources._harvest import board_from_url, parse_tokens, write_tokens
from company_dataset.validate import smartrecruiters_generic_page

VENDORS = (GREENHOUSE, LEVER, ASHBY, SMARTRECRUITERS)
SECONDS_BETWEEN_REQUESTS = 0.5  # 2 requests a second per vendor
MAX_CONSECUTIVE_RATE_LIMITS = 5
RATE_LIMIT_BACKOFF_SECONDS = 30
LARGE_COMPANY_SIZES = ("1K-5K", "5K-10K", "10K+")
LIVE, MISSING, RATE_LIMITED, ERROR = "live", "missing", "rate_limited", "error"

META = SourceMeta(
    name="slug_prober_2026_11_03",
    description="Board identifiers guessed from BigPicture company names and domains, confirmed against each vendor's public endpoint",
    download_url="https://huggingface.co/datasets/bigpictureio/companies-2023-q4-sm",
    filename="tokens.csv",
    license="ODC-By-1.0 (BigPicture names used as guesses only)",
    license_url="https://opendatacommons.org/licenses/by/1-0/",
    license_note="Only identifiers a vendor answered for are kept; every board is verified against its provider before use.",
    attribution="BigPicture (bigpicture.io) Free Company Dataset 2023 Q4, for candidate generation",
    downloadable=False,
)


def candidate_slugs(name: str | None, website: str | None) -> list[str]:
    """The domain's label ("stripe"), the name run together ("acmerobotics"), and the name hyphenated ("acme-robotics")."""
    out = []
    if website:
        out.append(re.sub(r"[^a-z0-9-]", "", registrable_domain(host_of(website) or website).split(".")[0].lower()))
    key = name_key(name)
    if key:
        out.append(key)
        words = re.findall(r"[a-z0-9]+", (name or "").lower())
        if len(words) > 1:
            out.append("-".join(words))
    return [s for s in dict.fromkeys(out) if 3 <= len(s) <= 60]


def probe_url(ats: str, slug: str) -> str:
    q = quote(slug, safe="")
    if ats == GREENHOUSE:
        return f"https://boards-api.greenhouse.io/v1/boards/{q}"
    if ats == LEVER:
        return f"https://api.lever.co/v0/postings/{q}?mode=json&limit=1"
    if ats == ASHBY:
        return f"https://api.ashbyhq.com/posting-api/job-board/{q}"
    if ats == BREEZY:
        return f"https://{q}.breezy.hr/json"
    # The SmartRecruiters API answers 200 for any identifier: only its careers page can tell.
    return canonical_board_url(SMARTRECRUITERS, slug)


def probe_detail(client: HttpClient, ats: str, slug: str) -> tuple[str, tuple[str, str, str] | None]:
    """(result, another board this one redirected to, if any)."""
    try:
        resp = client.get(probe_url(ats, slug))
    except RequestError:
        return ERROR, None
    if resp.status == 429:
        return RATE_LIMITED, None
    if resp.status in (404, 410):
        return MISSING, None
    if resp.status != 200:
        return ERROR, None
    if ats == SMARTRECRUITERS and smartrecruiters_generic_page(resp.url):
        return MISSING, None
    landed = board_from_url(resp.url or "")
    moved = landed if landed and (landed[0], landed[1].casefold()) != (ats, slug.casefold()) else None
    return LIVE, moved


def probe(client: HttpClient, ats: str, slug: str) -> str:
    return probe_detail(client, ats, slug)[0]


def probe_vendor(client: HttpClient, ats: str, slugs: list[str], sleep=time.sleep, on_hit=lambda ats, slug, moved=None: None) -> dict:
    stats = {"tried": 0, LIVE: 0, MISSING: 0, ERROR: 0, RATE_LIMITED: 0, "stopped_by_rate_limit": False}
    streak = 0
    for slug in slugs:
        result, moved = probe_detail(client, ats, slug)
        stats["tried"] += 1
        stats[result] += 1
        if result == RATE_LIMITED:
            streak += 1
            if streak >= MAX_CONSECUTIVE_RATE_LIMITS:
                stats["stopped_by_rate_limit"] = True
                break
            sleep(RATE_LIMIT_BACKOFF_SECONDS * streak)
            continue
        streak = 0
        if result == LIVE:
            on_hit(ats, slug, moved)
    return stats


def large_companies(raw_dir: Path) -> list[tuple[str, str]]:
    from company_dataset.reference import META as BIGPICTURE

    path = Path(raw_dir) / BIGPICTURE.name / BIGPICTURE.filename
    return duckdb.sql(
        "SELECT name, website FROM read_csv(?, header=true, all_varchar=true, ignore_errors=true, parallel=false, quote='\"')"
        " WHERE size IN ('1K-5K', '5K-10K', '10K+') AND website IS NOT NULL ORDER BY website, name", params=[str(path)]).fetchall()


def plan_slugs(companies: list[tuple[str, str]], known: set[tuple[str, str]], limit: int, seed: int) -> dict[str, list[str]]:
    """Per vendor: a seeded random sample of up to `limit` identifiers, leaving out those already in the store."""
    shuffled = list(companies)
    random.Random(seed).shuffle(shuffled)
    plan = {}
    for ats in VENDORS:
        chosen: dict[str, None] = {}
        for name, website in shuffled:
            for slug in candidate_slugs(name, website):
                if (ats, slug.casefold()) not in known:
                    chosen.setdefault(slug)
            if len(chosen) >= limit:
                break
        plan[ats] = list(chosen)[:limit]
    return plan


def run(raw_dir: Path, plan: dict[str, list[str]], client_factory=None, meta: SourceMeta = META, keep_existing: bool = False, report_key: str | None = None) -> dict:
    """keep_existing: add to an earlier run's hits and report instead of replacing them. report_key: this run's name in the report, for a single vendor."""
    out = Path(raw_dir) / meta.name
    out.mkdir(parents=True, exist_ok=True)
    tokens: dict[tuple[str, str], tuple[str, str, str]] = {}
    lock = threading.Lock()
    report: dict[str, dict] = {}
    if keep_existing:
        with open(out / meta.filename, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                tokens[(row["ats"], row["code"].casefold())] = (row["code"], row["region"], row["evidence"])
        report = json.loads((out / "probe_report.json").read_text())

    def on_hit(ats, slug, moved=None):
        with lock:
            tokens[(ats, slug.casefold())] = (slug, "", "slug_probe")
            if moved:
                tokens.setdefault((moved[0], moved[1].casefold()), (moved[1], moved[2], f"redirect:{ats}:{slug}"))

    def work(ats):
        client = (client_factory or (lambda: HttpClient(timeout=20, max_attempts=1, per_host_interval=SECONDS_BETWEEN_REQUESTS)))()
        key = report_key or ats
        report[key] = probe_vendor(client, ats, plan[ats], on_hit=on_hit)
        print(f"[prober] {key}: {report[key]}", flush=True)

    threads = [threading.Thread(target=work, args=(ats,)) for ats in plan]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    write_tokens(out / meta.filename, tokens)
    (out / "probe_report.json").write_text(json.dumps(report, indent=1, sort_keys=True))
    return report


def parse(path: Path):
    return parse_tokens(path, META.name)


def main(argv=None) -> int:
    from company_dataset import store

    p = argparse.ArgumentParser(prog="company_dataset.sources.slug_prober")
    p.add_argument("--raw", default="data/raw")
    p.add_argument("--db", required=True)
    p.add_argument("--limit-per-vendor", type=int, required=True)
    p.add_argument("--seed", type=int, default=20261103)
    a = p.parse_args(argv)
    conn = store.connect(a.db)
    known = {(r["ats"], r["code_key"]) for r in conn.execute("SELECT ats, code_key FROM boards")}
    conn.close()
    plan = plan_slugs(large_companies(Path(a.raw)), known, a.limit_per_vendor, a.seed)
    print(json.dumps(run(Path(a.raw), plan), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
