#!/usr/bin/env python3
"""GH board pages' og:description (boards.greenhouse.io/<code>) -> description evidence.
run label: gh_og_2026-11-05. Only records when og:description passes describes_company."""
import re
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, "src")
from company_dataset import store
from company_dataset.website_meta import og_description
from company_dataset.description_rules import describes_company
from company_dataset.enrichment import clean_description

RUN = "gh_og_2026-11-05"
DB = "data/store.db"
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


def fetch(url):
    try:
        r = http().get(url, timeout=12)
        if r.status_code != 200 or not r.text:
            return None
        return r.text[:2_000_000]
    except Exception:
        return None


def targets():
    c = tconn()
    return [dict(r) for r in c.execute(
        """SELECT c.primary_board_id AS board_id, c.company_name AS name, b.code
           FROM companies c JOIN boards b ON b.id=c.primary_board_id
           WHERE c.status='incomplete' AND (c.description='' OR c.description IS NULL) AND b.ats='greenhouse'""")]


def record(b, cleaned, url, evidence_text):
    c = tconn()
    with c:
        c.execute("INSERT OR IGNORE INTO field_evidence (board_id, field, value, method, evidence_url, evidence_text, status, observed_at, run) VALUES (?,?,?,?,?,?,?,?,?)",
                  (b["board_id"], "description", cleaned, "board_page_meta", url, evidence_text, "verified",
                   time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), RUN))


def work(b):
    try:
        code = urllib.parse.quote(b["code"], safe="")
        for host in ("https://boards.greenhouse.io/%s", "https://job-boards.greenhouse.io/%s"):
            html = fetch(host % code)
            if not html:
                continue
            og = og_description(html)
            if not og:
                continue
            cleaned = clean_description(og)
            if cleaned and describes_company(cleaned, b["name"]):
                record(b, cleaned, host % code, og[:2000])
                return True
    except Exception:
        pass
    return False


if __name__ == "__main__":
    rows = targets()
    print(f"[gh-og] targets: {len(rows)}")
    sample = rows[:100]
    t0 = time.time(); sample_hits = 0
    with ThreadPoolExecutor(max_workers=16) as ex:
        for ok in ex.map(work, sample):
            sample_hits += bool(ok)
    dt = time.time() - t0
    per = dt / max(1, len(sample))
    proj = per * len(rows) / 60
    print(f"[gh-og] sample: rate={sample_hits/max(1,len(sample)):.0%} projected={proj:.1f}min")
    if sample_hits < 5:
        print("[gh-og] rate too low; not scaling")
        sys.exit(0)
    added = sample_hits
    with ThreadPoolExecutor(max_workers=16) as ex:
        for i, ok in enumerate(ex.map(work, rows[100:])):
            added += bool(ok)
            if (i + 1) % 500 == 0:
                print(f"[gh-og] {i+1}/{len(rows)-100} +{added}", flush=True)
    print(f"[gh-og] DONE total +{added}")
