"""Board identifiers from companies' own sites: a careers page that links to or embeds its ATS board.

    python -m company_dataset.sources.company_sites --raw data/raw --limit 30000

This reads what a company publishes about itself, so nothing is guessed, and it reaches companies no
list mentions. Starts from the Y Combinator directory, then large companies in BigPicture. Each site
gets its robots.txt read first and at most three page requests. A batch is saved when it finishes.
"""
from __future__ import annotations

import argparse
import json
import random
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from company_dataset.http import HttpClient, RequestError
from company_dataset.models import SourceMeta
from company_dataset.normalize import clean_website
from company_dataset.sources._harvest import boards_in_text, parse_tokens, write_tokens
from company_dataset.sources.slug_prober import large_companies
from company_dataset.sources.yc_slugs import DIRECTORY, companies as yc_companies

PATHS = ("/careers", "/jobs", "")
USER_AGENT = "company-dataset/1.0"
BATCH = 500
WORKERS = 24
MAX_PAGE_CHARS = 3_000_000
BROWSER_ACCEPT = {"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"}

META = SourceMeta(
    name="company_sites",
    description="ATS board links and embeds on companies' own websites",
    download_url="https://yc-oss.github.io/api/companies/all.json",
    filename="tokens.csv",
    license="Public web pages; only URLs are kept",
    license_url="",
    license_note="Only pages robots.txt allows are read, and only URLs (factual board identifiers) kept; every board is verified against its provider before use.",
    attribution="Company websites, from the Y Combinator directory and BigPicture",
    downloadable=False,
)


def _text(client: HttpClient, url: str) -> str | None:
    try:
        resp = client.get(url, headers=BROWSER_ACCEPT)
    except RequestError:
        return None
    return resp.text[:MAX_PAGE_CHARS] if resp.status == 200 else None


def boards_on_site(client: HttpClient, site: str) -> dict:
    """The first page with a board answers: a careers page is more specific than a homepage."""
    robots = RobotFileParser()
    robots.parse((_text(client, site + "/robots.txt") or "").splitlines())
    for path in PATHS:
        url = site + path
        if not robots.can_fetch(USER_AGENT, url):
            continue
        body = _text(client, url)
        if body:
            found = boards_in_text(body.replace("\\/", "/").replace("\\u002F", "/"), url)
            if found:
                return found
    return {}


def plan_sites(companies: list[tuple[str, str]], seed: int = 20261103) -> list[str]:
    """Each company's site once, with listed order kept per group by the caller; invalid hosts dropped."""
    sites: dict[str, None] = {}
    for _, website in companies:
        site = clean_website(website)
        if site:
            sites.setdefault(f"https://{urlsplit(site).hostname}")
    return list(sites)


def harvest(raw_dir: Path, sites: list[str], client: HttpClient, workers: int = WORKERS) -> dict:
    out = Path(raw_dir) / META.name
    out.mkdir(parents=True, exist_ok=True)
    log = out / "batches.jsonl"
    done, tokens = set(), {}
    if log.exists():
        with open(log, encoding="utf-8") as fh:
            for line in fh:
                entry = json.loads(line)
                done.update(entry["sites"])
                for ats, code, region, evidence in entry["tokens"]:
                    tokens.setdefault((ats, code.casefold()), (code, region, evidence))
    todo = [s for s in sites if s not in done]
    with_board = 0
    with open(log, "a", encoding="utf-8") as fh:
        for start in range(0, len(todo), BATCH):
            batch = todo[start:start + BATCH]
            with ThreadPoolExecutor(max_workers=workers) as ex:
                results = list(ex.map(lambda s: boards_on_site(client, s), batch))
            found: dict = {}
            for result in results:
                with_board += bool(result)
                found.update(result)
            fh.write(json.dumps({"sites": batch, "tokens": [[k[0], v[0], v[1], v[2]] for k, v in found.items()]}) + "\n")
            fh.flush()
            for key, value in found.items():
                tokens.setdefault(key, value)
            write_tokens(out / META.filename, tokens)
            print(f"[sites] {start + len(batch)}/{len(todo)}: {with_board} sites with a board, {len(tokens)} boards", flush=True)
    write_tokens(out / META.filename, tokens)
    return {"sites_checked": len(done) + len(todo), "boards": len(tokens)}


def parse(path: Path):
    return parse_tokens(path, META.name)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="company_dataset.sources.company_sites")
    p.add_argument("--raw", default="data/raw")
    p.add_argument("--limit", type=int, required=True, help="sites to check, Y Combinator first")
    p.add_argument("--seed", type=int, default=20261103)
    a = p.parse_args(argv)
    client = HttpClient(timeout=15, max_attempts=1, per_host_interval=0.5)
    yc = yc_companies(json.loads(client.get(DIRECTORY).text))
    large = large_companies(Path(a.raw))
    random.Random(a.seed).shuffle(large)
    print(json.dumps(harvest(Path(a.raw), plan_sites(yc + large)[:a.limit], client), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
