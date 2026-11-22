"""A description for every live board that lacks one, from the most trustworthy text available.

    python -m company_dataset.fallback_descriptions --db data/store.db --run descriptions_fallback_2026-11-03 --from-runs fullrun,...

Stages, in order. A stage only answers when its text passes the same rules as every other description:
  1. the vendor's own record: board info, SmartRecruiters' company description, or the "About us"
     section of a job ad on Greenhouse, Lever or Ashby;
  2. meta descriptions on the company's homepage, /about and /careers, and on the hosted board;
  3. the first clean paragraph of /about, /careers or the homepage;
  4. text on the hosted board.
Paragraphs must name the company, since a page's first long paragraph is often a cookie notice or a
slogan. A title or heading alone is never used: "Job openings at Acme" is not a description.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from urllib.parse import quote

from company_dataset import store
from company_dataset.config import FAST_PER_HOST_INTERVAL, FAST_WORKERS
from company_dataset.description_rules import acceptable
from company_dataset.enrich import PARKED_RE
from company_dataset.enrichment import ENGLISH_HEADERS, clean_description, homepage_description, parse_page
from company_dataset.export import _latest_identity_per_board, decided_rows
from company_dataset.http import HttpClient, RequestError
from company_dataset.identity import _LEGAL_WORDS_FOR_NAMES, _fold, registrable_domain
from company_dataset.models import utc_now
from company_dataset.normalize import ASHBY, GREENHOUSE, LEVER, SMARTRECRUITERS, canonical_board_url, clean_website, host_of
from company_dataset.repairs import _record_evidence, usable_page_description
from company_dataset.verification import LIVE

MIN_PARAGRAPH_CHARS = 80
JOB_ADS_READ = 3
_CHECKPOINT_BATCH = 200
_ABOUT_HEADING_RE = re.compile(r"^(about( us| the company| our company)?|who we are|our company|the company|company (overview|description|profile))\W*$", re.I)
_ABOUT_INLINE_RE = re.compile(r"^(about( us)?|who we are)\s*[:\-–—]\s*(?P<rest>.+)$", re.I)
_LEGAL_NOTICE_RE = re.compile(r"\b(cookies?|privacy (policy|notice)|terms of (use|service)|all rights reserved|javascript|your browser|consent|copyright)\b", re.I)
_SKIPPED_TAGS = frozenset(("script", "style", "nav", "footer", "noscript", "svg", "form", "header", "aside", "template"))
_BLOCK_TAGS = frozenset(("p", "div", "section", "article", "li", "h1", "h2", "h3", "h4", "h5", "h6", "br", "td"))


@dataclass
class Target:
    board_id: int
    ats: str
    code: str
    region: str
    name: str
    website: str | None
    jobs: int


@dataclass
class Found:
    text: str
    method: str
    url: str


class _Blocks(HTMLParser):
    """Visible text as blocks, in order, with page furniture left out."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, str]] = []
        self._text: list[str] = []
        self._tag = "div"
        self._skipping = 0

    def _flush(self):
        text = re.sub(r"\s+", " ", "".join(self._text)).strip()
        if text and not self._skipping:
            self.blocks.append((self._tag, text))
        self._text = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIPPED_TAGS:
            self._flush()
            self._skipping += 1
        elif tag in _BLOCK_TAGS:
            self._flush()
            self._tag = tag

    def handle_endtag(self, tag):
        if tag in _SKIPPED_TAGS:
            self._flush()
            self._skipping = max(0, self._skipping - 1)
        elif tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data):
        if not self._skipping:
            self._text.append(data)

    def close(self):
        self._flush()
        super().close()


def text_blocks(html: str) -> list[tuple[str, str]]:
    parser = _Blocks()
    try:
        parser.feed(html or "")
        parser.close()
    except (ValueError, AssertionError):
        return parser.blocks
    return parser.blocks


def _name_words(name: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", _fold(name).lower()) if w not in _LEGAL_WORDS_FOR_NAMES]


def _names_company(text: str, name: str) -> bool:
    """The text uses the company's first distinctive word, whole."""
    words = _name_words(name)
    return bool(words) and re.search(rf"\b{re.escape(words[0])}\b", _fold(text).lower()) is not None


def _accept_paragraph(text: str, name: str) -> str | None:
    if len(text) < MIN_PARAGRAPH_CHARS or _LEGAL_NOTICE_RE.search(text) or not _names_company(text, name):
        return None
    return usable_page_description(text)


def about_section(html: str, name: str) -> str | None:
    """The paragraph under an "About us" or "About <company>" heading, or following one inline."""
    blocks = text_blocks(html)
    own_heading = re.compile(rf"^about\s+{re.escape(name)}\W*$", re.I)
    own_inline = re.compile(rf"^about\s+{re.escape(name)}\s*[:\-–—]\s*(?P<rest>.+)$", re.I)
    for i, (_, text) in enumerate(blocks):
        inline = _ABOUT_INLINE_RE.match(text) or own_inline.match(text)
        if inline:
            hit = _accept_paragraph(inline.group("rest"), name)
            if hit:
                return hit
        if len(text) <= 80 and (_ABOUT_HEADING_RE.match(text) or own_heading.match(text)):
            for _, following in blocks[i + 1:i + 3]:
                hit = _accept_paragraph(following, name)
                if hit:
                    return hit
    return None


def first_paragraph(html: str, name: str, method: str = "homepage_paragraph") -> str | None:
    for tag, text in text_blocks(html):
        if tag in ("p", "div", "section", "article"):
            hit = _accept_paragraph(text, name)
            if hit and acceptable(method, hit, name):
                return hit
    return None


def _json(client: HttpClient, url: str):
    try:
        resp = client.get(url)
        return resp.json() if resp.status == 200 else None
    except (RequestError, ValueError):
        return None


def vendor_profile(client: HttpClient, t: Target) -> Found | None:
    """Stage 1. Ashby's API and page data carry no company profile, so a job ad's about section is the vendor's record there."""
    q = quote(t.code, safe="")
    if t.ats == SMARTRECRUITERS:
        if not t.jobs:
            return None
        base = f"https://api.smartrecruiters.com/v1/companies/{q}/postings"
        listing = _json(client, f"{base}?limit=1") or {}
        for posting in (listing.get("content") or [])[:1]:
            detail = _json(client, f"{base}/{quote(str(posting.get('id')), safe='')}") or {}
            section = ((detail.get("jobAd") or {}).get("sections") or {}).get("companyDescription") or {}
            text = clean_description(section.get("text"))
            if text:
                return Found(text, "smartrecruiters_company_description", f"{base}/{posting.get('id')}")
        return None
    ads: list[str] = []
    url = ""
    if t.ats == GREENHOUSE:
        api = "boards-api.eu.greenhouse.io" if t.region == "eu" else "boards-api.greenhouse.io"
        info = _json(client, f"https://{api}/v1/boards/{q}") or {}
        text = clean_description(info.get("content"))
        if text:
            return Found(text, "greenhouse_board_info", f"https://{api}/v1/boards/{q}")
        if not t.jobs:
            return None
        url = f"https://{api}/v1/boards/{q}/jobs?content=true"
        ads = [unescape(j.get("content") or "") for j in ((_json(client, url) or {}).get("jobs") or [])[:JOB_ADS_READ]]
    elif t.ats == LEVER and t.jobs:
        host = "api.eu.lever.co" if t.region == "eu" else "api.lever.co"
        url = f"https://{host}/v0/postings/{q}?mode=json&limit={JOB_ADS_READ}"
        ads = [f"{j.get('description') or ''}{j.get('additional') or ''}" for j in (_json(client, url) or [])[:JOB_ADS_READ]]
    elif t.ats == ASHBY and t.jobs:
        url = f"https://api.ashbyhq.com/posting-api/job-board/{q}"
        ads = [j.get("descriptionHtml") or "" for j in ((_json(client, url) or {}).get("jobs") or [])[:JOB_ADS_READ]]
    for ad in ads:
        text = about_section(ad, t.name)
        if text:
            return Found(text, "job_ad_about_section", url)
    return None


def _html(client: HttpClient, url: str, site: str | None) -> str | None:
    """A page's HTML, if it answered from the same site and is not parked."""
    try:
        resp = client.get(url, headers=ENGLISH_HEADERS)
    except RequestError:
        return None
    if resp.status != 200:
        return None
    if site and registrable_domain(host_of(resp.url or url)) != registrable_domain(site):
        return None
    page = parse_page(resp.text[:3_000_000])
    if PARKED_RE.search(page.title + " " + (page.meta.get("description") or "")):
        return None
    return resp.text[:3_000_000]


def cascade(client: HttpClient, t: Target) -> Found | None:
    found = vendor_profile(client, t)
    if found:
        return found
    site = clean_website(t.website) if t.website else None
    pages: dict[str, tuple[str, str]] = {}
    if site:
        for suffix, label in (("", "homepage"), ("/about", "about_page"), ("/careers", "careers_page")):
            url = site + suffix
            html = _html(client, url, site)
            if html is None:
                if not suffix:
                    break  # the site itself is gone, redirected or parked: its other pages can't speak for the company
                continue
            pages[label] = (url, html)
            text = homepage_description(parse_page(html))
            if suffix:
                text = usable_page_description(text)
            method = f"{label}_meta" if suffix else "homepage_meta"
            if text and acceptable(method, text, t.name):
                return Found(text, method, url)
    board = canonical_board_url(t.ats, t.code, t.region or None)
    board_html = _html(client, board, None)
    if board_html:
        text = usable_page_description(homepage_description(parse_page(board_html)))
        if text and acceptable("board_page_meta", text, t.name):
            return Found(text, "board_page_meta", board)
    for label in ("about_page", "careers_page", "homepage"):
        if label in pages:
            text = first_paragraph(pages[label][1], t.name, f"{label}_paragraph")
            if text:
                return Found(text, f"{label}_paragraph", pages[label][0])
    if board_html:
        text = first_paragraph(board_html, t.name, "board_page_paragraph")
        if text:
            return Found(text, "board_page_paragraph", board)
    return None


def targets(conn, runs: list[str], confirmed_only: bool = False) -> list[Target]:
    """Live target boards with a usable name and no description, confirmed ones first: only they can
    be delivered now. Open boards are included because their identity may be confirmed later, and
    their description evidence is read here since merged rows show none until then."""
    identities = _latest_identity_per_board(conn, runs)
    marks = ",".join("?" * len(runs))
    described = defaultdict(list)
    for r in conn.execute(f"SELECT board_id, value, method FROM field_evidence WHERE field='description' AND status='verified' AND run IN ({marks})", runs):
        described[r["board_id"]].append(r)
    confirmed, others = [], []
    for r in decided_rows(conn, runs):
        if r["verification_status"] not in LIVE or r["description"]:
            continue
        name = r["company_name"] or identities[r["board_id"]]["name"]
        if not name or any(acceptable(e["method"], e["value"], name) for e in described[r["board_id"]]):
            continue
        target = Target(r["board_id"], r["ats_system"], r["ats_code"], r["region"], name, r["website_url"] or None, int(r["jobs_count"] or 0))
        (confirmed if r["company_name"] else others).append(target)
    return confirmed if confirmed_only else confirmed + others


def run(conn, run_label: str, from_runs: list[str], client: HttpClient, now: str, workers: int = 12, confirmed_only: bool = False) -> dict:
    todo = targets(conn, from_runs, confirmed_only)
    recovered = Counter()
    for start in range(0, len(todo), _CHECKPOINT_BATCH):
        batch = todo[start:start + _CHECKPOINT_BATCH]
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(lambda t: cascade(client, t), batch))
        with conn:
            for t, found in zip(batch, results):
                if found:
                    _record_evidence(conn, run_label, t.board_id, "description", found.text, found.method, found.url, found.text[:200], now)
                    recovered[found.method] += 1
        print(f"[fallback] {start + len(batch)}/{len(todo)} {dict(recovered)}", flush=True)
    return {"targets": len(todo), "recovered": dict(sorted(recovered.items())), "still_missing": len(todo) - sum(recovered.values())}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="company_dataset.fallback_descriptions")
    p.add_argument("--db", required=True)
    p.add_argument("--run", required=True)
    p.add_argument("--from-runs", required=True)
    p.add_argument("--fast", action="store_true", help="higher concurrency and request rates")
    p.add_argument("--confirmed-only", action="store_true", help="only boards that could be delivered now")
    a = p.parse_args(argv)
    conn = store.connect(a.db)
    client = HttpClient(timeout=20, per_host_interval=FAST_PER_HOST_INTERVAL if a.fast else 0.5, max_attempts=2)
    print(json.dumps(run(conn, a.run, [r for r in a.from_runs.split(",") if r], client, utc_now(), workers=FAST_WORKERS if a.fast else 12, confirmed_only=a.confirmed_only), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
