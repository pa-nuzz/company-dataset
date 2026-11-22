"""Workable boards beyond those already stored: GitHub files that list them, and {code}.workable.com hosts.

    subfinder -d workable.com -all -silent -o data/raw/workable_discovery/subfinder.txt
    python -m company_dataset.sources.workable_discovery --raw data/raw

Workable is delivered separately from the five target ATS, so this keeps to its own source and file.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import subprocess
import time
import unicodedata
from pathlib import Path
from urllib.parse import quote, urlencode

import duckdb
import requests

from company_dataset.http import HttpClient, RequestError
from company_dataset.models import SourceMeta
from company_dataset.normalize import parse_board_url
from company_dataset.platforms import WORKABLE
from company_dataset.sources._harvest import board_from_url, boards_in_text, parse_tokens, plausible_slug, write_tokens
from company_dataset.sources.github_lists import MAX_FILE_BYTES, _gh
from company_dataset.sources.slug_files import VENDOR_WORDS, discover, slugs_in_file

QUERIES = ["filename:workable_slugs", "filename:workable_boards", "filename:companies_workable", "filename:slugs.json workable", "filename:workable.txt", "filename:workable.json",
           '"apply.workable.com" extension:md', '"apply.workable.com" extension:json', '"apply.workable.com" extension:csv', '"apply.workable.com" extension:txt', '"apply.workable.com" extension:yaml']
ALLOWED = (WORKABLE,)
JSON_ACCEPT = {"Accept": "application/json"}

META = SourceMeta(
    name="workable_discovery",
    description="Workable boards from public GitHub files and passive DNS",
    download_url="https://github.com/search",
    filename="tokens.csv",
    license="Public repository data and DNS records; only identifiers are read",
    license_url="",
    license_note="Only board identifiers are read; every board is verified against Workable before use.",
    attribution="Public GitHub repositories and subfinder's passive sources",
    downloadable=False,
)


def board_from_host(host: str) -> str | None:
    host = host.strip().lower().rstrip(".").removeprefix("*.")
    if not host.endswith(".workable.com") or host.count(".") != 2 or host.split(".")[0] in VENDOR_WORDS:
        return None
    parsed = parse_board_url(f"https://{host}/")
    return parsed.code if parsed and parsed.ats == WORKABLE and plausible_slug(parsed.code) else None


def harvest(raw_dir: Path, client: HttpClient, search=_gh, sleep=time.sleep) -> dict:
    out = Path(raw_dir) / META.name
    out.mkdir(parents=True, exist_ok=True)
    tokens: dict = {}
    passive = out / "subfinder.txt"
    if passive.exists():
        for line in passive.read_text(errors="ignore").splitlines():
            code = board_from_host(line)
            if code:
                tokens.setdefault((WORKABLE, code.casefold()), (code, "", f"subfinder:{line.strip()}"))
    files = 0
    for url in discover(search, sleep, queries=QUERIES, pages=2):
        try:
            resp = client.get(url)
        except (RequestError, subprocess.SubprocessError):
            continue
        if resp.status != 200 or len(resp.text) > MAX_FILE_BYTES:
            continue
        files += 1
        for ats, code in slugs_in_file(url, resp.text):
            if ats == WORKABLE:
                tokens.setdefault((WORKABLE, code.casefold()), (code, "", url))
        for key, value in boards_in_text(resp.text, url, allowed=ALLOWED).items():
            tokens.setdefault(key, value)
    write_tokens(out / META.filename, tokens)
    return {"files_read": files, "boards": len(tokens)}


# ---------------------------------------------------------------------------
# Workable's own job site, Common Crawl and the Wayback Machine
# ---------------------------------------------------------------------------

JOBS_API = "https://jobs.workable.com/api/v1/jobs"
ACCOUNT_API = "https://apply.workable.com/api/v1/widget/accounts"
MATCH_BACKOFF_SECONDS = (5, 15, 45, 120)
MATCH_IDLE_SECONDS = 120
MATCH_INTERVAL_SECONDS = 0.4
TOKENS_EVERY_PAGES = 50
CC_PREFIX = "com,workable,apply)/"
WAYBACK_TARGET = ("apply.workable.com/*", None)


def _hits_path(raw_dir: Path) -> Path:
    path = Path(raw_dir) / META.name / "hits.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def rebuild_tokens(raw_dir: Path) -> int:
    """tokens.csv from everything found so far, so a growing harvest can be imported at any point."""
    tokens: dict = {}
    out = Path(raw_dir) / META.name
    existing = out / META.filename
    if existing.exists():
        with open(existing, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                tokens[(row["ats"], row["code"].casefold())] = (row["code"], row["region"], row["evidence"])
    if _hits_path(raw_dir).exists():
        with open(_hits_path(raw_dir), encoding="utf-8") as fh:
            for line in fh:
                hit = json.loads(line)
                tokens.setdefault((WORKABLE, hit["code"].casefold()), (hit["code"], "", hit["evidence"]))
    return write_tokens(existing, tokens)


def _code_from_link(url: str | None) -> str | None:
    board = board_from_url(url or "", ALLOWED)
    return board[1] if board else None


def walk_jobs(raw_dir: Path, client: HttpClient, max_pages: int | None = None) -> dict:
    """Page through every live job and record each employer once. Resumes from the last saved page."""
    out = Path(raw_dir) / META.name
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / "jobs_state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {"token": None, "pages": 0, "companies": [], "done": False}
    seen = set(state["companies"])
    pages = 0
    with open(out / "employers.jsonl", "a", encoding="utf-8") as employers:
        while not state.get("done") and (max_pages is None or pages < max_pages):
            url = JOBS_API + ("?" + urlencode({"pageToken": state["token"]}) if state["token"] else "")
            try:
                resp = client.get(url, headers=JSON_ACCEPT)
            except RequestError as exc:
                print(f"[workable-jobs] {type(exc).__name__}; stopping, finished pages are saved", flush=True)
                break
            if resp.status != 200:
                print(f"[workable-jobs] HTTP {resp.status}; stopping, finished pages are saved", flush=True)
                break
            data = resp.json()
            for job in data.get("jobs") or []:
                company = job.get("company") or {}
                if company.get("id") and company["id"] not in seen:
                    seen.add(company["id"])
                    employers.write(json.dumps({"company_id": company["id"], "name": company.get("title"), "website": company.get("website")}) + "\n")
            employers.flush()
            state.update(token=data.get("nextPageToken"), pages=state["pages"] + 1, companies=sorted(seen), done=not data.get("nextPageToken"))
            pages += 1
            if pages % TOKENS_EVERY_PAGES == 0 or state["done"]:
                state_path.write_text(json.dumps(state))
                print(f"[workable-jobs] page {state['pages']} of about {(data.get('totalSize') or 0) // 20}: {len(seen)} employers", flush=True)
    state_path.write_text(json.dumps(state))
    return {"pages_this_run": pages, "employers": len(seen), "finished": bool(state["done"])}


def slug_candidates(name: str, website: str | None) -> list[str]:
    ascii_name = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().casefold()
    hyphenated = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")
    found = [hyphenated, hyphenated.replace("-", "")]
    if website:
        host = re.sub(r"^(?:https?://)?(?:www\.)?", "", website.strip().casefold()).split("/")[0]
        found.append(host.split(".")[0])
    return [c for c in dict.fromkeys(found) if plausible_slug(c)]


def _same_name(a: str | None, b: str | None) -> bool:
    key = lambda text: re.sub(r"\W+", "", (text or "").casefold())
    return bool(key(a)) and key(a) == key(b)


def _account_name(client: HttpClient, code: str) -> str | None:
    for wait in MATCH_BACKOFF_SECONDS:
        try:
            resp = client.get(f"{ACCOUNT_API}/{quote(code, safe='')}", headers=JSON_ACCEPT)
        except RequestError:
            return None
        if resp.status == 200:
            try:
                return resp.json().get("name")
            except ValueError:
                return None
        if resp.status != 429:
            return None
        time.sleep(wait)
    return None


def match_employers(raw_dir: Path, client: HttpClient, known: set[str]) -> dict:
    """Find each employer's board. A slug counts only when Workable's own account of that name is the employer's name."""
    out = Path(raw_dir) / META.name
    done_path = out / "matched.jsonl"
    done = set()
    if done_path.exists():
        with open(done_path, encoding="utf-8") as fh:
            done = {json.loads(line)["company_id"] for line in fh}
    with open(out / "employers.jsonl", encoding="utf-8") as fh:
        employers = [json.loads(line) for line in fh]
    counts = {"employers": len(employers), "matched": 0, "known": 0, "unmatched": 0}
    with open(done_path, "a", encoding="utf-8") as log, open(_hits_path(raw_dir), "a", encoding="utf-8") as hits:
        for index, employer in enumerate(employers):
            if employer["company_id"] in done:
                continue
            candidates = slug_candidates(employer.get("name"), employer.get("website"))
            code = None
            if any(c in known for c in candidates):
                outcome = "known"
            else:
                code = next((c for c in candidates if _same_name(_account_name(client, c), employer.get("name"))), None)
                outcome = "matched" if code else "unmatched"
            counts[outcome] += 1
            if code:
                hits.write(json.dumps({"code": code, "evidence": f"{ACCOUNT_API}/{code}"}) + "\n")
                hits.flush()
            log.write(json.dumps({"company_id": employer["company_id"], "outcome": outcome, "code": code}) + "\n")
            log.flush()
            if (index + 1) % 1000 == 0:
                print(f"[workable-match] {index + 1}/{len(employers)}: {counts}", flush=True)
    rebuild_tokens(raw_dir)
    return counts


def match_until_walk_ends(raw_dir: Path, client: HttpClient, known: set[str]) -> dict:
    """Keep matching while the job walk adds employers, so boards appear as soon as they can."""
    state_path = Path(raw_dir) / META.name / "jobs_state.json"
    total = {"matched": 0, "known": 0, "unmatched": 0}
    while True:
        finished = state_path.exists() and json.loads(state_path.read_text()).get("done")
        result = match_employers(raw_dir, client, known)
        for key in total:
            total[key] += result[key]
        total["employers"] = result["employers"]
        if finished:
            return total
        time.sleep(MATCH_IDLE_SECONDS)


def common_crawl(raw_dir: Path) -> dict:
    """apply.workable.com URLs in every crawl since 2019, reusing the footers the main index harvest saved."""
    from company_dataset.sources import commoncrawl_indexer as cci

    footers = Path(raw_dir) / cci.META.name / "footers"
    footers.mkdir(parents=True, exist_ok=True)
    crawls = sorted({c for c, _ in cci.work_list([c["id"] for c in requests.get(cci.COLLECTIONS, timeout=60).json()], harvested=[])}, reverse=True)
    con = cci._connect()
    found = 0
    with open(_hits_path(raw_dir), "a", encoding="utf-8") as log:
        for crawl in crawls:
            try:
                files = cci.relevant_files(cci._footers(con, footers, crawl), CC_PREFIX)
                rows = con.execute("SELECT DISTINCT url FROM read_parquet(?) WHERE url_surtkey >= ? AND url_surtkey < ?", [files, CC_PREFIX, cci.prefix_end(CC_PREFIX)]).fetchall() if files else []
            except (duckdb.Error, requests.RequestException, OSError) as exc:
                print(f"[workable-cc] {crawl} failed: {type(exc).__name__}", flush=True)
                continue
            codes = {}
            for (url,) in rows:
                code = _code_from_link(url)
                if code:
                    codes.setdefault(code.casefold(), (code, url))
            for code, url in codes.values():
                log.write(json.dumps({"code": code, "evidence": url}) + "\n")
            log.flush()
            found += len(codes)
            rebuild_tokens(raw_dir)
            print(f"[workable-cc] {crawl}: {len(rows)} urls, {len(codes)} boards", flush=True)
    return {"crawls": len(crawls), "boards_seen": found}


def wayback(raw_dir: Path, client: HttpClient, sleep=time.sleep) -> dict:
    from company_dataset.sources import wayback_cdx as wb

    state_path = Path(raw_dir) / META.name / "wayback_state.json"
    next_page = json.loads(state_path.read_text())["next_page"] if state_path.exists() else 0
    pages = wb.page_count(client, WAYBACK_TARGET, sleep)
    with open(_hits_path(raw_dir), "a", encoding="utf-8") as log:
        for page in range(next_page, pages):
            codes = {}
            for line in wb._get(client, wb._query(WAYBACK_TARGET, page=page, fl="original"), sleep).splitlines():
                code = _code_from_link(line.strip())
                if code:
                    codes.setdefault(code.casefold(), (code, line.strip()))
            for code, url in codes.values():
                log.write(json.dumps({"code": code, "evidence": url}) + "\n")
            log.flush()
            state_path.write_text(json.dumps({"next_page": page + 1}))
            if page % 10 == 0:
                rebuild_tokens(raw_dir)
                print(f"[workable-wayback] page {page + 1}/{pages}", flush=True)
    rebuild_tokens(raw_dir)
    return {"pages": pages}


def parse(path: Path):
    return parse_tokens(path, META.name, allowed=ALLOWED)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="company_dataset.sources.workable_discovery")
    p.add_argument("mode", nargs="?", default="github", choices=["github", "jobs", "match", "cc", "wayback", "tokens"])
    p.add_argument("--raw", default="data/raw")
    p.add_argument("--db", default="data/store.db")
    a = p.parse_args(argv)
    raw = Path(a.raw)
    if a.mode == "github":
        out = harvest(raw, HttpClient(timeout=60, per_host_interval=0.1, max_attempts=2))
    elif a.mode == "jobs":
        out = walk_jobs(raw, HttpClient(timeout=45, per_host_interval=0.1, max_attempts=3))
    elif a.mode == "match":
        conn = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True, timeout=60)
        known = {code for (code,) in conn.execute("select code_key from boards where ats=?", (WORKABLE,))}
        conn.close()
        out = match_until_walk_ends(raw, HttpClient(timeout=30, per_host_interval=MATCH_INTERVAL_SECONDS, max_attempts=2), known)
    elif a.mode == "cc":
        out = common_crawl(raw)
    elif a.mode == "wayback":
        out = wayback(raw, HttpClient(timeout=180, max_attempts=1, per_host_interval=4.0))
    else:
        out = {"boards": rebuild_tokens(raw)}
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
