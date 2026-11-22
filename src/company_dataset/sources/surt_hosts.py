#!/usr/bin/env python3
"""SURT cross-reference: company names -> candidate registrable hosts via CC columnar index.

For each company without a website, generate SURT prefixes from name tokens,
query the CC columnar index for URLs under those prefixes, and extract
candidate registrable domains that might be the company's homepage.

Run label: surt_hosts_2026-11-05
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

import duckdb

from company_dataset.models import SourceMeta
from company_dataset.normalize import clean_website
from company_dataset.identity import registrable_domain, _fold
from company_dataset.sources._harvest import write_tokens

META = SourceMeta(
    name="surt_hosts_2026_11_05",
    description="SURT cross-reference from CC columnar index: candidate hosts for companies without websites",
    download_url="https://data.commoncrawl.org/cc-index/table/cc-main/warc/",
    filename="tokens.csv",
    license="Common Crawl Terms of Use",
    license_url="https://commoncrawl.org/terms-of-use/",
    license_note="Only URLs (factual board identifiers) are read from the index; every host is verified before use.",
    attribution="Common Crawl Foundation, columnar URL index",
    downloadable=False,
)


# Company name stopwords that don't help form SURT prefixes
_STOPWORDS = frozenset(
    "inc incorporated llc llp lp ltd limited corp corporation co company plc pbc gmbh ag ab as asa oy oyj bv nv sa sas sarl srl spa kg ug se pty pte the and of for in on at to by with from into during including until against among throughout despite".split()
)


def _tokens(name: str) -> list[str]:
    """Extract meaningful tokens from a company name."""
    return [
        t for t in re.findall(r"[a-z0-9]+", (name or "").lower().replace("&", "and").replace("+", "plus"))
        if t not in _STOPWORDS and len(t) >= 3
    ]


def _surt_prefix(domain: str) -> str:
    """Convert a registrable domain to SURT prefix (e.g., example.com -> com,example)."""
    parts = domain.split(".")
    if len(parts) < 2:
        return ""
    return ",".join(reversed(parts))


def _surt_range(prefix: str) -> tuple[str, str]:
    """Get the SURT range for a prefix (inclusive start, exclusive end)."""
    return prefix, prefix + "\uffff"


def _load_company_names(db_path: str) -> list[tuple[int, str]]:
    """Load incomplete companies without websites: (company_id, name)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT c.id, c.company_name
        FROM companies c
        WHERE c.status = 'incomplete'
          AND (c.website_url = '' OR c.website_url IS NULL)
          AND c.company_name IS NOT NULL
          AND c.company_name != ''
    """).fetchall()
    conn.close()
    return [(r["id"], r["company_name"]) for r in rows]


def _candidate_prefixes(name: str, max_prefixes: int = 3) -> list[str]:
    """Generate candidate SURT prefixes from a company name."""
    tokens = _tokens(name)
    if not tokens:
        return []

    prefixes = set()

    # Single token prefixes - only com and io, limit to top 3 tokens
    for t in tokens[:3]:
        if len(t) >= 4:
            prefixes.add(f"com,{t}")
            prefixes.add(f"io,{t}")

    return sorted(prefixes)[:3]


def _connect_duckdb() -> duckdb.DuckDBPyConnection:
    """Create DuckDB connection with httpfs for remote parquet access."""
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("SET threads=4; SET http_timeout=120000; SET http_retries=5; SET http_retry_wait_ms=2000;")
    return con


def _load_footers(cc_index_dir: Path, crawl: str) -> list[tuple[str, str, str]]:
    """Load footer metadata for a crawl from local cache.
    Returns list of (file_url, min_surtkey, max_surtkey).
    """
    path = cc_index_dir / "footers" / f"{crawl}.json"
    if path.exists():
        data = json.loads(path.read_text())
        return [(row[0], row[1], row[2]) for row in data]
    return []


def _surt_prefix(domain: str) -> str:
    """Convert a registrable domain to SURT prefix (e.g., example.com -> com,example)."""
    parts = domain.split(".")
    if len(parts) < 2:
        return ""
    return ",".join(reversed(parts))


def _surt_range(prefix: str) -> tuple[str, str]:
    """Get the SURT range for a prefix (inclusive start, exclusive end)."""
    return prefix, prefix + "\uffff"


def _load_footers(cc_index_dir: Path, crawl: str) -> list[tuple[str, str, str]]:
    """Load footer metadata for a crawl from local cache.
    Returns list of (file_url, min_surtkey, max_surtkey).
    """
    path = cc_index_dir / "footers" / f"{crawl}.json"
    if path.exists():
        data = json.loads(path.read_text())
        return [(row[0], row[1], row[2]) for row in data]
    return []


def _relevant_files(footers: list[tuple[str, str, str]], prefix: str) -> list[str]:
    """Select parquet files whose surtkey range overlaps the prefix."""
    start, end = _surt_range(prefix)
    return [f for f, min_k, max_k in footers if max_k is not None and min_k is not None and max_k >= start and min_k < end]


def _query_cc_index(cc_index_dir: Path, prefixes: list[str]) -> set[str]:
    """Query the CC columnar index for hosts under given SURT prefixes."""
    if not prefixes:
        return set()

    hosts = set()

    # Build DuckDB connection with httpfs
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("SET threads=4; SET http_timeout=30000; SET http_retries=2; SET http_retry_wait_ms=1000;")

    # Get available crawls from footers directory - only use most recent 3 crawls
    footers_dir = cc_index_dir / "footers"
    all_crawls = sorted([p.stem for p in footers_dir.glob("*.json")])
    # Only use the most recent 3 crawls (newest first)
    crawls = sorted([p.stem for p in footers_dir.glob("*.json")], reverse=True)[:3]

    for prefix in prefixes:
        # Load footers for recent crawls and collect relevant parquet files
        all_files = []
        for crawl in crawls:
            footers = _load_footers(cc_index_dir, crawl)
            files = _relevant_files(footers, prefix)
            if files:
                all_files.extend(files)

        if not all_files:
            continue

        # Query for distinct registrable domains (hosts) under this prefix
        start, end = _surt_range(prefix)
        query = f"""
            SELECT DISTINCT url_host_name
            FROM read_parquet({all_files})
            WHERE url_host_name IS NOT NULL
              AND url_surtkey >= ?
              AND url_surtkey < ?
        """
        try:
            for row in con.execute(query, [start, end]).fetchall():
                host = row[0]
                if host:
                    rd = registrable_domain(host)
                    if rd:
                        hosts.add(rd)
        except Exception as e:
            print(f"  [surt_hosts] query error for prefix {prefix}: {e}")
            continue

    con.close()
    return hosts


def harvest(db_path: str, raw_dir: Path, client, workers: int = 1, sample: int = 0) -> dict:
    """Main harvest function: find candidate hosts for companies without websites.
    If sample > 0, only process that many companies (randomly selected)."""
    out_dir = Path(raw_dir) / META.name
    out_dir.mkdir(parents=True, exist_ok=True)

    cc_index_dir = Path(raw_dir) / "commoncrawl_indexer_2026_11_03"
    if not cc_index_dir.exists():
        return {"error": "CC index directory not found"}

    companies = _load_company_names(db_path)
    print(f"[surt_hosts] {len(companies)} companies without websites")

    # Sample if requested
    if sample > 0 and len(companies) > sample:
        companies = random.sample(companies, sample)
        print(f"[surt_hosts] Sampling {sample} companies")

    # Build candidate prefixes for all companies
    company_prefixes = {}
    for cid, name in companies:
        prefixes = _candidate_prefixes(name)
        if prefixes:
            company_prefixes[cid] = (name, prefixes)

    print(f"[surt_hosts] {len(company_prefixes)} companies with candidate prefixes")

    # Aggregate all unique prefixes to query once
    all_prefixes = set()
    for _, prefixes in company_prefixes.values():
        all_prefixes.update(prefixes)

    print(f"[surt_hosts] querying CC index for {len(all_prefixes)} unique prefixes...")
    all_hosts = _query_cc_index(cc_index_dir, sorted(all_prefixes))
    print(f"[surt_hosts] found {len(all_hosts)} unique candidate hosts from CC index")

    # Now match hosts back to companies
    results = defaultdict(list)
    for cid, (name, prefixes) in company_prefixes.items():
        tokens = [t for t in re.findall(r"[a-z0-9]+", _fold(name).lower()) if len(t) >= 3]
        for host in all_hosts:
            host_tokens = [t for t in re.findall(r"[a-z0-9]+", _fold(host).lower()) if len(t) >= 3]
            # Check if any name token appears in host
            if any(t in host_tokens for t in tokens if len(t) >= 4):
                results[cid].append(host)

    # Deduplicate
    for cid in results:
        results[cid] = list(set(results[cid]))

    # Write tokens.csv format: ats,code,region,evidence
    # We use a pseudo-ATS "surt_host" and the company ID as code
    tokens = {}
    for cid, hosts in results.items():
        if hosts:
            for host in hosts[:5]:  # limit to 5 per company
                key = ("surt_host", str(cid).casefold())
                tokens[key] = (str(cid), "", f"cc_index:{host}")

    out_dir = Path("data/raw") / META.name
    out_dir.mkdir(parents=True, exist_ok=True)
    write_tokens(out_dir, tokens)

    return {
        "companies_processed": len(company_prefixes),
        "companies_with_prefixes": len(company_prefixes),
        "unique_prefixes_queried": len(all_prefixes),
        "candidate_hosts_found": len(all_hosts),
        "companies_with_candidates": len(results),
        "tokens_written": len(tokens),
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="company_dataset.sources.surt_hosts")
    p.add_argument("--db", required=True)
    p.add_argument("--raw", default="data/raw")
    p.add_argument("--sample", type=int, default=0, help="Sample N companies (0 = all)")
    a = p.parse_args(argv)

    # This source doesn't need an HTTP client
    class DummyClient:
        pass

    result = harvest(a.db, Path(a.raw), DummyClient(), sample=a.sample)
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())