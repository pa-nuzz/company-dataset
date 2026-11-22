#!/usr/bin/env python3
"""Mine company About-sections out of the job ads that are ALREADY known to us.

run label: jobad_about_2026-11-05
Sources of text (all public evidence, one fetch mode each):
  smartrecruiters  job pages (jobs.job_url)  -> section id="st-companyDescription"
  lever            job pages (jobs.job_url)  -> visible "About <company>" paragraph
  greenhouse       boards-api .../jobs?content=true -> job body "About <company>" blocks
  breezy           job pages (jobs.job_url)  -> visible "About <company>" paragraph
Every accepted value must pass describes_company() (company as subject of the open sentence).
Capabilities: idempotent via UNIQUE(board_id, field, value, method, run).
"""
import re
import sys
import time
import urllib.parse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, "src")
from company_dataset import store
from company_dataset.description_rules import describes_company
from company_dataset.enrichment import clean_description
from company_dataset.website_meta import _LINKEDIN_RE

RUN = "jobad_about_2026-11-05"
DB = "data/store.db"
UA = {"User-Agent": "Mozilla/5.0 shared-research-crawler/1.0 (freeze-dataset; contact none)"}
_tl = threading.local()

def http():
    import requests
    if getattr(_tl, "s", None) is None:
        from requests.adapters import HTTPAdapter
        s = requests.Session()
        s.headers.update(UA)
        _tl.s = s
    return _tl.s

def tconn():
    if getattr(_tl, "db", None) is None:
        _tl.db = store.connect(DB)
    return _tl.db

def fetch(url, timeout=12):
    try:
        r = http().get(url, timeout=timeout, allow_redirects=True)
        if r.status_code != 200:
            return None
        return r.text
    except Exception:
        return None

def has_desc(conn, board_id):
    c = tconn()
    return c.execute('SELECT 1 FROM field_evidence WHERE board_id=? AND field=\'description\''
                     ' AND status=\'verified\' AND value != \'\' AND run=\'jobad_about_2026-11-05\' LIMIT 1', (board_id,)).fetchone() is not None

def strip_tags(html):
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", "\n", html)
    import html as H
    return H.unescape(html)


def _is_job_ad_boilerplate(text: str) -> bool:
    """Check if text is job-ad boilerplate (requirements, responsibilities, etc.) rather than company description."""
    if not text:
        return True
    t = text.lower()
    boilerplate_patterns = [
        r"\b(responsibilities|requirements|qualifications|duties|what you['\u2019]?ll do|what we['\u2019]?re looking for|you will|you['\u2019]?ll be)\b",
        r"\b(years of experience|degree in|bachelor|master|phd|certification|proficient in|experience with)\b",
        r"\b(salary|compensation|benefits|bonus|stock options|equity|401k|health insurance)\b",
        r"\b(apply now|apply here|click to apply|send resume|submit application)\b",
    ]
    for pat in boilerplate_patterns:
        if re.search(pat, t):
            return True
    return False


def record(conn, b, text, method, evidence_url, evidence_text) -> bool:  # conn arg kept for compat
    cleaned = clean_description(text)
    if not cleaned:
        return False
    if not describes_company(cleaned, b["name"]):
        # Special handling for SR: the st-companyDescription section is explicitly about the company
        # even if the company name isn't in the first sentence. Accept if substantial and not boilerplate.
        if method in ("sr_jobad_about", "jobad_about_section_deep"):
            if not cleaned.strip() or len(cleaned.strip()) < 50 or _is_job_ad_boilerplate(cleaned):
                return False
        else:
            # The ad may use the company's fuller trading name ("2A Consulting" for identity "2A").
            # Accept only when the identity name's tokens are all inside the subject AND the subject
            # starts with the identity name (no different-entity case like IFS vs IGT1).
            first = re.split(r"(?<=[.!?])\s", cleaned.strip(), maxsplit=1)[0]
            m = re.match(
                r"^(?:\s*about\s*)?(?:founded|established|since|based|headquartered)?\s*(?:in\s+\d{4},\s*)?((?:[A-Z0-9][\w&\'\u2019\.-]*[\s-]*)+)",
                first)
            if not m:
                return False
            alias = m.group(1).strip()
            alias_tok = [t.lower() for t in re.findall(r"[A-Za-z0-9]+", alias)]
            name_tok = [t.lower() for t in re.findall(r"[A-Za-z0-9]+", b["name"])]
            if not name_tok or not alias or not alias.lower().startswith(name_tok[0]) or not all(t in alias_tok for t in name_tok):
                return False
            if not describes_company(cleaned, alias):
                return False
    try:
        c = tconn()
        with c:
            c.execute("""INSERT OR IGNORE INTO field_evidence
                (board_id, field, value, method, evidence_url, evidence_text, status, observed_at, run)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (b["board_id"], "description", cleaned, method, evidence_url, evidence_text,
                 "verified", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), RUN))
        return True
    except Exception:
        return False


def about_block(text, name):
    """Sentences following an About-style heading; drops the bare heading themselves."""
    tokens = [t for t in re.findall(r"[a-z0-9]+", (name or "").lower()) if len(t) >= 2]
    if not tokens:
        return ""
    pat = re.compile(r"(?i)\babout\s+us\b|\babout\s+the company\b|\babout\s+"
                     + "|".join(re.escape(t) for t in tokens[:2]))
    m = pat.search(text)
    if not m:
        return ""
    tail = text[m.start():m.end() + 900]
    tail = re.sub(r"(?i)^\s*about\b[^\n.]{3,45}?\n\s*\n", "", tail, count=1).strip()
    tail = re.sub(r"(?i)^\s*about\s*(us|the company|our team|our story|the agency)\b\s*[:,.-]?\s*", "", tail, count=1)
    sents = re.split(r"(?<=[.!?])\s", tail)
    keep = []
    for s in sents:
        if re.search(r"(?i)\b(apply|responsibilities|you will|your role|perks\b|we offer|qualifications|"
                     r"requirements|benefits\b|what we['’]?re looking for|salary|hourly wage)\b", s):
            break
        if s.strip():
            keep.append(s.strip())
        if len(" ".join(keep)) > 600:
            break
    return " ".join(keep)


def do_sr(conn, b, u):
    html = fetch(u) if u else None
    if not html:
        return False
    m = re.search(r'(?is)<section[^>]+id="st-companyDescription".*?</section>', html)
    if not m:
        return False
    return record(conn, b, strip_tags(m.group(0)), "jobad_about_section_deep", u, "SR job page company description")


def do_lever(conn, b, u):
    html = fetch(u) if u else None
    if not html:
        return False
    # The About-company blurb lives in the posting body: prefer the JSON-LD JobPosting.description,
    # fall back to visible page text.
    texts = []
    for m in re.finditer(r'(?is)<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html):
        try:
            import json as _json
            data = _json.loads(m.group(1).strip() or "null")
        except Exception:
            continue
        for node in (data if isinstance(data, list) else [data]):
            if isinstance(node, dict) and isinstance(node.get("description"), str):
                texts.append(strip_tags(node["description"]))
            elif isinstance(node, dict) and isinstance(node.get("@graph"), list):
                for sub in node["@graph"]:
                    if isinstance(sub, dict) and isinstance(sub.get("description"), str):
                        texts.append(strip_tags(sub["description"]))
    texts.append(strip_tags(html))
    for text in texts:
        block = about_block(text, b["name"])
        if block and record(conn, b, block, "lever_jobad_about", u, "Lever job ad About-section"):
            return True
    return False


def do_gh(conn, b, u=None):
    gh = f"https://boards-api.greenhouse.io/v1/boards/{urllib.parse.quote(b['code'], safe='')}/jobs?content=true"
    try:
        r = http().get(gh, timeout=15)
        if r.status_code != 200:
            return False
        jobs = r.json().get("jobs", [])
    except Exception:
        return False
    import html as H
    for j in jobs[:50]:
        c = j.get("content") or ""
        if not c:
            continue
        text = about_block(strip_tags(H.unescape(c)), b["name"])
        if text and record(conn, b, text, "gh_content_about", gh, "Greenhouse job ad content About"):
            return True
    return False


def do_breezy(conn, b, u):
    html = fetch(u) if u else None
    if not html:
        return False
    return record(conn, b, about_block(strip_tags(html), b["name"]), "breezy_jobad_about", u, "Breezy job ad About-section")


def do_ashby(conn, b, u=None):
    """Ashby posting-api job-board/<code>: each job's descriptionHtml may carry an About <company> block."""
    if has_desc(conn, b["board_id"]):
        return False
    code = urllib.parse.quote(b["code"], safe="")
    url = f"https://api.ashbyhq.com/posting-api/job-board/{code}"
    try:
        r = http().get(url, timeout=12)
        if r.status_code != 200:
            return False
        data = r.json()
    except Exception:
        return False
    jobs = (data.get("jobs") or []) if isinstance(data, dict) else []
    for j in jobs[:10]:
        jh = j.get("descriptionHtml") or ""
        if not jh:
            continue
        try:
            eb = about_block(strip_tags(jh), b["name"])
        except Exception:
            eb = ""
        if not eb:
            continue
        cleaned = clean_description(eb)
        if cleaned and describes_company(cleaned, b["name"]):
            return record(conn, b, cleaned, "ashby_jobad_about", url, strip_tags(jh)[:4000])
    return False

if __name__ == "__main__":
    conn = store.connect(DB)
    targets = [dict(r) for r in conn.execute("""
        SELECT c.primary_board_id AS board_id, c.company_name AS name, b.ats, b.code
        FROM companies c JOIN boards b ON b.id = c.primary_board_id
        WHERE c.status='incomplete' AND (c.description = '' OR c.description IS NULL)
          AND b.ats IN ('greenhouse','lever','ashby','smartrecruiters','breezy')""")]
    print("targets:", len(targets))
    from collections import Counter
    ats_counts = Counter(t["ats"] for t in targets)
    print("by ats:", dict(ats_counts))

    urls_by_board = {}
    for r in conn.execute("SELECT board_id, job_url FROM jobs WHERE job_url LIKE 'http%' ORDER BY board_id, id"):
        urls_by_board.setdefault(r[0], []).append(r[1])

    per_ats = {"smartrecruiters": [], "lever": [], "breezy": [], "greenhouse": [], "ashby": []}
    for t in targets:
        if has_desc(conn, t["board_id"]):
            continue
        if t["ats"] in per_ats:
            per_ats[t["ats"]].append(t)
    for k, v in per_ats.items():
        print(f"  remaining {k}: {len(v)}")

    FN = {"smartrecruiters": do_sr, "lever": do_lever, "greenhouse": do_gh, "breezy": do_breezy, "ashby": do_ashby}
    for ats, rows in per_ats.items():
        if not rows:
            continue
        fn = FN[ats]
        if ats != "greenhouse":
            rows = [t for t in rows if t["board_id"] in urls_by_board]
        print(f"[{ats}] workload: {len(rows)}")
        t0 = time.time()
        done = added = 0
        with ThreadPoolExecutor(max_workers=16) as ex:
            def run(t, kind):
                try:
                    if kind == "gh":
                        return 1 if fn(conn, t, None) else 0
                    urls = urls_by_board.get(t["board_id"], [])
                    for u in urls[:3]:
                        if fn(conn, t, u):
                            return 1
                    return 0
                except Exception:
                    return 0
            futs = {ex.submit(run, t, "gh" if ats == "greenhouse" else "u"): t for t in rows}
            for f in as_completed(futs):
                done += 1
                if f.result():
                    added += 1
                if done % 100 == 0:
                    rate = time.time() - t0
                    print(f"[{ats}] {done}/{len(rows)} +{added} ({rate:.0f}s, {rate/max(1,done):.2f}s/b)")
        print(f"[{ats}] DONE {done} boards, +{added} accepted ({time.time()-t0:.0f}s)")
