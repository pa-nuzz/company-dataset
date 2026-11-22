"""Board identifiers from web search: boards on the ATS hosts, and company pages that embed one.

    SERPAPI_KEY=... python -m company_dataset.sources.search_dorks --raw data/raw --max-queries 100

Queries cost money, so --max-queries is required and nothing runs without a key. Backends: SerpAPI, Brave
(BRAVE_API_KEY), or Google Custom Search for an existing customer (GOOGLE_API_KEY + GOOGLE_CSE_ID); that API is closed to
new customers. Bing's search API was retired in 2025. Result pages are never scraped directly.
A result on a company's own site is fetched once, to read which board it links to.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlencode

from company_dataset.enrichment import fetch_page
from company_dataset.http import HttpClient, RequestError
from company_dataset.models import SourceMeta
from company_dataset.normalize import CORE_ATS
from company_dataset.sources._harvest import board_from_url, parse_tokens, write_tokens

SITE_QUERIES = ["site:boards.greenhouse.io", "site:job-boards.greenhouse.io", "site:jobs.lever.co", "site:jobs.ashbyhq.com", "site:jobs.smartrecruiters.com", "site:*.breezy.hr/p/"]
EMBED_QUERIES = ['intext:"Powered by Greenhouse" -site:greenhouse.io', 'intext:"Powered by Lever" -site:lever.co', 'intext:"Powered by Ashby" -site:ashbyhq.com',
                 'intext:"Powered by SmartRecruiters" -site:smartrecruiters.com', 'intext:"Powered by Breezy" -site:breezy.hr']
INDUSTRIES = ["fintech", "healthcare", "biotech", "cybersecurity", "robotics", "climate", "logistics", "insurance", "education", "gaming", "retail", "manufacturing", "legal", "real estate", "media", "energy", "aerospace", "hospitality", "nonprofit", "government"]
STACKS = ["python", "kotlin", "golang", "rust", "kubernetes", "react", "machine learning", "data engineering"]
RESULTS_PER_PAGE = 100
GOOGLE_PAGE_SIZE = 10
BRAVE_PAGE_SIZE = 20

META = SourceMeta(
    name="search_dorks",
    description="Board URLs from web search queries on ATS hosts and pages embedding an ATS board",
    download_url="https://serpapi.com/",
    filename="tokens.csv",
    license="Search result URLs (factual identifiers); subject to the search provider's terms",
    license_url="https://serpapi.com/terms",
    license_note="Only result URLs are used; every board is verified against its provider before use.",
    attribution="Web search via SerpAPI or Google Custom Search",
    downloadable=False,
)


class NoSearchBackend(Exception):
    pass


def build_queries(yc_terms: list[str] | tuple = ()) -> list[str]:
    """Direct queries first, then one per host for each industry, stack and Y Combinator term."""
    sites = [q for q in SITE_QUERIES if "breezy" not in q] + ["site:breezy.hr"]
    terms = [f'"{t}"' for t in (*INDUSTRIES, *STACKS, *yc_terms)]
    return list(dict.fromkeys(SITE_QUERIES + EMBED_QUERIES + [f"{site} {term}" for term in terms for site in sites]))


def backend_from_env(env=os.environ):
    """(name, function(query, start) -> (url, is_last_page)); keys stay out of everything written or printed."""
    if env.get("SERPAPI_KEY") or env.get("SERPAPI_API_KEY"):
        key = env.get("SERPAPI_KEY") or env.get("SERPAPI_API_KEY")
        return "serpapi", lambda q, start: "https://serpapi.com/search.json?" + urlencode({"engine": "google", "q": q, "num": RESULTS_PER_PAGE, "start": start, "api_key": key})
    if env.get("BRAVE_API_KEY"):
        return "brave", lambda q, start: "https://api.search.brave.com/res/v1/web/search?" + urlencode({"q": q, "count": BRAVE_PAGE_SIZE, "offset": start // BRAVE_PAGE_SIZE})
    if env.get("GOOGLE_API_KEY") and env.get("GOOGLE_CSE_ID"):
        key, cx = env["GOOGLE_API_KEY"], env["GOOGLE_CSE_ID"]
        return "google_cse", lambda q, start: "https://www.googleapis.com/customsearch/v1?" + urlencode({"key": key, "cx": cx, "q": q, "num": GOOGLE_PAGE_SIZE, "start": start + 1})
    raise NoSearchBackend("set SERPAPI_KEY, BRAVE_API_KEY, or GOOGLE_API_KEY and GOOGLE_CSE_ID: no search was run")


def request_headers(name: str, env=os.environ) -> dict | None:
    return {"X-Subscription-Token": env["BRAVE_API_KEY"], "Accept": "application/json"} if name == "brave" else None


def result_links(payload: dict) -> list[str]:
    links = [r.get("link") for r in payload.get("organic_results") or []] + [r.get("link") for r in payload.get("items") or []] + [r.get("url") for r in (payload.get("web") or {}).get("results") or []]
    return [u for u in links if u]


def boards_from_links(links: list[str], client: HttpClient, fetched: set[str]) -> dict[tuple[str, str], tuple[str, str, str]]:
    found: dict[tuple[str, str], tuple[str, str, str]] = {}
    for url in links:
        board = board_from_url(url)
        if board:
            found.setdefault((board[0], board[1].casefold()), (board[1], board[2], url))
            continue
        if url in fetched:
            continue
        fetched.add(url)
        page = fetch_page(client, url)
        final = board_from_url(page.get("final_url") or "")
        if final:
            found.setdefault((final[0], final[1].casefold()), (final[1], final[2], url))
        for ref in page.get("board_refs") or []:
            ats, _, code = ref.partition(":")
            if ats in CORE_ATS and code:
                found.setdefault((ats, code.casefold()), (code, "", url))
    return found


def harvest(raw_dir: Path, client: HttpClient, max_queries: int, env=os.environ, pages_per_query: int = 1) -> dict:
    name, build_url = backend_from_env(env)
    out = Path(raw_dir) / META.name
    out.mkdir(parents=True, exist_ok=True)
    log = out / "queries.jsonl"
    done, tokens = set(), {}
    if log.exists():
        with open(log, encoding="utf-8") as fh:
            for line in fh:
                entry = json.loads(line)
                done.add(entry["query"])
                for ats, code, region, url in entry["tokens"]:
                    tokens.setdefault((ats, code.casefold()), (code, region, url))
    spent, failed = 0, 0
    fetched: set[str] = set()
    with open(log, "a", encoding="utf-8") as fh:
        for query in build_queries():
            if query in done:
                continue
            if spent >= max_queries:
                break
            found: dict = {}
            for page in range(pages_per_query):
                if spent >= max_queries:
                    break
                spent += 1
                size = {"serpapi": RESULTS_PER_PAGE, "brave": BRAVE_PAGE_SIZE}.get(name, GOOGLE_PAGE_SIZE)
                try:
                    resp = client.get(build_url(query, page * size), headers=request_headers(name, env))
                except RequestError:
                    failed += 1  # the message would contain the URL, and with it the key
                    continue
                if resp.status != 200:
                    failed += 1
                    print(f"[dorks] HTTP {resp.status} for {query}", flush=True)
                    continue
                links = result_links(resp.json())
                found.update(boards_from_links(links, client, fetched))
                if not links:
                    break
            fh.write(json.dumps({"query": query, "tokens": [[k[0], v[0], v[1], v[2]] for k, v in found.items()]}) + "\n")
            fh.flush()
            for key, value in found.items():
                tokens.setdefault(key, value)
            print(f"[dorks] {query}: {len(found)} boards, {len(tokens)} total", flush=True)
    write_tokens(out / META.filename, tokens)
    return {"backend": name, "api_requests": spent, "failed_requests": failed, "boards": len(tokens)}


def parse(path: Path):
    return parse_tokens(path, META.name)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="company_dataset.sources.search_dorks")
    p.add_argument("--raw", default="data/raw")
    p.add_argument("--max-queries", type=int, required=True, help="API requests to spend; each one may cost money")
    a = p.parse_args(argv)
    try:
        print(json.dumps(harvest(Path(a.raw), HttpClient(timeout=60, per_host_interval=1.0, max_attempts=2), a.max_queries), indent=1))
    except NoSearchBackend as exc:
        print(f"[dorks] {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
