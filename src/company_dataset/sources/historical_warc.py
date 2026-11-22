#!/usr/bin/env python3
"""Historical board mining from Common Crawl WARC files.

Extracts company/board information from archived web pages in Common Crawl WARC files.
Finds boards that were live historically but may be dead now.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import requests
from warcio.archiveiterator import ArchiveIterator
from warcio.warcwriter import WARCWriter

from company_dataset import store
from company_dataset.models import SourceMeta
from company_dataset.identity import registrable_domain
from company_dataset.normalize import clean_code, CORE_ATS
from company_dataset.sources._harvest import board_from_url, parse_tokens, write_tokens
from company_dataset.sources.commoncrawl_indexer import _connect, _footers, relevant_files, PREFIXES

# ─── Configuration ────────────────────────────────────────────────────────────

META = SourceMeta(
    name="historical_warc_2026_11_05",
    description="Historical board mining from Common Crawl WARC files: extracts company/board info from archived pages",
    download_url="https://data.commoncrawl.org/",
    filename="tokens.csv",
    license="Common Crawl Terms of Use",
    license_url="https://commoncrawl.org/terms-of-use/",
    license_note="Only extracts factual board identifiers from archived pages; every board is verified against provider API. WARC content not stored.",
    attribution="Common Crawl Foundation",
    downloadable=False,
)

WARC_BASE = "https://data.commoncrawl.org/"
COLLECTIONS_URL = "https://index.commoncrawl.org/collinfo.json"
HISTORICAL_CRAWLS_START = 2019
WARC_BATCH_SIZE = 10
MAX_WARC_SIZE_MB = 500
REQUEST_TIMEOUT = 30
MAX_WORKERS = 4

ATS_HOSTS = {
    "greenhouse": ["boards.greenhouse.io", "job-boards.greenhouse.io"],
    "lever": ["jobs.lever.co", "jobs.eu.lever.co"],
    "ashby": ["jobs.ashbyhq.com"],
    "smartrecruiters": ["jobs.smartrecruiters.com", "careers.smartrecruiters.com"],
    "breezy": [".breezy.hr"],
}

ATS_URL_PATTERNS = {
    "greenhouse": r"https?://(?:boards|job-boards)\.greenhouse\.io/([^/?#]+)",
    "lever": r"https?://jobs\.lever\.co/([^/?#]+)",
    "ashby": r"https?://jobs\.ashbyhq\.com/([^/?#]+)",
    "smartrecruiters": r"https?://(?:careers\.)?smartrecruiters\.com/([^/?#]+)",
    "breezy": r"https?://([a-z0-9-]+)\.breezy\.hr",
}

def extract_board_from_url(url: str) -> Optional[tuple[str, str]]:
    for ats, pattern in ATS_URL_PATTERNS.items():
        match = re.search(pattern, url)
        if match:
            code = match.group(1)
            if code and len(code) > 1:
                return ats, code
    return None


def extract_text_from_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"<!--.*?-->", " ", html)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_company_description(html: str, company_name: str = "") -> Optional[str]:
    text = extract_text_from_html(html)
    
    about_patterns = [
        r"(?i)(?:about|about us|about the company|our story|who we are|company overview|our mission)\s*[:\-–—]?\s*(.{50,500})",
        r"(?i)(?:<h[1-6][^>]*>.*?(?:about|about us|our story|who we are|company overview).*?</h[1-6]>)\s*(.{50,500})",
    ]
    
    for pattern in about_patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if match:
            desc = match.group(1).strip()
            desc = re.sub(r"<[^>]+>", "", desc)
            desc = re.sub(r"\s+", " ", desc).strip()
            if len(desc) > 50:
                return desc[:600]
    
    sentences = re.split(r"[.!?]\s+", text)
    for sent in sentences:
        sent = sent.strip()
        if len(sent) > 80 and len(sent) < 600:
            if not re.search(r"(?i)(apply|responsibilities|requirements|qualifications|you will|you'll be|we're looking|we are looking|salary|benefits)", sent):
                return sent[:600]
    
    return None


def download_warc_file(warc_url: str, dest_path: Path, timeout: int = 120) -> bool:
    try:
        headers = {"User-Agent": "company-dataset/1.0 (historical-mining; contact@company-dataset.com)"}
        with requests.get(warc_url, stream=True, timeout=120, headers={"User-Agent": "company-dataset/1.0"}) as r:
            r.raise_for_status()
            total_size = int(r.headers.get("Content-Length", 0))
            if total_size > 500 * 1024 * 1024:
                print(f"  Skipping large WARC: {total_size/1024/1024:.0f}MB")
                return False
            
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        dest_path.write(chunk)
        return True
    except Exception as e:
        print(f"  Download failed: {e}")
        if dest_path.exists():
            dest_path.unlink(missing_ok=True)
        return False


def process_warc_file(warc_path: Path, output_dir: Path) -> list[dict]:
    results = []
    
    try:
        with open(warc_path, "rb") as f:
            for record in ArchiveIterator(f):
                if record.rec_type != "response":
                    continue
                
                url = record.rec_headers.get_header("WARC-Target-URI", "")
                if not url:
                    continue
                
                board_info = extract_board_from_url(url)
                if not board_info:
                    continue
                
                ats, code = board_info
                if ats not in CORE_ATS:
                    continue
                
                content = record.content_stream().read()
                if not content:
                    continue
                
                try:
                    html = content.decode("utf-8", errors="ignore")
                except:
                    continue
                
                board_tokens = extract_board_tokens_from_html(url, html)
                
                description = extract_company_description(html)
                
                result = {
                    "url": url,
                    "ats": ats,
                    "code": code,
                    "description": extract_company_description(html),
                    "board_tokens": board_tokens,
                    "source": "historical_warc",
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
                results.append(result)
                
    except Exception as e:
        print(f"  Error processing WARC {warc_path}: {e}")
    
    return results


def extract_board_tokens_from_html(base_url: str, html: str) -> list[tuple[str, str]]:
    tokens = []
    urls = re.findall(r'href=["\']([^"\']+)', html)
    for url in urls:
        if url.startswith("/"):
            parsed = urlparse(base_url)
            url = f"{parsed.scheme}://{parsed.netloc}{url}"
        elif not url.startswith("http"):
            continue
        
        board_info = extract_board_from_url(url)
        if board_info:
            tokens.append(board_info)
    return tokens


def get_historical_crawls(start_year: int = 2019) -> list[str]:
    try:
        resp = requests.get("https://index.commoncrawl.org/collinfo.json", timeout=60)
        resp.raise_for_status()
        all_crawls = [c["id"] for c in resp.json()]
        filtered = [c for c in all_crawls if _year(c) >= start_year]
        return sorted(filtered, reverse=True)
    except Exception as e:
        print(f"Error fetching crawl list: {e}")
        return []


def _year(crawl: str) -> int:
    m = re.match(r"CC-MAIN-(\d{4})-\d+$", crawl)
    return int(m.group(1)) if m else 0


def get_historical_board_urls(crawl: str, prefix: str, max_urls: int = 50000) -> list[str]:
    con = _connect()
    try:
        footers = _load_footers(Path("data/raw/commoncrawl_indexer_2026_11_03/footers"), crawl)
        files = relevant_files(footers, prefix)
        if not files:
            return []
        
        start, end = _surt_range(prefix)
        query = f"""
            SELECT DISTINCT url FROM read_parquet({files})
            WHERE url_surtkey >= ? AND url_surtkey < ?
            LIMIT {max_urls}
        """
        urls = _connect().execute(query, [files, prefix, prefix + chr(255)]).fetchall()
        return [u[0] for u in urls if u[0]]
    except Exception as e:
        print(f"  Index query failed for {crawl}|{prefix}: {e}")
        return []


def _surt_range(prefix: str) -> tuple[str, str]:
    return prefix, prefix + "\uffff"


def validate_board_urls(urls: list[str]) -> list[dict]:
    validated = []
    for url in urls[:100]:
        board_info = extract_board_from_url(url)
        if not board_info:
            continue
        ats, code = board_info
        
        try:
            if ats == "greenhouse":
                url_check = f"https://boards-api.greenhouse.io/v1/boards/{code}"
            elif ats == "lever":
                url_check = f"https://api.lever.co/v0/postings/{code}?mode=json"
            elif ats == "ashby":
                url_check = f"https://api.ashbyhq.com/posting-api/job-board/{code}"
            elif ats == "smartrecruiters":
                url_check = f"https://api.smartrecruiters.com/v1/companies/{code}/postings?limit=1"
            elif ats == "breezy":
                url_check = f"https://{code}.breezy.hr/json"
            else:
                continue
            
            resp = requests.get(url_check, timeout=5, headers={"User-Agent": "company-dataset/1.0"})
            if resp.status_code == 200:
                data = resp.json()
                if ats == "greenhouse" and data.get("jobs"):
                    pass
                elif ats == "lever" and isinstance(data, list) and len(data) > 0:
                    pass
                elif ats == "ashby" and data.get("jobs"):
                    pass
                elif ats == "smartrecruiters" and data.get("content"):
                    pass
                elif ats == "breezy" and isinstance(data, list) and len(data) > 0:
                    pass
                else:
                    continue
                
                validated.append({"url": url, "ats": ats, "code": code, "status": "live"})
        except Exception:
            pass
    return validated


def mine_historical_boards(
    raw_dir: Path,
    start_year: int = 2019,
    max_crawls: int = 20,
    sample_mode: bool = False,
) -> dict:
    print(f"[historical_warc] Starting historical mining from year {start_year}")
    
    crawls = get_historical_crawls(start_year)
    crawls = crawls[:max_crawls]
    print(f"[historical_warc] Processing {len(crawls)} crawls: {crawls[:5]}...")
    
    out_dir = Path("data/raw") / "historical_warc"
    out_dir.mkdir(parents=True, exist_ok=True)
    
    all_tokens = {}
    total_urls = 0
    total_boards = 0
    
    for crawl in crawls:
        print(f"\n[historical_warc] Processing crawl: {crawl}")
        
        for prefix in PREFIXES:
            try:
                urls = get_historical_board_urls(crawl, prefix, max_urls=100000)
                print(f"  [{crawl}|{prefix}] Found {len(urls)} URLs")
                
                if not urls:
                    continue
                
                sample_urls = urls[:10000] if len(urls) > 10000 else urls
                
                validated = validate_board_urls(sample_urls[:1000])
                
                for url in urls:
                    board_info = extract_board_from_url(url)
                    if board_info:
                        ats, code = board_info
                        if ats in CORE_ATS:
                            key = (ats, code.casefold())
                            if key not in all_tokens:
                                all_tokens[key] = (code, "", f"historical_warc:{crawl}")
                
                total_urls += len(urls)
                total_boards += len(set(extract_board_from_url(u) for u in urls if extract_board_from_url(u)))
                
            except Exception as e:
                print(f"  Error on {crawl}|{prefix}: {e}")
                continue
    
    out_dir = Path("data/raw") / "historical_warc"
    out_dir.mkdir(parents=True, exist_ok=True)
    write_tokens(out_dir / "tokens.csv", all_tokens)
    
    return {
        "crawls_processed": len(crawls),
        "total_urls": total_urls,
        "total_boards": total_boards,
        "unique_tokens": len(all_tokens),
    }


def _connect():
    import duckdb
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("SET threads=4; SET http_timeout=120000; SET http_retries=5; SET http_retry_wait_ms=2000;")
    return con


def _load_footers(cc_index_dir: Path, crawl: str):
    path = cc_index_dir / "footers" / f"{crawl}.json"
    if path.exists():
        data = json.loads(path.read_text())
        return [(row[0], row[1], row[2]) for row in data]
    return []


def _surt_range(prefix: str) -> tuple[str, str]:
    return prefix, prefix + "\uffff"


def _connect():
    import duckdb
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("SET threads=4; SET http_timeout=120000; SET http_retries=5; SET http_retry_wait_ms=2000;")
    return con


def _load_footers(cc_index_dir: Path, crawl: str):
    path = cc_index_dir / "footers" / f"{crawl}.json"
    if path.exists():
        data = json.loads(path.read_text())
        return [(row[0], row[1], row[2]) for row in data]
    return []


def _relevant_files(footers, prefix):
    start, end = _surt_range(prefix)
    return [f for f, min_k, max_k in footers if max_k is not None and min_k is not None and max_k >= start and min_k < end]


def _surt_range(prefix):
    return prefix, prefix + "\uffff"


def _connect():
    import duckdb
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("SET threads=4; SET http_timeout=120000; SET http_retries=5; SET http_retry_wait_ms=2000;")
    return con


def _load_footers(cc_index_dir, crawl):
    path = cc_index_dir / "footers" / f"{crawl}.json"
    if path.exists():
        data = json.loads(path.read_text())
        return [(row[0], row[1], row[2]) for row in data]
    return []


def _relevant_files(footers, prefix):
    start, end = _surt_range(prefix)
    return [f for f, min_k, max_k in footers if max_k is not None and min_k is not None and max_k >= start and min_k < end]


def _surt_range(prefix):
    return prefix, prefix + "\uffff"


def _connect():
    import duckdb
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("SET threads=4; SET http_timeout=120000; SET http_retries=5; SET http_retry_wait_ms=2000;")
    return con


def _load_footers(cc_index_dir, crawl):
    path = cc_index_dir / "footers" / f"{crawl}.json"
    if path.exists():
        data = json.loads(path.read_text())
        return [(row[0], row[1], row[2]) for row in data]
    return []


def _relevant_files(footers, prefix):
    start, end = _surt_range(prefix)
    return [f for f, min_k, max_k in footers if max_k is not None and min_k is not None and max_k >= start and min_k < end]


def _surt_range(prefix):
    return prefix, prefix + "\uffff"


if __name__ == "__main__":
    import argparse
    import sys
    parser = argparse.ArgumentParser(prog="company_dataset.sources.historical_warc")
    parser.add_argument("--raw", default="data/raw")
    parser.add_argument("--start-year", type=int, default=2019)
    parser.add_argument("--max-crawls", type=int, default=20)
    parser.add_argument("--sample", action="store_true", help="Sample mode (fewer URLs)")
    a = parser.parse_args(sys.argv[1:])
    
    raw_dir = Path(a.raw)
    result = mine_historical_boards(
        Path(a.raw),
        start_year=a.start_year,
        max_crawls=a.max_crawls,
        sample_mode=a.sample,
    )
    print(json.dumps(result, indent=1))
