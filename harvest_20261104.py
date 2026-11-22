#!/usr/bin/env python3
"""Harvest missing fields, real evidence only, offline cache first then measured network.

run label: harvest_2026-11-04
Stages (each prints progress every 500 items):
  offline   websites from verifications.provider_website + cached homepages linking the board
  network   bordered by measurement: 100-item speed on the first 100 targets, cut if > projected cap
  jobs      schema + endpoint fills with measurement
  filters   flag job-posting-text and foreign_ENTITY descriptions (demoted at populate)
  sr_suffix duplicate-name pairs csv
  examples  20 accepted samples per method
"""
import json
import sqlite3
import sys
import threading
import time
import urllib.parse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

sys.path.insert(0, "src")
from company_dataset import store
from company_dataset.description_rules import acceptable
from company_dataset.website_meta import og_description, twitter_description, jsonld_org_description, about_meta
from company_dataset.identity import _fold, registrable_domain
from company_dataset.normalize import clean_website

RUN = "harvest_2026-11-04"
DB = "data/store.db"
NOW = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
HTTP = None

ATS_HOSTS = {"boards.greenhouse.io", "job-boards.greenhouse.io", "jobs.ashbyhq.com", "jobs.lever.co",
             "jobs.smartrecruiters.com", "career.lever.co", "apply.workable.com"}

def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def has_verified_evidence(conn, board_id, field):
    return conn.execute("SELECT 1 FROM field_evidence WHERE board_id=? AND field=? AND status='verified' LIMIT 1",
                        (board_id, field)).fetchone() is not None

def record_evidence(conn, board_id, field, value, method, evidence_url, evidence_text, status="verified"):
    try:
        with conn:
            conn.execute("""INSERT OR IGNORE INTO field_evidence
                (board_id, field, value, method, evidence_url, evidence_text, status, observed_at, run)
                VALUES (?,?,?,?,?,?,?,?,?)""", (board_id, field, value, method, evidence_url, evidence_text, status, now(), RUN))
        return True
    except Exception as e:
        return False

def load_companies(conn):
    return [dict(r) for r in conn.execute(
        "SELECT id, primary_board_id, company_name, description, website_url, linkedin_url, status, missing_fields, ats_system, ats_code FROM companies")]

def target_boards(conn, field):
    boards = []
    colmap = {"website_url": "c.website_url = ''", "description": "(c.description = '' OR c.description IS NULL)",
              "linkedin_url": "c.linkedin_url = ''"}
    where = colmap.get(field)
    if not where:
        return boards
    for r in conn.execute(f"""SELECT b.id, b.ats, b.code FROM boards b
        JOIN companies c ON c.primary_board_id = b.id WHERE {where}"""):
        boards.append(dict(r))
    return boards

# ---------- OFFLINE: websites from vendor records + cached page board links ----------
def offline_websites(conn):
    # vendor records
    targets = defaultdict(list)  # board_id -> row
    for r in target_boards(conn, "website_url"):
        targets[r["id"]] = r
    added_vendor = added_cache = 0
    for board_id, url in conn.execute(
        "SELECT DISTINCT v.board_id, v.provider_website FROM verifications v WHERE v.provider_website LIKE 'http%'"):
        if board_id in targets:
            w = clean_website(url)
            if w and not has_verified_evidence(conn, board_id, "website_url"):
                rec = record_evidence(conn, board_id, "website_url", w, "vendor_provider_website", "", "vendor record")
                if rec: added_vendor += 1
    # cached pages: index summaries by board_ref
    ref_pages = defaultdict(list)
    for url, summary in conn.execute("SELECT url, summary FROM pages"):
        try: s = json.loads(summary)
        except Exception: continue
        if s.get("status") != "ok": continue
        refs = s.get("board_refs") or []
        if not refs: continue
        for ref in set(refs):
            if isinstance(ref, str) and ":" in ref:
                ref_pages[ref.casefold()].append((url, s.get("final_url") or url, s))
    print(f"[offline] provider steps: +{added_vendor} websites; indexed {len(ref_pages)} board refs from cache")
    # cached page linking board = domain proof
    web_targets = {r["id"]: r for r in target_boards(conn, "website_url")}
    for bid, r in web_targets.items():
        if has_verified_evidence(conn, bid, "website_url"): continue
        ref = f"{r['ats']}:{r['code'].casefold()}"
        for url, final_url, s in ref_pages.get(ref, []):
            host = urllib.parse.urlparse(final_url).netloc.lower() if final_url else ""
            if not host or host in ATS_HOSTS: continue
            dom = registrable_domain(host)
            if not dom: continue
            site = f"https://{dom}"
            if clean_website(site):
                if record_evidence(conn, bid, "website_url", site, "cached_homepage_board_link", url, f"links_to_board {ref}"):
                    added_cache += 1
                    break
    print(f"[offline] cached board-link: +{added_cache} websites")
    return added_vendor, added_cache

# ---------- OFFLINE: per-board summaries of description for boards w/ website ----------
def cache_by_board(conn):
    idx = defaultdict(list)
    for url, summary in conn.execute("SELECT url, summary FROM pages"):
        try: s = json.loads(summary)
        except Exception: continue
        if s.get("status") != "ok": continue
        for ref in set(s.get("board_refs") or []):
            if isinstance(ref, str) and ":" in ref:
                idx[ref.casefold()].append((url, s))
    return idx

def offline_descriptions(conn, ref_pages):
    ref_pages = ref_pages or cache_by_board(conn)
    boards = target_boards(conn, "description")
    added = 0
    for i, b in enumerate(boards, 1):
        if i % 500 == 0: print(f"[desc-offline] {i}/{len(boards)}")
        if has_verified_evidence(conn, b["id"], "description"): continue
        ref = f"{b['ats']}:{b['code'].casefold()}"
        for url, s in ref_pages.get(ref, []):
            host = urllib.parse.urlparse(url or "").netloc.lower()
            if not host or host in ATS_HOSTS: continue
            desc = (s.get("description") or "").strip()
            if desc and acceptable("homepage_meta", desc, None):
                if record_evidence(conn, b["id"], "description", desc, "homepage_meta", url, "homepage og/twitter/jsonld meta"):
                    added += 1
                    break
    print(f"[desc-offline] +{added} descriptions from cache")
    # linkedin from same cached homepage
    for i, b in enumerate(target_boards(conn, "linkedin_url"), 1):
        if i % 500 == 0: print(f"[li-offline] {i}")
        if has_verified_evidence(conn, b["id"], "linkedin_url"): continue
        ref = f"{b['ats']}:{b['code'].casefold()}"
        for url, s in ref_pages.get(ref, []):
            host = urllib.parse.urlparse(url or "").netloc.lower()
            if host in ATS_HOSTS or not host: continue
            slugs = s.get("linkedin_slugs") or []
            if slugs:
                slug = slugs[0] if isinstance(slugs[0], str) else (slugs[0] or {}).get("slug", "")
                if slug:
                    if record_evidence(conn, b["id"], "linkedin_url", f"https://www.linkedin.com/company/{slug}/", "homepage_linkedin", url, "linkedin on own homepage"):
                        break
    return added

# ---------- NETWORK ----------
_tl = threading.local()

def http_client():
    import requests
    if getattr(_tl, "c", None) is None:
        from requests.adapters import HTTPAdapter
        c = requests.Session()
        c.headers.update({"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 pvt-companies-dataset/1.0"})
        _tl.c = c
    return _tl.c

def fetch(url, timeout=10):
    try:
        resp = http_client().get(url, timeout=timeout, allow_redirects=True)
        ctype = resp.headers.get("content-type", "")
        if resp.status_code != 200 or "html" not in ctype:
            return None
        return resp.text[:3000000]
    except Exception:
        return None

def measure(targets, sample_n, fn, cap_s):
    """Run fn on the first sample_n targets (a list) measuring wall time; returns rate + seconds per item."""
    if not targets:
        return None
    t0 = time.time()
    hits = 0
    with ThreadPoolExecutor(max_workers=16) as ex:
        for is_hit in ex.map(fn, targets[:sample_n]):
            hits += bool(is_hit)
    dur = time.time() - t0
    per = dur / max(1, len(targets[:sample_n]))
    return {"sample_count": len(targets[:sample_n]), "sample_hits": hits, "rate": hits / max(1, len(targets[:sample_n])),
            "s_per_item": per, "projected_s": per * len(targets), "cap_s": cap_s}

def print_samples(samples, label, n=15):
    print(f"[sample {label}]")
    for s in samples[:n]:
        print(" ", s)

def _extract_desc(b, html, ref=None):
    """Accept og > twitter > jsonld, filtered by acceptable()."""
    for method, val in (("og_description", og_description(html)), ("twitter_description", twitter_description(html)),
                        ("jsonld_org_description", jsonld_org_description(html))):
        if not val:
            continue
        if acceptable(method, val, None):
            return method, val
    return None

def stage_meta(conn):
    """og/twitter/jsonld descriptions + linkedin for companies WITH a website missing those fields."""
    targets = []
    for r in conn.execute("""SELECT b.id, b.ats, b.code, c.website_url FROM companies c JOIN boards b ON b.id=c.primary_board_id
        WHERE (c.description = '' OR c.description IS NULL) AND c.website_url != '' AND b.ats IN ('greenhouse','lever','ashby','smartrecruiters','breezy')"""):
        targets.append(dict(r))
    print(f"[meta] targets: {len(targets)} (cap 10 min)")
    m = measure(targets, 100, lambda t: stage_meta_one(t), 600)
    if not m:
        print("[meta] no targets"); return
    print(f"[meta] sample: hits={m['sample_hits']}/{m['sample_count']} rate={m['rate']:.0%} measured={m['s_per_item']*100:.2f}s/item projected={m['projected_s']/60:.1f}min cap=10min")
    if m["projected_s"] > 600:
        cut = max(1, int(600 / m["s_per_item"]))
        print(f"[meta] CUT scope to {cut} of {len(targets)} to respect 10-min cap")
        targets = targets[:cut]
    done = 0
    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = {ex.submit(stage_meta_one, t): t for t in targets}
        for f in as_completed(futs):
            done += 1
            if done % 500 == 0:
                print(f"[meta] {done}/{len(targets)}")
    print(f"[meta] processed {done}")

def stage_meta_one(t):
    dom = registrable_domain(t["website_url"]) or ""
    if not dom:
        return False
    html = fetch(f"https://{dom}", timeout=10)
    if not html:
        html = fetch(t["website_url"], timeout=10)
    if not html:
        return False
    conn = store.connect(DB)
    ev_url = f"https://{dom}"
    added = False
    if not has_verified_evidence(conn, t["id"], "description"):
        got = _extract_desc(t, html)
        if got:
            method, val = got
            if record_evidence(conn, t["id"], "description", val, method, ev_url, "homepage meta"):
                added = True
    if not has_verified_evidence(conn, t["id"], "linkedin_url"):
        from company_dataset.website_meta import _first_linkedin
        li = _first_linkedin(html, ev_url)
        if li and record_evidence(conn, t["id"], "linkedin_url", li, "homepage_linkedin", ev_url, "linkedin on own homepage"):
            added = True
    conn.close()
    return added

def stage_about(conn):
    """/about page meta for boards with website still missing desc."""
    targets = [dict(r) for r in conn.execute("""SELECT b.id, b.ats, b.code, c.website_url FROM companies c JOIN boards b ON b.id=c.primary_board_id
        WHERE (c.description = '' OR c.description IS NULL) AND c.website_url != '' AND b.ats IN ('greenhouse','lever','ashby','smartrecruiters','breezy')""")]
    print(f"[about] targets: {len(targets)} (cap 10 min)")
    m = measure(targets, 100, stage_about_one, 600)
    if not m:
        print("[about] none"); return
    print(f"[about] sample: rate={m['rate']:.0%} projected={m['projected_s']/60:.1f}min cap=10min")
    if m["projected_s"] > 600:
        cut = max(1, int(600 / m["s_per_item"]))
        print(f"[about] CUT to {cut}"); targets = targets[:cut]
    done = 0
    with ThreadPoolExecutor(max_workers=16) as ex:
        for _ in ex.map(stage_about_one, targets):
            done += 1
            if done % 500 == 0:
                print(f"[about] {done}/{len(targets)}")
    print(f"[about] processed {done}")

def stage_about_one(t):
    dom = registrable_domain(t["website_url"]) or ""
    if not dom:
        return False
    html = fetch(f"https://{dom}/about", timeout=10)
    if not html:
        html = fetch(f"https://{dom}/company", timeout=10)
    if not html:
        return False
    conn = store.connect(DB)
    desc, li = about_meta(f"https://{dom}/about", html)
    added = False
    if desc and not has_verified_evidence(conn, t["id"], "description"):
        if record_evidence(conn, t["id"], "description", desc, "about_page_meta", f"https://{dom}/about", "about page meta"):
            added = True
    conn.close()
    return added

_TLD = (".com", ".io", ".co", ".ai")

def dom_guesses(name, code):
    base = _fold(name or code) or code
    slug = "".join(ch for ch in base.casefold().replace(' ', '') if ch.isalnum())
    words = "".join(ch for ch in "".join(base.split()).casefold() if ch.isalnum())
    cands = {slug, words} - {""}
    out = []
    for c in cands:
        for tld in _TLD:
            out.append(f"https://www.{c}{tld}")
    return out[:8]

def stage_guess(conn):
    """LAST resort domain guesses; must link to board."""
    targets = [dict(r) for r in conn.execute("""SELECT b.id, b.ats, b.code, c.company_name FROM companies c JOIN boards b ON b.id=c.primary_board_id
        WHERE c.website_url = '' AND b.ats IN ('greenhouse','lever','ashby','smartrecruiters','breezy')""")]
    print(f"[guess] targets: {len(targets)} (cap 15 min)")
    m = measure(targets, 100, stage_guess_one, 480) if False else measure(targets, 100, lambda t: stage_guess_one(t), 480)
    if not m:
        print("[guess] none"); return
    _DATA = None
    print(f"[guess] sample: hits={m['sample_hits']}/{m['sample_count']} rate={m['rate']:.0%} projected={m['projected_s']/60:.1f}min cap-cut=8min")
    print_samples([t for t in targets[:15]], "guess sample targets", 5)
    if m["rate"] < 0.05:
        print("[guess] rate < 5% - NOT running in full"); return
    if m["projected_s"] > 480:
        cut = max(1, int(480 / m["s_per_item"]))
        print(f"[guess] CUT scope to {cut} of {len(targets)} to respect 15-min cap")
        targets = targets[:cut]
    done = added = 0
    with ThreadPoolExecutor(max_workers=24) as ex:
        futs = {ex.submit(stage_guess_one, t): t for t in targets}
        for f in as_completed(futs):
            done += 1
            if f.result():
                added += 1
            if done % 500 == 0:
                print(f"[guess] {done}/{len(targets)} (+{added})")
    print(f"[guess] processed {done} accepted {added}")

def stage_guess_one(t):
    conn = store.connect(DB)
    url = urllib.parse.quote(t["code"], safe='')
    ref = f"{t['ats']}:{t['code'].casefold()}"
    for cand in dom_guesses(t["company_name"], t["code"]):
        html = fetch(cand, timeout=10)
        if not html:
            continue
        from company_dataset.enrichment import Page, board_refs, parse_page
        page = parse_page(html)
        refs = board_refs(page)[:50]
        if ref in [x.casefold() for x in refs]:
            dom = registrable_domain(cand)
            site = f"https://{dom}"
            if not clean_website(site):
                continue
            if has_verified_evidence(conn, t["id"], "website_url"):
                conn.close(); return False
            ok = record_evidence(conn, t["id"], "website_url", site, "domain_guess_homepage", cand, f"links_to_board {ref}")
            desc = _extract_desc(t, html)
            if desc and not has_verified_evidence(conn, t["id"], "description"):
                method, val = desc
                record_evidence(conn, t["id"], "description", val, method, cand, "homepage meta")
            from company_dataset.website_meta import _first_linkedin
            li = _first_linkedin(html, cand)
            if li and not has_verified_evidence(conn, t["id"], "linkedin_url"):
                record_evidence(conn, t["id"], "linkedin_url", li, "homepage_linkedin", cand, "linkedin on own homepage")
            conn.close()
            return ok
    conn.close()
    return False

JOBS_URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{code}/jobs",
    "lever": "https://api.lever.co/v0/postings/{code}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{code}",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{code}/postings?limit=100",
    "breezy": "https://{code}.breezy.hr/json",
}

def stage_jobs(conn):
    ensuring_jobs_table(conn)
    targets = [dict(r) for r in conn.execute("""SELECT b.id, b.ats, b.code FROM boards b JOIN companies c ON c.primary_board_id=b.id
        WHERE b.ats IN ('greenhouse','lever','ashby','smartrecruiters','breezy')
        AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.board_id=b.id)""")]
    print(f"[jobs] targets: {len(targets)} (cap 10 min)")
    m = measure(targets, 60, stage_jobs_one, 600)
    if not m:
        print("[jobs] none"); return
    print(f"[jobs] sample: rate={m['rate']:.0%} projected={m['projected_s']/60:.1f}min cap=10min")
    if m["projected_s"] > 600:
        cut = max(1, int(600 / m["s_per_item"]))
        print(f"[jobs] CUT to {cut}"); targets = targets[:cut]
    done = 0
    with ThreadPoolExecutor(max_workers=16) as ex:
        for _ in ex.map(stage_jobs_one, targets):
            done += 1
            if done % 500 == 0:
                print(f"[jobs] {done}/{len(targets)}")
    n = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    for r in conn.execute("SELECT b.ats, COUNT(DISTINCT j.board_id) FROM jobs j JOIN boards b ON b.id=j.board_id GROUP BY 1"):
        print(f"[jobs] {r[0]}: boards={r[1]}")
    print(f"[jobs] rows: {n}")

def ensuring_jobs_table(conn):
    with conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY,
            board_id INTEGER NOT NULL REFERENCES boards(id),
            title TEXT NOT NULL,
            department TEXT,
            location TEXT,
            job_url TEXT,
            fetched_at TEXT NOT NULL,
            evidence_url TEXT,
            run TEXT NOT NULL,
            UNIQUE (board_id, title, location)
        )""")

def stage_jobs_one(t):
    conn = store.connect(DB)
    code = urllib.parse.quote(t["code"], safe='')
    endpoint = JOBS_URLS[t["ats"]].format(code=code)
    try:
        resp = http_client().get(endpoint, timeout=10)
        if resp.status_code != 200:
            conn.close(); return False
        data = resp.json()
    except Exception:
        conn.close(); return False
    rows = []
    if t["ats"] == "greenhouse":
        ads = data.get("jobs") or []
        rows = [(j.get("title") or "", "", j.get("location") or "", j.get("absolute_url") or "") for j in ads[:100]]
    elif t["ats"] == "lever":
        ads = data if isinstance(data, list) else []
        rows = [(j.get("text") or "", (j.get("categories") or {}).get("department") or "", (j.get("categories") or {}).get("location") or "", j.get("hostedUrl") or "") for j in ads[:100]]
    elif t["ats"] == "ashby":
        ads = (data.get("jobs") or []) if isinstance(data, dict) else []
        rows = [(j.get("title") or "", j.get("department") or "", j.get("location") or "", j.get("jobUrl") or "") for j in ads[:100]]
    elif t["ats"] == "smartrecruiters":
        rows = []
        offset = 0
        limit = 100
        while True:
            endpoint = f"https://api.smartrecruiters.com/v1/companies/{urllib.parse.quote(t['code'], safe='')}/postings?limit={limit}&offset={offset}"
            try:
                resp = http_client().get(endpoint, timeout=10)
                if resp.status_code != 200:
                    break
                data = resp.json()
            except Exception:
                break
            ads = (data.get("content") or []) if isinstance(data, dict) else []
            if not ads:
                break
            for j in ads:
                rows.append((j.get("name") or "",
                            (j.get("department") or {}).get("label") or "",
                            f"{(j.get('location') or {}).get('city','')} {(j.get('location') or {}).get('country','')}".strip(),
                            f"https://jobs.smartrecruiters.com/{t['code']}/{j.get('id')}" if j.get("id") else ""))
            if len(ads) < limit:
                break
            offset += limit
    elif t["ats"] == "breezy":
        ads = data if isinstance(data, list) else []
        rows = [(j.get("name") or "", "", j.get("location", {}).get("name") if isinstance(j.get("location"), dict) else (j.get("location") or ""), j.get("url") or "") for j in ads[:100]]
    with conn:
        for title, dep, loc, url in rows:
            if title:
                try:
                    conn.execute("""INSERT OR IGNORE INTO jobs (board_id, title, department, location, job_url, fetched_at, evidence_url, run)
                                    VALUES (?,?,?,?,?,?,?,?)""",
                                 (t["id"], title[:300], dep or None, loc or None, url or None, now(), endpoint, RUN))
                except Exception:
                    pass
    conn.close()
    return bool(rows)

def network_stage(conn, name):
    if name == "meta": stage_meta(conn)
    elif name == "about": stage_about(conn)
    elif name == "guess": stage_guess(conn)
    elif name == "jobs": stage_jobs(conn)
    else:
        print("unknown stage")

if __name__ == "__main__":
    conn = store.connect(DB)
    ref_pages = cache_by_board(conn)

    stage = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "offline"
    if stage == "offline":
        offline_websites(conn)
        offline_descriptions(conn, ref_pages)
    elif stage in ("meta", "about", "guess", "jobs"):
        network_stage(conn, stage)


