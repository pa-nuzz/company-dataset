"""Board identifiers from Common Crawl's columnar URL index, queried with DuckDB over HTTPS.

    python -m company_dataset.sources.commoncrawl_indexer --raw data/raw

Covers what sources/commoncrawl.py never harvested: crawls outside its list, and the EU Greenhouse
hosts on every crawl. The index holds the URLs a crawl captured, not the links on those pages, so
this finds boards the crawler visited. Row-group statistics pick the few files that can hold a
host; everything else is never read. A finished (crawl, prefix) is saved at once, so a stopped
harvest resumes.
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
from pathlib import Path

import duckdb
import requests

from company_dataset.models import SourceMeta
from company_dataset.sources._harvest import board_from_url, parse_tokens, write_tokens
from company_dataset.sources.commoncrawl import CRAWLS, PREFIXES

BASE = "https://data.commoncrawl.org/"
COLLECTIONS = "https://index.commoncrawl.org/collinfo.json"
EU_PREFIXES = ["io,greenhouse,eu,boards)/", "io,greenhouse,eu,job-boards)/"]
OLDEST_CRAWL_YEAR = 2019  # the target ATS hosts barely appear before this
DUCKDB_THREADS = 4

META = SourceMeta(
    name="commoncrawl_indexer_2026_11_03",
    description="Common Crawl columnar URL index: board URLs in crawls and hosts the earlier harvests did not cover",
    download_url="https://data.commoncrawl.org/cc-index/table/cc-main/warc/",
    filename="tokens.csv",
    license="Common Crawl Terms of Use",
    license_url="https://commoncrawl.org/terms-of-use/",
    license_note="Only URLs (factual board identifiers) are read from the index; every board is verified against its provider before use. No page content is used.",
    attribution="Common Crawl Foundation, columnar URL index",
    downloadable=False,
)


def prefix_end(prefix: str) -> str:
    """The smallest string after every string starting with prefix."""
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


def _year(crawl: str) -> int:
    m = re.match(r"CC-MAIN-(\d{4})-\d+$", crawl)
    return int(m.group(1)) if m else 0


def work_list(all_crawls: list[str], harvested: list[str] = CRAWLS, oldest_year: int = OLDEST_CRAWL_YEAR) -> list[tuple[str, str]]:
    """(crawl, prefix) pairs, newest crawls first: everything for an unharvested crawl, only the EU hosts for a harvested one."""
    pairs = []
    for crawl in sorted((c for c in all_crawls if _year(c) >= oldest_year), key=lambda c: (_year(c), c), reverse=True):
        pairs += [(crawl, p) for p in (EU_PREFIXES if crawl in harvested else PREFIXES + EU_PREFIXES)]
    return pairs


def relevant_files(footers: list[tuple[str, str, str]], prefix: str) -> list[str]:
    """Files holding a row group whose url_surtkey range overlaps the prefix."""
    end = prefix_end(prefix)
    return sorted({f for f, low, high in footers if low is not None and high is not None and low < end and high >= prefix})


def _connect():
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(f"SET threads={DUCKDB_THREADS}; SET http_timeout=120000; SET http_retries=5; SET http_retry_wait_ms=2000;")
    return con


def _footers(con, cache: Path, crawl: str) -> list[tuple[str, str, str]]:
    """Each file's url_surtkey range per row group, kept on disk: it takes about a minute per crawl to read."""
    path = cache / f"{crawl}.json"
    if path.exists():
        return [tuple(x) for x in json.loads(path.read_text())]
    listing = requests.get(f"{BASE}crawl-data/{crawl}/cc-index-table.paths.gz", timeout=60)
    listing.raise_for_status()
    files = [BASE + line for line in gzip.decompress(listing.content).decode().split() if "subset=warc" in line]
    rows = con.execute("SELECT file_name, stats_min_value, stats_max_value FROM parquet_metadata(?) WHERE path_in_schema='url_surtkey'", [files]).fetchall()
    path.write_text(json.dumps(rows))
    return rows


def harvest(raw_dir: Path) -> dict:
    out = Path(raw_dir) / META.name
    footers_dir = out / "footers"
    footers_dir.mkdir(parents=True, exist_ok=True)
    log = out / "done.jsonl"
    done, tokens = set(), {}
    if log.exists():
        with open(log, encoding="utf-8") as fh:
            for line in fh:
                entry = json.loads(line)
                done.add((entry["crawl"], entry["prefix"]))
                for ats, code, region, url in entry["tokens"]:
                    tokens.setdefault((ats, code.casefold()), (code, region, url))
    crawls = [c["id"] for c in requests.get(COLLECTIONS, timeout=60).json()]
    pairs = work_list(crawls)
    con = _connect()
    failures = []
    with open(log, "a", encoding="utf-8") as fh:
        for crawl, prefix in pairs:
            if (crawl, prefix) in done:
                continue
            try:
                files = relevant_files(_footers(con, footers_dir, crawl), prefix)
                urls = con.execute("SELECT DISTINCT url FROM read_parquet(?) WHERE url_surtkey >= ? AND url_surtkey < ?", [files, prefix, prefix_end(prefix)]).fetchall() if files else []
            except (duckdb.Error, requests.RequestException, OSError) as exc:
                failures.append(f"{crawl}|{prefix}: {str(exc)[:200]}")
                print(f"[cc-index] FAILED {failures[-1]}", flush=True)
                continue
            seen = {}
            for (url,) in urls:
                board = board_from_url(url)
                if board:
                    seen.setdefault((board[0], board[1].casefold()), (board[0], board[1], board[2], url))
            fh.write(json.dumps({"crawl": crawl, "prefix": prefix, "tokens": list(seen.values())}) + "\n")
            fh.flush()
            for key, (ats, code, region, url) in seen.items():
                tokens.setdefault(key, (code, region, url))
            print(f"[cc-index] {crawl} {prefix}: {len(urls)} urls, {len(seen)} boards, {len(tokens)} total", flush=True)
    if not failures:
        write_tokens(out / META.filename, tokens)
    return {"pairs": len(pairs), "boards": len(tokens), "failures": failures, "complete": not failures}


def parse(path: Path):
    return parse_tokens(path, META.name)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="company_dataset.sources.commoncrawl_indexer")
    p.add_argument("--raw", default="data/raw")
    a = p.parse_args(argv)
    print(json.dumps(harvest(Path(a.raw)), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
