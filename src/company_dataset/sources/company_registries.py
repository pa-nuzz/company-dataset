#!/usr/bin/env python3
"""Company registry APIs for identity resolution and website discovery."""
from __future__ import annotations

import json
import os
import re
import requests
from typing import Any

from company_dataset import store
from company_dataset.models import SourceMeta
from company_dataset.normalize import clean_website
from company_dataset.identity import registrable_domain

DB = "data/store.db"

META = SourceMeta(
    name="company_registries",
    description="Company registry APIs for identity resolution and website discovery",
    download_url="",
    filename="tokens.csv",
    license="Various public APIs",
    license_url="",
    license_note="Public company registries: GLEIF, SEC EDGAR, Companies House, OpenCorporates, ProPublica",
    attribution="GLEIF, SEC, UK Companies House, OpenCorporates, ProPublica",
    downloadable=False,
)


def gleif_search(name: str = "", limit: int = 100) -> list[dict]:
    """Search GLEIF for legal entities."""
    try:
        params = {"page[size]": min(limit, 1000)}
        if name:
            params["filter[fulltext]"] = name
        r = requests.get(
            "https://api.gleif.org/api/v1/lei-records",
            params=params,
            timeout=30,
        )
        if r.status_code == 200:
            return r.json().get("data", [])
    except Exception as e:
        print(f"[gleif] error: {e}")
    return []


def sec_edgar_cik_lookup(name: str = "") -> list[dict]:
    """Search SEC EDGAR for company CIKs."""
    try:
        r = requests.get("https://www.sec.gov/files/company_tickers.json", timeout=30)
        if r.status_code == 200:
            tickers = r.json()
            if name:
                name_lower = name.lower()
                return [t for t in tickers.values() if name_lower in t.get("title", "").lower()]
            return list(tickers.values())
    except Exception as e:
        print(f"[sec_edgar] error: {e}")
    return []


def sec_edgar_company_filings(cik: str) -> dict:
    """Get company filings from SEC EDGAR."""
    try:
        r = requests.get(
            f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json",
            timeout=15,
            headers={"User-Agent": "company-dataset/1.0 (contact@company-dataset.com)"}
        )
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"[sec_edgar] error: {e}")
    return {}


def gleif_search_by_name(name: str) -> list[dict]:
    """Search GLEIF by entity name."""
    try:
        r = requests.get(
            "https://api.gleif.org/api/v1/lei-records",
            params={"filter[fulltext]": name, "page[size]": 20},
            timeout=30,
        )
        if r.status_code == 200:
            return r.json().get("data", [])
    except Exception as e:
        print(f"[gleif] error: {e}")
    return []


def companies_house_search(name: str) -> list[dict]:
    """Search UK Companies House."""
    api_key = os.getenv("COMPANIES_HOUSE_API_KEY")
    if not api_key:
        return []
    try:
        r = requests.get(
            "https://api.company-information.service.gov.uk/search/companies",
            params={"q": name, "items_per_page": 20},
            auth=(api_key, ""),
            timeout=15,
        )
        if r.status_code == 200:
            return r.json().get("items", [])
    except Exception as e:
        print(f"[companies_house] error: {e}")
    return []


def opencorporates_search(name: str) -> list[dict]:
    """Search OpenCorporates."""
    api_key = os.getenv("OPENCORPORATES_API_KEY")
    if not api_key:
        return []
    try:
        r = requests.get(
            "https://api.opencorporates.com/v0.4/companies/search",
            params={"q": name, "api_token": api_key, "per_page": 20},
            timeout=15,
        )
        if r.status_code == 200:
            return r.json().get("results", {}).get("companies", [])
    except Exception as e:
        print(f"[opencorporates] error: {e}")
    return []


def propublica_nonprofit_search(name: str) -> list[dict]:
    """Search ProPublica Nonprofit Explorer."""
    api_key = os.getenv("PROPUBLICA_NONPROFITS_KEY")
    if not api_key:
        return []
    try:
        r = requests.get(
            "https://projects.propublica.org/nonprofits/api/v2/search.json",
            params={"q": name, "api_key": api_key},
            timeout=15,
        )
        if r.status_code == 200:
            return r.json().get("organizations", [])
    except Exception as e:
        print(f"[propublica] error: {e}")
    return []


def resolve_company_identity(company_name: str) -> dict:
    """Try to resolve a company's identity using multiple registries."""
    results = {
        "gleif": [],
        "sec_edgar": [],
        "companies_house": [],
        "opencorporates": [],
        "propublica": [],
    }

    # GLEIF
    gleif_results = gleif_search_by_name(company_name)
    for record in gleif_results[:5]:
        attrs = record.get("attributes", {})
        entity = attrs.get("entity", {})
        results["gleif"].append({
            "lei": record.get("id"),
            "legal_name": entity.get("legalName", {}).get("name", ""),
            "website": entity.get("website", ""),
            "address": entity.get("legalAddress", {}),
        })

    # SEC EDGAR
    sec_results = sec_edgar_cik_lookup(name=company_name)
    for ticker in sec_results[:5]:
        results["sec_edgar"].append({
            "cik": ticker.get("cik_str"),
            "ticker": ticker.get("ticker"),
            "title": ticker.get("title"),
        })

    # Companies House
    ch_results = companies_house_search(company_name)
    for item in ch_results[:5]:
        results["companies_house"].append({
            "company_number": item.get("company_number"),
            "title": item.get("title"),
            "company_status": item.get("company_status"),
            "address": item.get("registered_office_address", {}),
        })

    return results


def enrich_unresolved_boards(limit: int = 100) -> int:
    """Try to resolve unresolved boards using registry data."""
    import sqlite3
    from company_dataset import store

    conn = store.connect(DB)
    conn.row_factory = sqlite3.Row

    # Get unresolved boards
    rows = conn.execute("""
        SELECT b.id, b.ats, b.code, c.company_name
        FROM boards b
        JOIN companies c ON c.primary_board_id = b.id
        JOIN identities i ON i.board_id = b.id
        WHERE i.status IN ('identity_unresolved', 'identity_mismatch')
          AND i.run = (SELECT MAX(run) FROM identities WHERE board_id = b.id)
        LIMIT ?
    """, (limit,)).fetchall()

    resolved = 0
    for row in rows:
        b = dict(row)
        print(f"Resolving {b['code']} ({b['ats']}) - {b['company_name']}")

        # Search registries
        results = resolve_company_identity(row["company_name"])

        # Check GLEIF for website matching
        for gleif in results["gleif"]:
            website = gleif.get("website", "")
            if website:
                # Check if domain matches board code or company name
                pass

        # SEC EDGAR - check for website in filings
        for sec in results.get("sec_edgar", []):
            cik = sec.get("cik")
            if cik:
                filings = sec_edgar_company_filings(cik)
                # Could extract website from filings

    return 0


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(prog="company_dataset.sources.company_registries")
    p.add_argument("--db", default="data/store.db")
    p.add_argument("--limit", type=int, default=100)
    a = p.parse_args()

    resolved = enrich_unresolved_boards(a.limit)
    print(f"Resolved: {resolved}")