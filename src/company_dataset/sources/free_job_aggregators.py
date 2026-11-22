#!/usr/bin/env python3
"""Free job aggregator APIs with direct ATS URLs."""
from __future__ import annotations

import json
import os
import re
import requests
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from company_dataset.models import SourceMeta, SourceRecord
from company_dataset.normalize import CORE_ATS, clean_code
from company_dataset.sources._harvest import board_from_url, parse_tokens, write_tokens

META = SourceMeta(
    name="free_job_aggregators",
    description="Free job aggregator APIs with direct ATS URLs",
    download_url="",
    filename="tokens.csv",
    license="Various free tiers",
    license_url="",
    license_note="Free job APIs with direct ATS URLs",
    attribution="Multiple free job APIs",
    downloadable=False,
)

ATS_PATTERNS = {
    "greenhouse": r"boards\.greenhouse\.io/([^/?]+)",
    "lever": r"jobs\.lever\.co/([^/?]+)",
    "ashby": r"jobs\.ashbyhq\.com/([^/?]+)",
    "smartrecruiters": r"jobs\.smartrecruiters\.com/([^/?]+)",
    "breezy": r"([a-z0-9-]+)\.breezy\.hr",
}

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def fetch_remoteok_jobs(limit: int = 100) -> list[dict]:
    """Fetch jobs from RemoteOK - returns jobs with direct apply URLs."""
    try:
        r = requests.get("https://remoteok.com/api", timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            data = r.json()
            return data[1:limit+1] if len(data) > 1 else []
    except Exception as e:
        print(f"[remoteok] error: {e}")
    return []


def fetch_wwr_jobs() -> list[dict]:
    """Fetch jobs from WeWorkRemotely RSS."""
    try:
        r = requests.get("https://weworkremotely.com/remote-jobs.rss", timeout=15, headers=HEADERS)
        if r.status_code == 200:
            root = ET.fromstring(r.content)
            jobs = []
            for item in root.findall(".//item"):
                job = {}
                for child in item:
                    job[child.tag] = child.text
                jobs.append(job)
            return jobs
    except Exception as e:
        print(f"[wwr] error: {e}")
    return []


def fetch_wwr_job_details(url: str) -> list[str]:
    """Fetch WWR job page and extract ATS URLs."""
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            urls = re.findall(r'https?://[^\s"\'<>]+', r.text)
            return urls
    except Exception:
        pass
    return []


def fetch_arbeitnow(limit: int = 100) -> list[dict]:
    try:
        r = requests.get("https://www.arbeitnow.com/api/job-board-api", params={"limit": limit}, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            return r.json().get("data", [])
    except Exception as e:
        print(f"[arbeitnow] error: {e}")
    return []


def fetch_remotive() -> list[dict]:
    try:
        r = requests.get("https://remotive.com/api/remote-jobs", timeout=15)
        if r.status_code == 200:
            return r.json().get("jobs", [])
    except Exception as e:
        print(f"[remotive] error: {e}")
    return []


def fetch_jobicy() -> list[dict]:
    try:
        r = requests.get("https://jobicy.com/api/v2/remote-jobs", params={"count": 50}, timeout=15)
        if r.status_code == 200:
            return r.json().get("jobs", [])
    except Exception as e:
        print(f"[jobicy] error: {e}")
    return []


def fetch_gleif(query: str = "", limit: int = 1000) -> list[dict]:
    """Fetch legal entities from GLEIF."""
    try:
        r = requests.get(
            "https://api.gleif.org/api/v1/lei-records",
            params={"filter[fulltext]": query, "page[size]": min(limit, 1000)},
            timeout=30,
        )
        if r.status_code == 200:
            return r.json().get("data", [])
    except Exception as e:
        print(f"[gleif] error: {e}")
    return []


def fetch_sec_edgar(cik: str = "") -> list[dict]:
    """Fetch company info from SEC EDGAR."""
    try:
        if not cik:
            # Get all CIKs
            r = requests.get("https://www.sec.gov/files/company_tickers.json", timeout=30)
            if r.status_code == 200:
                return list(r.json().values())
        else:
            r = requests.get(f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json", timeout=15)
            if r.status_code == 200:
                return [r.json()]
    except Exception as e:
        print(f"[sec_edgar] error: {e}")
    return []


def harvest(raw_dir: Path) -> dict:
    out = Path(raw_dir) / META.name
    out.mkdir(parents=True, exist_ok=True)

    all_tokens = {}

    def add_token(ats: str, code: str, evidence: str):
        key = (ats, code.casefold())
        if key not in all_tokens:
            all_tokens[(ats, code.casefold())] = (code, "", evidence)

    def extract_from_urls(urls: list[str], source: str):
        for url in urls:
            for ats, pattern in ATS_PATTERNS.items():
                m = re.search(pattern, url)
                if m:
                    code = m.group(1)
                    if code:
                        add_token(ats, code, f"{source}:{url}")

    print("[free_job_aggregators] Fetching RemoteOK jobs...")
    for job in fetch_remoteok_jobs(200):
        apply_url = job.get("apply_url") or job.get("url", "")
        if apply_url:
            for ats, pattern in ATS_PATTERNS.items():
                m = re.search(pattern, apply_url)
                if m:
                    code = m.group(1)
                    if code:
                        add_token(ats, code, f"remoteok:{apply_url}")

    print("[free_job_aggregators] Fetching WWR jobs...")
    for job in fetch_wwr_jobs():
        link = job.get("link", "")
        if link:
            urls = fetch_wwr_job_details(link)
            for url in urls:
                for ats, pattern in ATS_PATTERNS.items():
                    m = re.search(pattern, url)
                    if m:
                        code = m.group(1)
                        if code:
                            add_token(ats, code, f"wwr:{url}")

    print("[free_job_aggregators] Fetching Arbeitnow...")
    try:
        r = requests.get("https://www.arbeitnow.com/api/job-board-api", params={"limit": 100}, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            for job in r.json().get("data", []):
                url = job.get("url", "")
                if url:
                    for ats, pattern in ATS_PATTERNS.items():
                        m = re.search(pattern, url)
                        if m:
                            code = m.group(1)
                            if code:
                                add_token(ats, code, f"arbeitnow:{url}")
    except Exception as e:
        print(f"[arbeitnow] error: {e}")

    print("[free_job_aggregators] Fetching Remotive...")
    for job in fetch_remotive():
        url = job.get("url", "")
        if url:
            for ats, pattern in ATS_PATTERNS.items():
                m = re.search(pattern, url)
                if m:
                    code = m.group(1)
                    if code:
                        add_token(ats, code, f"remotive:{url}")

    print("[free_job_aggregators] Fetching Jobicy...")
    for job in fetch_jobicy():
        url = job.get("url", "")
        if url:
            for ats, pattern in ATS_PATTERNS.items():
                m = re.search(pattern, url)
                if m:
                    code = m.group(1)
                    if code:
                        add_token(ats, code, f"jobicy:{url}")

    print("[free_job_aggregators] Fetching GLEIF entities...")
    gleif_data = fetch_gleif("", 2000)
    for record in gleif_data:
        attrs = record.get("attributes", {})
        entity = attrs.get("entity", {})
        website = entity.get("website", "")
        lei = record.get("id", "")
        if website and lei:
            for ats, pattern in ATS_PATTERNS.items():
                m = re.search(pattern, website)
                if m:
                    code = m.group(1)
                    if code:
                        add_token(ats, code, f"gleif:{lei}:{website}")

    print("[free_job_aggregators] Fetching SEC EDGAR company tickers...")
    try:
        r = requests.get("https://www.sec.gov/files/company_tickers.json", timeout=30)
        if r.status_code == 200:
            tickers = r.json()
            print(f"[free_job_aggregators] Got {len(tickers)} SEC tickers")
    except Exception as e:
        print(f"[sec_edgar] error: {e}")

    # Write tokens
    out_dir = Path(raw_dir) / META.name
    out_dir.mkdir(parents=True, exist_ok=True)
    write_tokens(out_dir / META.filename, all_tokens)

    return {"tokens": len(all_tokens)}


META = SourceMeta(
    name="free_job_aggregators",
    description="Free job aggregator APIs with direct ATS URLs",
    download_url="",
    filename="tokens.csv",
    license="Various free tiers",
    license_url="",
    license_note="Free job APIs with direct ATS URLs",
    attribution="Multiple free job APIs",
    downloadable=False,
)

ATS_PATTERNS = {
    "greenhouse": r"boards\.greenhouse\.io/([^/?]+)",
    "lever": r"jobs\.lever\.co/([^/?]+)",
    "ashby": r"jobs\.ashbyhq\.com/([^/?]+)",
    "smartrecruiters": r"jobs\.smartrecruiters\.com/([^/?]+)",
    "breezy": r"([a-z0-9-]+)\.breezy\.hr",
}

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def fetch_remoteok_jobs(limit: int = 100) -> list[dict]:
    """Fetch jobs from RemoteOK - returns jobs with direct apply URLs."""
    try:
        r = requests.get("https://remoteok.com/api", timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            data = r.json()
            return data[1:limit+1] if len(data) > 1 else []
    except Exception as e:
        print(f"[remoteok] error: {e}")
    return []


def fetch_wwr_jobs() -> list[dict]:
    """Fetch jobs from WeWorkRemotely RSS."""
    try:
        r = requests.get("https://weworkremotely.com/remote-jobs.rss", timeout=15, headers=HEADERS)
        if r.status_code == 200:
            root = ET.fromstring(r.content)
            jobs = []
            for item in root.findall(".//item"):
                job = {}
                for child in item:
                    job[child.tag] = child.text
                jobs.append(job)
            return jobs
    except Exception as e:
        print(f"[wwr] error: {e}")
    return []


def fetch_wwr_job_details(url: str) -> list[str]:
    """Fetch WWR job page and extract ATS URLs."""
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            urls = re.findall(r'https?://[^\s"\'<>]+', r.text)
            return urls
    except Exception:
        pass
    return []


def fetch_arbeitnow(limit: int = 100) -> list[dict]:
    try:
        r = requests.get("https://www.arbeitnow.com/api/job-board-api", params={"limit": limit}, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            return r.json().get("data", [])
    except Exception as e:
        print(f"[arbeitnow] error: {e}")
    return []


def fetch_remotive() -> list[dict]:
    try:
        r = requests.get("https://remotive.com/api/remote-jobs", timeout=15)
        if r.status_code == 200:
            return r.json().get("jobs", [])
    except Exception as e:
        print(f"[remotive] error: {e}")
    return []


def fetch_jobicy() -> list[dict]:
    try:
        r = requests.get("https://jobicy.com/api/v2/remote-jobs", params={"count": 50}, timeout=15)
        if r.status_code == 200:
            return r.json().get("jobs", [])
    except Exception as e:
        print(f"[jobicy] error: {e}")
    return []


def fetch_gleif(query: str = "", limit: int = 1000) -> list[dict]:
    """Fetch legal entities from GLEIF."""
    try:
        r = requests.get(
            "https://api.gleif.org/api/v1/lei-records",
            params={"filter[fulltext]": query, "page[size]": min(limit, 1000)},
            timeout=30,
        )
        if r.status_code == 200:
            return r.json().get("data", [])
    except Exception as e:
        print(f"[gleif] error: {e}")
    return []


def fetch_sec_edgar(cik: str = "") -> list[dict]:
    """Fetch company info from SEC EDGAR."""
    try:
        if not cik:
            r = requests.get("https://www.sec.gov/files/company_tickers.json", timeout=30)
            if r.status_code == 200:
                return list(r.json().values())
        else:
            r = requests.get(f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json", timeout=15)
            if r.status_code == 200:
                return [r.json()]
    except Exception as e:
        print(f"[sec_edgar] error: {e}")
    return []


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(prog="company_dataset.sources.free_job_aggregators")
    p.add_argument("--raw", default="data/raw")
    a = p.parse_args()
    import json
    print(json.dumps(harvest(Path(a.raw)), indent=1))