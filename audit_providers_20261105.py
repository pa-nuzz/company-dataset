#!/usr/bin/env python3
"""Audit the 87 vendor-provider websites (run harvest_2026-11-04) whose website_url evidence
lacks a links_to_board attestation. For each board fetch the site, look for a link to its own
ATS board, and record a website_url evidence row with links_to_board text under run
audit_providers_2026-11-05. Nothing guessed: the board link must appear on the company's site.
"""
import re
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, "src")
from company_dataset import store
from company_dataset.enrichment import board_refs, parse_page

RUN = "audit_providers_2026-11-05"
DB = "data/store.db"
URLS = {
    "greenhouse": "https://job-boards.greenhouse.io/%s",
    "lever": "https://jobs.lever.co/%s",
    "ashby": "https://jobs.ashbyhq.com/%s",
    "breezy": "https://%s.breezy.hr",
    "smartrecruiters": "https://jobs.smartrecruiters.com/%s",
}
_tl = threading.local()
UA = {"User-Agent": "Mozilla/5.0 shared-research-crawler/1.0 (freeze-dataset; contact none)"}


def http():
    if getattr(_tl, "http", None) is None:
        import requests
        s = requests.Session()
        s.headers.update(UA)
        _tl.http = s
    return _tl.http


def tconn():
    if getattr(_tl, "db", None) is None:
        _tl.db = store.connect(DB)
    return _tl.db


def fetch(url, timeout=12):
    try:
        r = http().get(url, timeout=timeout, allow_redirects=True)
        if r.status_code != 200 or not r.text:
            return None
        return r.text[:3_000_000]
    except Exception:
        return None


def links_to_board(html, ats, code):
    if not html:
        return False
    try:
        page = parse_page(html)
        refs = {r.lower() for r in (board_refs(page) or [])}
    except Exception:
        refs = set()
    c = (code or "").casefold()
    if c and c in refs:
        return True
    frag = URLS.get(ats, "")
    target = re.sub(r"[^a-z0-9]", "", c.lower())
    for m in re.finditer(r'href=["\']([^"\']+)["\']', html, re.I):
        href = m.group(1)
        ref = re.sub(r"[^a-z0-9]", "", (urllib.parse.urlparse(href).path if href.lower().startswith("http") else href).casefold())
        if all(tok in ref for tok in (ats[:6], target)) or (target and (target in ref and (ats[:5] in ref))):
            return True
    return False


def record(board_id, site, ats, code, now):
    c = tconn()
    with c:
        c.execute("INSERT OR IGNORE INTO field_evidence (board_id, field, value, method, evidence_url, evidence_text, status, observed_at, run) VALUES (?,?,?,?,?,?,?,?,?)",
                  (board_id, "website_url", site, "provider_site_board_link", site, f"links_to_board:{ats}:{code}", "verified", now, RUN))
    return c.execute("SELECT changes()").fetchone()[0]


def main():
    conn = store.connect(DB)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cand = [(r["board_id"], r["value"], r["ats"], r["code"]) for r in conn.execute(
        """SELECT e.board_id, e.value, b.ats, b.code FROM field_evidence e JOIN boards b ON b.id=e.board_id
           WHERE e.run='harvest_2026-11-04' AND e.field='website_url'
             AND e.board_id NOT IN (SELECT board_id FROM field_evidence WHERE field='website_url'
                 AND (evidence_text LIKE '%links_to_board%' OR evidence_text LIKE '%domain%' OR evidence_text LIKE '%provider%'))""")]
    print(f"[audit] boards: {len(cand)}")
    added = 0
    with ThreadPoolExecutor(max_workers=12) as ex:
        def work(row):
            board_id, site, ats, code = row
            page = fetch(site)
            if links_to_board(page, ats, code):
                return (board_id, site, ats, code)
            if links_to_board(fetch(re.sub(r"/$", "", site) + "/careers"), ats, code):
                return (board_id, site, ats, code)
            return None
        for row in ex.map(work, cand):
            if row:
                added += record(row[0], row[1], row[2], row[3], now)
    print(f"[audit] +{added} links_to_board rows of {len(cand)}")


if __name__ == "__main__":
    main()
