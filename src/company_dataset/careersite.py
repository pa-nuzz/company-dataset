"""What a board's own hosted page says about the company: its name, its website, its LinkedIn.

Works the same for every ATS: the page's structured data (JSON-LD Organization /
hiringOrganization), og:site_name, a title like "Careers at Acme", and outbound links.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from html import unescape
from urllib.parse import urljoin

from company_dataset.enrichment import _LINKEDIN_COMPANY_RE, clean_name, name_key, parse_page
from company_dataset.normalize import career_subdomain_parent, clean_website, host_of

_JSONLD_RE = re.compile(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.I | re.S)
_TITLE_NAME_RES = [
    re.compile(r"^(?:careers|jobs|job openings|open positions|current openings|vacancies)\s+(?:at|@|with|bei|chez|hos)\s+(.+)$", re.I),
    re.compile(r"^(.+?)\s+(?:careers|jobs|job board|karriere|vacancies|stellenangebote)$", re.I),
    re.compile(r"^(?:jobs bei|stellenangebote bei|vacatures bij|offres d'emploi chez)\s+(.+)$", re.I),
]
_TITLE_SPLIT = re.compile(r"\s+[|\-–—:•·]\s+")
_HOME_TEXT = re.compile(r"\b(home ?page|website|visit (our )?(web)?site|company site|about us|back to)\b", re.I)


def _jsonld_objects(html: str):
    for m in _JSONLD_RE.finditer(html or ""):
        try:
            data = json.loads(m.group(1).strip())
        except ValueError:
            continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, list):
                stack.extend(x)
            elif isinstance(x, dict):
                yield x
                for k in ("@graph", "hiringOrganization", "publisher", "author", "brand"):
                    if k in x:
                        stack.append(x[k])


def organizations(html: str) -> list[dict]:
    """Organization-like JSON-LD objects: {name, url, same_as}."""
    out = []
    for o in _jsonld_objects(html):
        t = o.get("@type")
        types = t if isinstance(t, list) else [t]
        if not any(isinstance(x, str) and x in ("Organization", "Corporation", "LocalBusiness", "EmployerAggregateRating", "NGO") for x in types):
            continue
        same = o.get("sameAs") or []
        out.append({
            "name": o.get("name") if isinstance(o.get("name"), str) else None,
            "url": o.get("url") if isinstance(o.get("url"), str) else None,
            "same_as": [s for s in (same if isinstance(same, list) else [same]) if isinstance(s, str)],
        })
    return out


def title_name(title: str) -> str | None:
    """"Careers at Acme | Lever" -> "Acme"; None when the title names no company."""
    for part in _TITLE_SPLIT.split((title or "").strip()):
        part = part.strip()
        for rx in _TITLE_NAME_RES:
            m = rx.match(part)
            if m:
                return m.group(1).strip()
    return None


_NEXT_DATA_RE = re.compile(r"<script[^>]+__NEXT_DATA__[^>]*>(.*?)</script>", re.S)
_COMPANY_NAME_RE = re.compile(r'"companyName"\s*:\s*"([^"{}\\]{2,120})"')
_JSON_HREF_RE = re.compile(r'href=\\?"(https?://[^"\\\s]+)')


def next_data_facts(html: str) -> tuple[list[str], list[str]]:
    """Company name(s) and links in an embedded Next.js payload (Rippling boards).

    Template strings like "{{companyName}}" are skipped. Links come from the job ads in the
    payload, the same evidence as Greenhouse job-ad links.
    """
    m = _NEXT_DATA_RE.search(html or "")
    if not m:
        return [], []
    text = m.group(1).replace("\\u002F", "/").replace("\\u0026", "&")
    names = list(dict.fromkeys(unescape(n).strip() for n in _COMPANY_NAME_RE.findall(text)))
    links = list(dict.fromkeys(_JSON_HREF_RE.findall(text)))
    return names, links


def extract(html: str, base_url: str) -> dict:
    """Names, website candidates and LinkedIn slugs the board page itself states."""
    page = parse_page(html)
    names: list[tuple[str, str]] = []
    nd_names, nd_links = next_data_facts(html)
    for n in nd_names[:2]:
        names.append((n, "next_data_company_name"))
    for org in organizations(html):
        if org["name"]:
            names.append((unescape(org["name"]).strip(), "jsonld_organization"))
    if page.meta.get("og:site_name"):
        names.append((unescape(page.meta["og:site_name"]).strip(), "og_site_name"))
    tn = title_name(page.title)
    if tn:
        names.append((tn, "title"))

    board_host = host_of(base_url)
    site_votes: Counter = Counter()
    site_basis: dict[str, str] = {}
    for org in organizations(html):
        site = clean_website(org["url"]) if org["url"] else None
        if site and host_of(site) != board_host:
            site_votes[site] += 5
            site_basis.setdefault(site, "jsonld_organization_url")
    linkedin: list[str] = []
    for href, text in page.anchors:
        href = (href or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(base_url, href)
        m = _LINKEDIN_COMPANY_RE.match(absolute)
        if m:
            slug = m.group(1).strip().rstrip("/")
            if slug and slug.lower() not in {s.lower() for s in linkedin}:
                linkedin.append(slug)
            continue
        site = clean_website(absolute)
        if not site or host_of(site) == board_host:
            continue
        site_votes[site] += 3 if _HOME_TEXT.search(text or "") else 1
        site_basis.setdefault(site, "board_link_home_text" if _HOME_TEXT.search(text or "") else "board_link")
    for href in nd_links:
        site = clean_website(href)
        if site and host_of(site) != board_host:
            site_votes[site] += 1
            site_basis.setdefault(site, "board_job_ad_link")
    for org in organizations(html):
        for s in org["same_as"]:
            m = _LINKEDIN_COMPANY_RE.match(s)
            if m and m.group(1).lower() not in {x.lower() for x in linkedin}:
                linkedin.append(m.group(1).rstrip("/"))
    # A careers.acme.com link also points at acme.com.
    for site in list(site_votes):
        parent = career_subdomain_parent(host_of(site))
        if parent:
            p = clean_website("https://" + parent)
            if p and p not in site_votes:
                site_votes[p] += site_votes[site]
                site_basis[p] = site_basis[site] + "_parent"
    return {
        "names": names,
        "websites": [(s, site_basis[s], v) for s, v in site_votes.most_common(8)],
        "linkedin_slugs": linkedin[:10],
        "title": page.title[:300],
    }


def rank_websites(websites: list[tuple[str, str, int]], name: str | None, code: str) -> list[tuple[str, str]]:
    """Website candidates most likely to be the company's, best first.

    A domain whose label matches the company name or board code ranks first; otherwise
    links the page labels as the homepage, then link frequency. Candidates still have to
    pass homepage verification.
    """
    nk, ck = name_key(name or ""), re.sub(r"[^a-z0-9]", "", (code or "").lower())

    def score(item):
        site, basis, votes = item
        label = re.sub(r"[^a-z0-9]", "", host_of(site).split(".")[0])
        agrees = len(label) >= 3 and (label == nk or label == ck or (len(label) >= 4 and (label in nk or ck.startswith(label) or nk.startswith(label))))
        return (0 if agrees else 1, -votes)

    return [(s, b) for s, b, _ in sorted(websites, key=score)]


# Hosted pages often carry the vendor's own brand in og:site_name or the title.
VENDOR_BRANDS = {
    "greenhouse", "lever", "ashby", "smartrecruiters", "breezy", "breezyhr", "workable", "workday", "bamboohr",
    "recruitee", "teamtailor", "personio", "rippling", "icims", "pinpoint", "pinpointhq", "join", "joincom",
}


def board_page_names(extracted: dict, code: str) -> list[tuple[str, str]]:
    seen, out = set(), []
    for n, basis in extracted.get("names") or []:
        c = clean_name(n, code)
        if c and name_key(c) in VENDOR_BRANDS:
            continue
        if c and c.casefold() not in seen:
            seen.add(c.casefold())
            out.append((c, basis))
    return out
