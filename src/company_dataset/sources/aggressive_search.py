#!/usr/bin/env python3
"""Aggressive search/fetch for company metadata and board discovery.

Uses ALL available APIs and web search to:
1. Resolve unresolved boards (1,827)
2. Fill descriptions for 7,757 incomplete companies
3. Find websites for 7,052 companies missing both
5. Discover new boards from SR and all ATS
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sqlite3
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests

from company_dataset import store
from company_dataset.identity import registrable_domain
from company_dataset.normalize import clean_website
from company_dataset.website_meta import og_description, twitter_description, jsonld_org_description, about_meta

DB = "data/store.db"
RUN = "aggressive_search_2026-11-05"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

# Load env
from dotenv import load_dotenv
load_dotenv()

BRAVE_KEY = os.getenv("BRAVE_API_KEY", "")
SERPAPI_KEY = os.getenv("SERPAPI_KEY", "")
MOJEEK_KEY = os.getenv("MOJEEK_API_KEY", "")
ADZUNA_ID = os.getenv("ADZUNA_APP_ID", "")
ADZUNA_KEY = os.getenv("ADZUNA_APP_KEY", "")
REED_KEY = os.getenv("REED_API_KEY", "")
THEIRSTACK_KEY = os.getenv("THEIRSTACK_API_KEY", "")
USAJOBS_KEY = os.getenv("USAJOBS_API_KEY", "")
USAJOBS_EMAIL = os.getenv("USAJOBS_USER_EMAIL", "")
JUJU_KEY = os.getenv("JUJU_API_KEY", "")
DICE_KEY = os.getenv("DICE_API_KEY", "")
CAREERJET_KEY = os.getenv("CAREERJET_API_KEY", "")
COMPANIES_HOUSE_KEY = os.getenv("COMPANIES_HOUSE_API_KEY", "")
OPENCORPORATES_KEY = os.getenv("OPENCORPORATES_API_KEY", "")
PROPUBLICA_KEY = os.getenv("PROPUBLICA_NONPROFITS_KEY", "")
WHOISXML_KEY = os.getenv("WHOISXML_API_KEY", "")
CRUNCHBASE_KEY = os.getenv("CRUNCHBASE_API_KEY", "")
USAJOBS_EMAIL = os.getenv("USAJOBS_USER_EMAIL", "")
THEIRSTACK_KEY = os.getenv("THEIRSTACK_API_KEY", "")
SECURITYTRAILS_KEY = os.getenv("SECURITYTRAILS_API_KEY", "")
WAPPALYZER_KEY = os.getenv("WAPPALYZER_API_KEY", "")
CENSYS_ID = os.getenv("CENSYS_API_ID", "")
CENSYS_SECRET = os.getenv("CENSYS_API_SECRET", "")
APIFY_TOKEN = os.getenv("APIFY_TOKEN", "")

_session = requests.Session()
_session.headers.update({"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"})


def log(msg: str):
    print(f"[{RUN}] {msg}", flush=True)


def get_conn():
    return store.connect(DB)


def record_evidence(conn, board_id: int, field: str, value: str, method: str, evidence_url: str, evidence_text: str):
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with conn:
        conn.execute("""INSERT OR IGNORE INTO field_evidence
            (board_id, field, value, method, evidence_url, evidence_text, status, observed_at, run)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (board_id, field, value, method, evidence_url, evidence_text[:5000],
             "verified", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), RUN))


# ─── SEARCH APIs ──────────────────────────────────────────────────────────────

def brave_search(query: str, count: int = 10) -> list[dict]:
    if not BRAVE_KEY:
        return []
    try:
        r = _session.get("https://api.search.brave.com/res/v1/web/search",
                         params={"q": query, "count": count},
                         headers={"Accept": "application/json", "X-Subscription-Token": BRAVE_KEY},
                         timeout=15)
        if r.status_code == 200:
            return r.json().get("web", {}).get("results", [])
    except Exception as e:
        log(f"Brave error: {e}")
    return []


def serpapi_search(query: str, count: int = 10) -> list[dict]:
    if not SERPAPI_KEY:
        return []
    try:
        r = _session.get("https://serpapi.com/search",
                         params={"q": query, "num": count, "api_key": SERPAPI_KEY, "engine": "google"},
                         timeout=15)
        if r.status_code == 200:
            return r.json().get("organic_results", [])
    except Exception as e:
        log(f"SerpAPI error: {e}")
    return []


def mojeek_search(query: str, count: int = 10) -> list[dict]:
    if not MOJEEK_KEY:
        return []
    try:
        r = _session.get("https://api.mojeek.com/v1/search",
                         params={"q": query, "fmt": "json", "api_key": MOJEEK_KEY, "count": count},
                         timeout=15)
        if r.status_code == 200:
            return r.json().get("response", {}).get("results", [])
    except Exception as e:
        log(f"Mojeek error: {e}")
    return []


def search_all(query: str, max_results: int = 20) -> list[str]:
    """Search all engines, return unique URLs."""
    urls = set()
    for results in (brave_search(query, max_results),
                    serpapi_search(query, max_results),
                    mojeek_search(query, max_results)):
        for r in results:
            url = r.get("url") or r.get("link")
            if url and url.startswith("http"):
                urls.add(url)
    return list(urls)[:max_results]


# ─── Company Registry APIs ────────────────────────────────────────────────────

def companies_house_search(name: str) -> list[dict]:
    if not COMPANIES_HOUSE_KEY:
        return []
    try:
        r = _session.get("https://api.company-information.service.gov.uk/search/companies",
                         params={"q": name, "items_per_page": 20},
                         auth=(COMPANIES_HOUSE_KEY, ""),
                         timeout=15)
        if r.status_code == 200:
            return r.json().get("items", [])
    except Exception as e:
        log(f"Companies House error: {e}")
    return []


def opencorporates_search(name: str) -> list[dict]:
    if not OPENCORPORATES_KEY:
        return []
    try:
        r = _session.get("https://api.opencorporates.com/v0.4/companies/search",
                         params={"q": name, "api_token": OPENCORPORATES_KEY, "per_page": 20},
                         timeout=15)
        if r.status_code == 200:
            return r.json().get("results", {}).get("companies", [])
    except Exception as e:
        log(f"OpenCorporates error: {e}")
    return []


def gleif_search(name: str) -> list[dict]:
    try:
        r = _session.get("https://api.gleif.org/api/v1/lei-records",
                         params={"filter[fulltext]": name, "page[size]": 20},
                         timeout=15)
        if r.status_code == 200:
            return r.json().get("data", [])
    except Exception as e:
        log(f"GLEIF error: {e}")
    return []


def propub_nonprofit_search(name: str) -> list[dict]:
    if not PROPUBLICA_KEY:
        return []
    try:
        r = _session.get("https://projects.propublica.org/nonprofits/api/v2/search.json",
                         params={"q": name, "api_key": PROPUBLICA_KEY},
                         timeout=15)
        if r.status_code == 200:
            return r.json().get("organizations", [])
    except Exception as e:
        log(f"ProPublica error: {e}")
    return []


def whoisxml_whois(domain: str) -> dict:
    if not WHOISXML_KEY:
        return {}
    try:
        r = _session.get("https://www.whoisxmlapi.com/whoisserver/WhoisService",
                         params={"apiKey": WHOISXML_KEY, "domainName": domain, "outputFormat": "JSON"},
                         timeout=15)
        if r.status_code == 200:
            return r.json().get("WhoisRecord", {})
    except Exception as e:
        log(f"WhoisXML error: {e}")
    return {}


def wappalyzer_tech(domain: str) -> dict:
    if not WAPPALYZER_KEY:
        return {}
    try:
        r = _session.get(f"https://api.wappalyzer.com/v2/lookup/?urls={domain}",
                         headers={"x-api-key": WAPPALYZER_KEY},
                         timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log(f"Wappalyzer error: {e}")
    return {}


def securitytrails_subdomains(domain: str) -> list[str]:
    if not SECURITYTRAILS_KEY:
        return []
    try:
        r = _session.get(f"https://api.securitytrails.com/v1/domain/{domain}/subdomains",
                         params={"apikey": SECURITYTRAILS_KEY},
                         timeout=15)
        if r.status_code == 200:
            return r.json().get("subdomains", [])
    except Exception as e:
        log(f"SecurityTrails error: {e}")
    return []


def censys_search(query: str) -> list[dict]:
    if not CENSYS_ID or not CENSYS_SECRET:
        return []
    try:
        r = _session.post("https://search.censys.io/api/v2/hosts/search",
                          json={"q": query, "per_page": 20},
                          auth=(CENSYS_ID, CENSYS_SECRET),
                          timeout=15)
        if r.status_code == 200:
            return r.json().get("result", {}).get("hits", [])
    except Exception as e:
        log(f"Censys error: {e}")
    return []


def crunchbase_search(name: str) -> list[dict]:
    if not CRUNCHBASE_KEY:
        return []
    try:
        r = _session.get("https://api.crunchbase.com/api/v4/searches/organizations",
                         params={"query": name, "field_ids": ["identifier", "short_description", "website_url"]},
                         headers={"Authorization": f"Bearer {CRUNCHBASE_KEY}"},
                         timeout=15)
        if r.status_code == 200:
            return r.json().get("entities", [])
    except Exception as e:
        log(f"Crunchbase error: {e}")
    return []


# ─── Job Aggregator APIs ──────────────────────────────────────────────────────

def adzuna_search(what: str, where: str = "") -> list[dict]:
    if not ADZUNA_ID or not ADZUNA_KEY:
        return []
    try:
        r = _session.get(f"https://api.adzuna.com/v1/api/jobs/us/search/1",
                         params={"app_id": ADZUNA_ID, "app_key": ADZUNA_KEY,
                                 "what": what, "where": where, "results_per_page": 50,
                                 "content_type": "application/json"},
                         timeout=15)
        if r.status_code == 200:
            return r.json().get("results", [])
    except Exception as e:
        log(f"Adzuna error: {e}")
    return []


def reed_search(keywords: str) -> list[dict]:
    if not REED_KEY:
        return []
    try:
        r = _session.get("https://www.reed.co.uk/api/1.0/search",
                         params={"keywords": keywords, "resultsToTake": 50},
                         headers={"Authorization": f"Bearer {REED_KEY}"},
                         timeout=15)
        if r.status_code == 200:
            return r.json().get("results", [])
    except Exception as e:
        log(f"Reed error: {e}")
    return []


def arbeitnow_search(query: str) -> list[dict]:
    try:
        r = _session.get("https://www.arbeitnow.com/api/job-board-api",
                         params={"q": query, "limit": 50},
                         timeout=15)
        if r.status_code == 200:
            return r.json().get("data", [])
    except Exception as e:
        log(f"Arbeitnow error: {e}")
    return []


def jobicy_search() -> list[dict]:
    try:
        r = _session.get("https://jobicy.com/api/v2/remote-jobs", params={"count": 50}, timeout=15)
        if r.status_code == 200:
            return r.json().get("jobs", [])
    except Exception as e:
        log(f"Jobicy error: {e}")
    return []


def remotive_search() -> list[dict]:
    try:
        r = _session.get("https://remotive.com/api/remote-jobs", timeout=15)
        if r.status_code == 200:
            return r.json().get("jobs", [])
    except Exception as e:
        log(f"Remotive error: {e}")
    return []


def jobnet_dk_search() -> list[dict]:
    # Jobnet.dk (Denmark) - no auth needed
    try:
        r = _session.get("https://jobnet.dk/api/v1/jobs", params={"limit": 50}, timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log(f"Jobnet.dk error: {e}")
    return []


# ─── Web Metadata Extraction ──────────────────────────────────────────────────

def fetch_url(url: str, timeout: int = 15) -> str | None:
    try:
        r = _session.get(url, timeout=timeout, allow_redirects=True)
        if r.status_code == 200 and r.text:
            return r.text[:3_000_000]
    except Exception:
        pass
    return None


def extract_all_meta(url: str, html: str) -> dict[str, str]:
    desc = og_description(html) or twitter_description(html) or jsonld_org_description(html)
    about_desc, _ = about_meta(url, html)
    results = {}
    if desc:
        results["og_twitter_jsonld"] = desc
    if about_desc:
        results["about_page"] = about_desc
    return results


# ─── Core Pipeline Functions ──────────────────────────────────────────────────

def enrich_company(company_name: str, website: str = "") -> dict[str, Any]:
    """Enrich a company with all available sources."""
    result = {"description": "", "website": website, "sources": []}

    # 1. Direct website fetch
    if website:
        html = fetch_url(website)
        if html:
            meta = extract_all_meta(website, html)
            for method, desc in meta.items():
                if desc and len(desc) > 50:
                    return {"description": desc, "source": f"website:{method}", "website": website}
            # Try about page
            about_urls = [website.rstrip("/") + p for p in ("/about", "/about-us", "/company", "/who-we-are")]
            for u in about_urls:
                h = fetch_url(u)
                if h:
                    meta = extract_all_meta(u, h)
                    for method, desc in meta.items():
                        if desc and len(desc) > 50:
                            return {"description": desc, "source": f"about_page:{method}", "website": website}

    # 2. Search engines for company name + "about" / "company description"
    queries = [
        f'"{company_name}" company description',
        f'"{company_name}" about us',
        f'"{company_name}" official website',
        f'{company_name} site:linkedin.com/company',
    ]
    for q in queries:
        urls = search_all(q, 5)
        for url in urls:
            html = fetch_url(url)
            if html:
                meta = extract_all_meta(url, html)
                for method, desc in meta.items():
                    if desc and len(desc) > 50:
                        # Verify it's about our company
                        if any(tok.lower() in desc.lower() for tok in company_name.split() if len(tok) > 3):
                            return {"description": desc, "source": f"search:{method}", "website": url}

    # 3. Company registries
    for source, results in [
        ("companies_house", companies_house_search(company_name)),
        ("opencorporates", opencorporates_search(company_name)),
        ("gleif", gleif_search(company_name)),
        ("propublica", propub_nonprofit_search(company_name)),
    ]:
        for item in results[:3]:
            # Extract description/website from registry data
            desc = item.get("description") or item.get("description_short") or ""
            site = item.get("website") or item.get("website_url") or item.get("company_url") or ""
            if desc and len(desc) > 50:
                return {"description": desc, "source": f"registry:{source}", "website": site}

    # 4. Job aggregators - search by company name
    for source, results in [
        ("adzuna", adzuna_search(company_name)),
        ("remotive", remotive_search()),
        ("jobicy", jobicy_search()),
    ]:
        for job in results[:10]:
            if company_name.lower() in (job.get("company", {}).get("display_name", "").lower() if isinstance(job.get("company"), dict) else job.get("company_name", "").lower()):
                desc = job.get("description", "")
                if desc and len(desc) > 50:
                    return {"description": desc, "source": f"jobs:{source}", "website": ""}

    return {}


def process_incomplete_companies(limit: int = 0) -> int:
    """Process companies missing descriptions."""
    conn = get_conn()
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT c.id, c.company_name, c.website_url, c.primary_board_id
        FROM companies c
        WHERE c.status = 'incomplete'
          AND (c.description = '' OR c.description IS NULL)
    """).fetchall()

    if limit:
        rows = rows[:limit]

    log(f"Processing {len(rows)} incomplete companies...")
    added = 0

    for i, row in enumerate(rows):
        cid = row["id"]
        name = row["company_name"]
        website = row["website_url"] or ""
        board_id = row["primary_board_id"]

        log(f"[{i+1}/{len(rows)}] Enriching: {name}")
        result = enrich_company(name, website)

        if result.get("description"):
            record_evidence(get_conn(), board_id, "description", result["description"],
                           result["source"], result.get("website", ""), result["description"][:500])
            if result.get("website") and not website:
                record_evidence(get_conn(), board_id, "website_url", result["website"],
                               result["source"], result["website"], result["website"])
            added += 1
            log(f"  ✓ Added description via {result['source']}")
        else:
            log(f"  ✗ No description found")

        if i % 50 == 0:
            time.sleep(1)

    log(f"Added {added} descriptions")
    return added


def process_unresolved_boards(limit: int = 0) -> int:
    """Try to resolve identity for unresolved boards using web search."""
    conn = get_conn()
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT b.id, b.ats, b.code, c.company_name
        FROM boards b
        JOIN companies c ON c.primary_board_id = b.id
        JOIN identities i ON i.board_id = b.id
        WHERE i.status IN ('identity_unresolved', 'identity_mismatch')
          AND i.run = (SELECT MAX(run) FROM identities WHERE board_id = b.id)
    """).fetchall()

    if limit:
        rows = rows[:limit]

    log(f"Processing {len(rows)} unresolved boards...")
    resolved = 0

    for row in rows:
        bid = row["id"]
        ats = row["ats"]
        code = row["code"]
        name = row["company_name"]

        # Search for official site linking to this board
        queries = [
            f'site:{ats}.com "{code}"',
            f'"{name}" "{code}" careers',
            f'"{name}" "ats"',
        ]
        for q in queries:
            urls = search_all(q, 5)
            for url in urls:
                html = fetch_url(url)
                if html and code.lower() in html.lower():
                    # Found a page linking to this board
                    record_evidence(get_conn(), bid, "name", name, "web_search",
                                   url, url, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
                    resolved += 1
                    break
        if resolved > 0:
            break

    return resolved


def discover_new_boards(limit: int = 0) -> int:
    """Discover new boards from job aggregators and registries."""
    conn = get_conn()

    # Adzuna - search for all companies with job postings
    jobs = adzuna_search("", "us")
    added = 0
    for job in jobs[:200]:
        board_url = job.get("redirect_url", "")
        for ats, pattern in [
            ("greenhouse", r"boards\.greenhouse\.io/([^/?]+)"),
            ("lever", r"jobs\.lever\.co/([^/?]+)"),
            ("ashby", r"jobs\.ashbyhq\.com/([^/?]+)"),
            ("smartrecruiters", r"jobs\.smartrecruiters\.com/([^/?]+)"),
            ("breezy", r"([a-z0-9-]+)\.breezy\.hr"),
        ]:
            import re
            m = re.search(pattern, board_url)
            if m:
                code = m.group(1)
                # Check if we already have this board
                existing = conn.execute("SELECT id FROM boards WHERE ats=? AND code=?", (ats, code)).fetchone()
                if not existing:
                    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    with conn:
                        conn.execute("""INSERT INTO boards (ats, code, code_key, region)
                                       VALUES (?,?,?,?)""", (ats, code, code.lower(), "US"))
                    added += 1
    return added


def run_all(sample: int = 100):
    """Run the full aggressive pipeline."""
    log(f"Starting aggressive search pipeline (sample={sample})")

    # 1. Fill incomplete descriptions
    process_incomplete_companies(sample)

    # 2. Resolve unresolved boards
    process_unresolved_boards(sample)

    # 3. Discover new boards
    discover_new_boards(sample)

    log("Pipeline complete")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=0, help="Sample size (0=all)")
    parser.add_argument("--only", choices=["incomplete", "unresolved", "discover"], help="Run only one step")
    args = parser.parse_args()

    if args.only == "incomplete":
        process_incomplete_companies(args.sample)
    elif args.only == "unresolved":
        process_unresolved_boards(args.sample)
    elif args.only == "discover":
        discover_new_boards(args.sample)
    else:
        run_all(args.sample)