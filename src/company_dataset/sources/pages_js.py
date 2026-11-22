#!/usr/bin/env python3
"""Headless-browser fetch for JS-shell/bot-walled homepages.

Targets the ~1,500 companies whose homepages returned bot-walled (403) or
empty/JS-shell responses with requests. Uses Playwright/Chromium to render
and extract og:description, twitter:description, json-ld, and about meta.

run label: pages_js_2026-11-05
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlparse

from company_dataset import store
from company_dataset.website_meta import og_description, twitter_description, jsonld_org_description, about_meta

from company_dataset.models import SourceMeta

META = SourceMeta(
    name="pages_js_2026-11-05",
    description="Headless-browser fetch for JS-shell/bot-walled homepages; extracts og/twitter/jsonld/about meta descriptions",
    download_url="",
    filename="tokens.csv",
    license="Internal tool",
    license_url="",
    license_note="Playwright/Chromium renders pages; only meta descriptions extracted",
    attribution="Playwright/Chromium",
    downloadable=False,
)
DB = "data/store.db"


def _load_targets(sample: int = 0) -> list[dict]:
    """Load companies with missing descriptions that have a website but no JS-rendered fetch yet."""
    conn = store.connect(DB)
    conn.row_factory = sqlite3.Row

    # Find incomplete companies with website but no description, and no existing pages_js evidence
    rows = conn.execute("""
        SELECT c.primary_board_id AS board_id, c.company_name AS name, c.website_url
        FROM companies c
        WHERE c.status = 'incomplete'
          AND c.description = ''
          AND c.website_url != ''
          AND c.website_url IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM field_evidence e
              WHERE e.board_id = c.primary_board_id
                AND e.field = 'description'
                AND e.run = 'pages_js_2026-11-05'
          )
    """).fetchall()
    conn.close()

    targets = [dict(r) for r in rows]
    if sample > 0 and len(targets) > sample:
        targets = random.sample(targets, sample)
    return targets


def _fetch_with_playwright(url: str, timeout: int = 60000) -> str | None:
    """Fetch a URL with Playwright and return the rendered HTML."""
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 720},
            )
            page = context.new_page()
            page.set_default_timeout(60000)

            # Block unnecessary resources for speed
            page.route("**/*.{png,jpg,jpeg,gif,svg,woff,woff2,css,js}", lambda route: route.abort())

            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            if response and response.status >= 400:
                return None

            # Wait a bit for JS to render
            page.wait_for_timeout(3000)

            html = page.content()
            browser.close()
            return html
    except Exception as e:
        print(f"  [pages_js] Playwright error for {url}: {e}")
        return None


def _extract_meta(url: str, html: str) -> dict[str, str | None]:
    """Extract all meta descriptions from HTML."""
    results = {}
    results["og_description"] = og_description(html)
    results["twitter_description"] = twitter_description(html)
    results["jsonld_org_description"] = jsonld_org_description(html)
    about_desc, _ = about_meta(url, html)
    results["about_meta"] = about_desc
    return results


def _record_evidence(conn, board_id: int, field: str, value: str, method: str, evidence_url: str, evidence_text: str, now: str) -> bool:
    try:
        with conn:
            conn.execute("""INSERT OR IGNORE INTO field_evidence
                (board_id, field, value, method, evidence_url, evidence_text, status, observed_at, run)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (board_id, field, value, method, evidence_url, evidence_text[:5000],
                 "verified", now, META.name))
        return True
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(prog="company_dataset.sources.pages_js")
    parser.add_argument("--db", default=DB)
    parser.add_argument("--sample", type=int, default=0, help="Sample N targets (0 = all)")
    parser.add_argument("--workers", type=int, default=4, help="Parallel workers")
    a = parser.parse_args()

    conn = store.connect(a.db)
    conn.row_factory = sqlite3.Row

    targets = _load_targets(a.sample)
    print(f"[pages_js] {len(targets)} targets to fetch")

    if a.sample == 0 and len(targets) > 100:
        # For full run, sample first 100 for measurement
        sample_targets = random.sample(targets, min(100, len(targets)))
        print(f"[pages_js] Measuring on {len(sample_targets)} sample targets...")
    else:
        sample_targets = targets

    # Measure sample
    sample_ok = 0
    for i, t in enumerate(sample_targets):
        url = t["website_url"]
        if not url:
            continue
        html = _fetch_with_playwright(url)
        if not html:
            continue
        meta = _extract_meta(url, html)
        for method, value in meta.items():
            if value and value.strip():
                sample_ok += 1
                break
        if (i + 1) % 20 == 0:
            print(f"  [pages_js] sample {i+1}/{len(sample_targets)} ok={sample_ok}")

    sample_rate = sample_ok / max(1, len(sample_targets))
    print(f"[pages_js] Sample rate: {sample_rate:.1%} ({sample_ok}/{len(sample_targets)})")

    if sample_rate < 0.05:
        print("[pages_js] Rate < 5% - NOT scaling to full run")
        return

    # Full run
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    added = 0
    for t in targets:
        url = t["website_url"]
        if not url:
            continue
        html = _fetch_with_playwright(url)
        if not html:
            continue
        meta = _extract_meta(url, html)
        for method, value in meta.items():
            if value and value.strip():
                cleaned = value.strip()
                _record_evidence(conn, t["board_id"], "description", cleaned, method, url, html[:5000], time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
                added += 1
                break
        if added % 50 == 0:
            print(f"[pages_js] processed {added}...")

    print(f"[pages_js] DONE: added {added} descriptions from {len(targets)} targets")


if __name__ == "__main__":
    main()