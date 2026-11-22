from __future__ import annotations

import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from company_dataset.http import HttpClient, RequestError
from company_dataset.models import load_candidates, utc_now
from company_dataset.platforms import EXTRA_ATS, PERSONIO
from company_dataset.normalize import (
    ASHBY,
    BREEZY,
    GREENHOUSE,
    LEVER,
    SMARTRECRUITERS,
    canonical_board_url,
    career_subdomain_parent,
    clean_website,
    host_of,
    parse_board_url,
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

COUNTRY_NAMES = {
    "usa": "US", "united states": "US", "united states of america": "US", "us": "US",
    "u.s.a.": "US", "u.s": "US", "america": "US", "estados unidos": "US",
    "united kingdom": "GB", "uk": "GB", "u.k.": "GB", "great britain": "GB",
    "britain": "GB", "england": "GB", "scotland": "GB", "wales": "GB",
    "northern ireland": "GB",
    "canada": "CA",
    "australia": "AU", "new zealand": "NZ", "ireland": "IE", "germany": "DE",
    "deutschland": "DE", "france": "FR", "spain": "ES", "italy": "IT",
    "netherlands": "NL", "the netherlands": "NL", "belgium": "BE", "austria": "AT",
    "switzerland": "CH", "sweden": "SE", "norway": "NO", "denmark": "DK",
    "finland": "FI", "iceland": "IS", "poland": "PL", "portugal": "PT",
    "greece": "GR", "czech republic": "CZ", "czechia": "CZ", "hungary": "HU",
    "romania": "RO", "bulgaria": "BG", "croatia": "HR", "serbia": "RS",
    "slovakia": "SK", "slovenia": "SI", "ukraine": "UA", "lithuania": "LT",
    "latvia": "LV", "estonia": "EE", "turkey": "TR", "türkiye": "TR",
    "israel": "IL", "united arab emirates": "AE", "saudi arabia": "SA",
    "qatar": "QA", "india": "IN", "singapore": "SG", "japan": "JP",
    "south korea": "KR", "china": "CN", "hong kong": "HK", "taiwan": "TW",
    "philippines": "PH", "malaysia": "MY", "indonesia": "ID", "vietnam": "VN",
    "thailand": "TH", "brazil": "BR", "mexico": "MX", "argentina": "AR",
    "chile": "CL", "colombia": "CO", "peru": "PE", "uruguay": "UY",
    "costa rica": "CR", "panama": "PA", "guatemala": "GT", "jamaica": "JM",
    "trinidad and tobago": "TT", "south africa": "ZA", "kenya": "KE",
    "nigeria": "NG", "egypt": "EG", "morocco": "MA",
}


def strip_html(text) -> str:
    if not text:
        return ""
    s = _TAG_RE.sub(" ", str(text))
    s = unescape(s)
    return _WS_RE.sub(" ", s).strip()


def normalize_country_name(name) -> str | None:
    if not isinstance(name, str):
        return None
    key = name.strip().lower().rstrip(".")
    if not key:
        return None
    return COUNTRY_NAMES.get(key)


def greenhouse_facts(payload: dict) -> dict:
    jobs = payload.get("jobs") or []
    meta = payload.get("meta") or {}
    jobs_count = meta.get("total", len(jobs))
    company_name = None
    for job in jobs:
        if isinstance(job, dict) and job.get("company_name"):
            company_name = str(job["company_name"]).strip()
            break
    origins: Counter[str] = Counter()
    for job in jobs:
        if not isinstance(job, dict):
            continue
        url = job.get("absolute_url") or ""
        try:
            host = urlsplit(url).hostname or ""
        except ValueError:
            continue
        if host and not host.endswith(".greenhouse.io"):
            scheme = urlsplit(url).scheme or "https"
            origins[f"{scheme}://{host}"] += 1
    website_url = origins.most_common(1)[0][0] if origins else None
    return {
        "jobs_count": jobs_count,
        "company_name": company_name,
        "website_url": website_url,
    }


def lever_facts(payload) -> dict:
    postings = payload if isinstance(payload, list) else []
    countries = {
        str(p.get("country")).strip()
        for p in postings
        if isinstance(p, dict) and p.get("country")
    }
    country = countries.pop() if len(countries) == 1 else None
    return {"jobs_count": len(postings), "country": country}


def ashby_facts(payload: dict) -> dict:
    jobs = payload.get("jobs") or []
    jobs_count = len(jobs)
    raw_countries = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        address = (job.get("address") or {}).get("postalAddress") or {}
        name = address.get("addressCountry")
        if isinstance(name, str) and name.strip():
            raw_countries.append(name.strip())
    country = None
    if raw_countries:
        mapped = {normalize_country_name(c) for c in raw_countries}
        if len(mapped) == 1 and None not in mapped:
            country = mapped.pop()
    return {"jobs_count": jobs_count, "country": country}


# ---------------------------------------------------------------------------
# HTML page summaries
# ---------------------------------------------------------------------------


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.anchors: list[tuple[str, str]] = []
        self._in_title = False
        self._title_done = False
        self._svg_depth = 0
        self._anchor_href: str | None = None
        self._anchor_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "svg":
            self._svg_depth += 1
        elif tag == "title":
            # Inline SVG icons carry their own <title>; only the document's first one names the page.
            self._in_title = not self._svg_depth and not self._title_done
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or "").strip().lower()
            if key and "content" in a and key not in self.meta:
                self.meta[key] = a["content"]
        elif tag == "a":
            if self._anchor_href is not None:
                self._close_anchor()
            self._anchor_href = a.get("href", "")
            self._anchor_text = []

    def handle_endtag(self, tag):
        if tag == "svg":
            self._svg_depth = max(0, self._svg_depth - 1)
        elif tag == "title":
            self._title_done = self._title_done or self._in_title
            self._in_title = False
        elif tag == "a" and self._anchor_href is not None:
            self._close_anchor()

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)
        if self._anchor_href is not None:
            self._anchor_text.append(data)

    def _close_anchor(self):
        self.anchors.append((self._anchor_href or "", _WS_RE.sub(" ", "".join(self._anchor_text)).strip()))
        self._anchor_href = None
        self._anchor_text = []


@dataclass
class Page:
    title: str = ""
    meta: dict = field(default_factory=dict)
    anchors: list = field(default_factory=list)
    html: str = ""


def parse_page(html: str) -> Page:
    parser = _PageParser()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:
        pass
    title = _WS_RE.sub(" ", unescape("".join(parser.title_parts))).strip()
    return Page(title=title, meta=parser.meta, anchors=parser.anchors, html=html or "")


_LINKEDIN_COMPANY_RE = re.compile(r"^https?://(?:[a-z]{2,3}\.)?linkedin\.com/company/([^/?#\s]+)", re.I)


def linkedin_company_slugs(page: Page) -> list[str]:
    """Distinct LinkedIn /company/ slugs linked from the page, in document order."""
    seen: list[str] = []
    for href, _ in page.anchors:
        m = _LINKEDIN_COMPANY_RE.match(href.strip())
        if not m:
            continue
        slug = unquote(m.group(1)).strip().rstrip("/")
        if slug and slug.lower() not in {s.lower() for s in seen}:
            seen.append(slug)
    return seen


def linkedin_url(slug: str) -> str:
    return f"https://www.linkedin.com/company/{slug}"


def board_refs(page: Page) -> list[str]:
    """ATS boards the page links to, as "ats:code" (code casefolded)."""
    refs: list[str] = []
    for href, _ in page.anchors:
        parsed = parse_board_url(href)
        if parsed:
            ref = f"{parsed.ats}:{parsed.code.casefold()}"
            if ref not in refs:
                refs.append(ref)
    for m in re.finditer(r"""["'](https?://[^"'\s]+(?:greenhouse\.io|lever\.co|ashbyhq\.com)[^"'\s]*)["']""", page.html):
        parsed = parse_board_url(m.group(1))
        if parsed:
            ref = f"{parsed.ats}:{parsed.code.casefold()}"
            if ref not in refs:
                refs.append(ref)
    return refs


# Phrases that mark recruiting copy, legal text or placeholder content rather than a
# description of what the company does.
_BOILERPLATE_RE = re.compile(
    r"(^(null|none|undefined|n/?a|test|home|homepage|welcome)\.?$"
    r"|test job board|lorem ipsum|we are (always )?looking for|we're (always )?looking for"
    r"|take a look at our open|open positions|current openings|join (our|the) team"
    r"|pre-employment|equal opportunity|eeo|applicants? (will|must)|apply (now|today|below)"
    r"|^(join|we['’]re seeking|we are seeking|want to be part)\b|join (us|our [a-z ]{0,20}team)"
    r"|careers? (at|with|opportunit)|learn and grow|rare opportunity|hire great people|we['’]re hiring"
    r"|explore open roles|open roles across|next career"
    r"|cookies?\b.*\b(consent|policy|accept)|enable javascript|just a moment|access denied"
    r"|page not found|coming soon|under construction|domain (is )?for sale|buy this domain"
    r"|attention required|verify you are human|privacy policy)",
    re.I,
)
# Meta descriptions that are not about the company at all: a bare media URL, domain-sale copy
# on parked domains, or the site's latest news item / talk.
_NOT_A_DESCRIPTION_RE = re.compile(
    r"(?i:^https?://|^\S+\.(mp4|jpe?g|png|gif|webp|pdf)\b)"
    r"|(?i:get this domain|this domain (name )?(is|may be) for sale|domain names? for sale|acquire the perfect domain|\bis for sale\b)"
    r"|(?i:^in this (talk|episode|webinar|video|article|post|session|interview)\b)"
    # News datelines: "LOS ANGELES— Today ..."
    r"|\b[A-Z][A-Z .]{2,}\s?[—–]\s?(?i:today|yesterday|this week)\b"
    r"|(?i:\b(received|wins|won) the award\b|\bpress release\b)",
)
_LABEL_PREFIX_RE = re.compile(r"^(about( us| the company)?|who we are|company (description|overview)|overview)\s*[:\-–—]\s*", re.I)
MAX_DESCRIPTION_CHARS = 600
MIN_DESCRIPTION_CHARS = 40


# A UTF-8 lead byte (Â..ô) followed by a continuation byte, both read as Latin-1, or the
# CP1252 form of the same ("â€"). Real text practically never contains these pairs.
_MOJIBAKE_RE = re.compile("[Â-ô][\x80-\xbf]|â€")
_INVISIBLE_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f​-‏‪-‮⁠﻿]")


def fix_mojibake(s: str) -> str:
    """Undo UTF-8 text that was decoded as Latin-1/CP1252 ("LÃ¶sungen" -> "Lösungen")."""
    if not _MOJIBAKE_RE.search(s):
        return s
    # "à " is C3 A0 20; decoded wrongly that's "Ã\xa0 ", which whitespace normalization
    # collapses to "Ã ". Restore the \xa0 and keep the real space after it.
    for candidate in (s, re.sub(r"([ÃÂ]) ", "\\1\xa0 ", s)):
        for codec in ("cp1252", "latin-1"):
            try:
                return candidate.encode(codec).decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
    return s


def clean_description(text) -> str | None:
    """A company description, or None when the text is boilerplate or too thin to use."""
    if not isinstance(text, str):
        return None
    s = strip_html(fix_mojibake(text))
    s = fix_mojibake(s)
    if _MOJIBAKE_RE.search(s):
        return None  # garbled beyond repair; better empty than unreadable
    s = _WS_RE.sub(" ", _INVISIBLE_RE.sub("", s)).strip()
    s = _LABEL_PREFIX_RE.sub("", s).strip()
    if len(s) < MIN_DESCRIPTION_CHARS:
        return None
    head = s[:200]
    if _BOILERPLATE_RE.search(head) or _NOT_A_DESCRIPTION_RE.search(s):
        return None
    if len(s) > MAX_DESCRIPTION_CHARS:
        cut = s[:MAX_DESCRIPTION_CHARS]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        s = cut[: end + 1] if end >= MIN_DESCRIPTION_CHARS else cut.rsplit(" ", 1)[0] + "…"
    return s


_EN_WORDS = frozenset("the and of to a in for is with our we you that on are your by from as it at an be".split())
# Common function words of the other languages that show up most (de, fr, es, pt, it, nl, id).
_FOREIGN_WORDS = frozenset(
    "der die das und mit für ist ein eine wir sie ihre unsere le la les des et pour avec une est nous vous "
    "el los las con por para que del una es em com uma não para os het een van voor met il di per che della "
    "dan yang untuk dengan og det er som på ikke af hos dig til vi jeg och att är pro je na se "
    "de du au al ed ti si sua seu tu ton votre notre nos sur dans "
    "ja joka sekä että myös tai kanssa noin ovat olla meidän".split()
)
# Letters that don't occur in English text (unlike é or ü, which do in loanwords and names).
_FOREIGN_LETTERS = frozenset("æøåßěščřžůďťňłąęśźżőűğşıãõœ")


def is_english(text) -> bool:
    """Is a description written in English? (langdetect, with a stopword check for close calls)."""
    if not isinstance(text, str) or not text.strip():
        return False
    from langdetect import DetectorFactory, detect_langs
    from langdetect.lang_detect_exception import LangDetectException

    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return False
    ascii_share = sum(ch.isascii() for ch in letters) / len(letters)
    if ascii_share < 0.8:
        return False  # mostly another script (or accented heavily): Korean, Japanese, Cyrillic…
    plain = re.sub(r"\S+\.[a-z]{2,6}\b\S*", " ", text.lower())  # "Squarepoint-Capital.com": not the word "com"
    words = re.findall(r"[^\W\d_]+", plain)
    english = sum(w in _EN_WORDS for w in words)
    foreign = sum(w in _FOREIGN_WORDS or not _FOREIGN_LETTERS.isdisjoint(w) for w in words)
    if foreign > english:
        return False
    # Finnish/Estonian: no stopword overlap with the list above, but ä/ö in many words.
    if words and sum(1 for w in words if "ä" in w or "ö" in w) >= max(2, 0.15 * len(words)):
        return False
    # English function words must be a real share of the text: Finnish "Usva on ... on" has two "on"s.
    if foreign == 0 and ((english >= 2 and english >= 0.1 * len(words)) or (len(text) < 100 and ascii_share >= 0.97)):
        return True  # clear English evidence; langdetect misreads short or technical text
    DetectorFactory.seed = 0  # deterministic results across runs
    try:
        best = detect_langs(text)[0]
    except LangDetectException:
        best = None
    if best is not None and best.prob >= 0.8:
        return best.lang == "en"
    return english >= 3 and foreign == 0


def homepage_description(page: Page) -> str | None:
    for key in ("description", "og:description", "twitter:description"):
        desc = clean_description(page.meta.get(key))
        if desc:
            return desc
    return None


_TITLE_SPLIT_RE = re.compile(r"\s+[|\-–—:•·]\s+")


def site_name(page: Page) -> str:
    name = (page.meta.get("og:site_name") or "").strip()
    if name:
        return unescape(name)
    parts = [p.strip() for p in _TITLE_SPLIT_RE.split(page.title) if p.strip()]
    return parts[0] if parts else ""


_LEGAL_SUFFIX_RE = re.compile(r"\b(inc|llc|ltd|limited|corp|corporation|co|company|plc|gmbh|llp|lp|pbc|holdings|group)\b\.?", re.I)


def name_key(name) -> str:
    if not isinstance(name, str):
        return ""
    return re.sub(r"[^a-z0-9]", "", _LEGAL_SUFFIX_RE.sub(" ", name.lower()))


def names_match(a, b) -> bool:
    ka, kb = name_key(a), name_key(b)
    if len(ka) < 3 or len(kb) < 3:
        return False
    return ka == kb or (len(ka) >= 4 and ka in kb) or (len(kb) >= 4 and kb in ka)


_BOARD_LABEL_RE = re.compile(
    r"(\bcareers?\b|\bjobs?\b|job board|open roles|openings|vacancies|\binternships?\b|\bgraduates?\b"
    r"|\bcontractors?\b|\bvia\b|\btest\b|career (site|website)|\bhiring\b|\brecruit)",
    re.I,
)


# Vendor demo / sandbox / test boards answer the API like any other board but are not
# companies. "Sandbox" alone is a real word in brand names (SandboxAQ, Sandbox VR), so
# it only counts when it follows another word ("Agoda Sandbox", "🔴 Sandbox").
_TEST_NAME_RE = re.compile(
    r"\S.*\bsandbox\b|\bdemo\b|^testing\b|testing purposes|\btest board\b|do not use|staging site"
    r"|^example corp|internal training|confidential|^test\d*$|^testes$|testjobpost|_test\b|accessibility tests|^democorp$"
    # Vendor sandbox accounts ("Sandbox: Millennium Hotels") and seeded candidates ("ACME candidate 1").
    r"|^sandbox\s*:|\bcandidate \d+$",
    re.I,
)
_TEST_CODE_RE = re.compile(r"sandbox\d*$|^demo\.|leverdemo|salesdemo|^testing$|^test\d*$|^example$|-demo$", re.I)

# Secondary boards a company runs next to its main one: internal mobility, referral
# intake, talent pools, general applications. Real boards, but not separate companies,
# and their names are labels ("Coupang Internal", "Buildkite Talent Community").
_AUX_NAME_RE = re.compile(
    # Boards the company itself marks as superseded: "Fortus (old)".
    r"\((old|inactive|legacy|archived|closed|do not use)\)|"
    # "Referral" only as a board label ("Acme Referrals", "Employee Referral Program"), not inside a
    # name ("Peel Addiction Assessment And Referral Centre").
    r"\binternal\b|\breferrals?\s+(only|program(me)?|portal|board|jobs|form|intake)\b|\bemployee referrals?\b|\breferrals\s*$|\(referrals?\)"
    r"|talent (community|pool|network)|general application|internal mobility"
    r"|employees only|staff and referrals|^talent pool$",
    re.I,
)


def is_test_board(name, code: str) -> bool:
    return bool((isinstance(name, str) and _TEST_NAME_RE.search(name)) or _TEST_CODE_RE.search(code or ""))


def is_auxiliary_board(name) -> bool:
    return bool(isinstance(name, str) and _AUX_NAME_RE.search(name))


# Whole values that are page furniture or placeholders, never a company's name. Exact matches
# only: "Home Depot", "Employers Holdings" and "EN Engineering" are names.
_JUNK_NAMES = frozenset({
    "null", "none", "undefined", "jobs", "careers", "unavailable", "n/a", "tbd", "default", "company", "home",
    "employer", "employers", "en", "de", "fr", "es", "nl", "homepage", "home page", "start", "startseite", "welcome",
    "accueil", "inicio", "index", "untitled", "my company", "your company", "company name", "test",
})


def clean_name(name, code: str = "") -> str | None:
    """A usable company name, or None for board labels and slug-shaped placeholders."""
    if not isinstance(name, str):
        return None
    s = _WS_RE.sub(" ", _INVISIBLE_RE.sub("", fix_mojibake(unescape(name)))).strip()
    # Decoration around a name ("*Owl Labs*", "-ASG-") is not part of it.
    s = re.sub(r"^[*\-_~ ]+|[*\-_~ ]+$", "", s)
    # A "- LinkedIn" suffix copied from a page title is not part of the name ("VerSprite - LinkedIn").
    s = re.sub(r"\s*[-|–—]\s*linkedin\s*$", "", s, flags=re.I).strip()
    if len(s) < 2 or s.lower() in _JUNK_NAMES:
        return None
    if re.fullmatch(r"Q\d+", s):  # a Wikidata item id shown in place of a missing label
        return None
    # A hostname in the name field ("starter-roundhouse.wpmudev.host") is a misconfigured board, not a name.
    if re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+){2,}", s):
        return None
    if _BOARD_LABEL_RE.search(s):
        return None
    if " " not in s and re.search(r"[-_]", s) and (not code or s.casefold() == code.casefold()):
        return None
    return s


def lever_home_page(page: Page) -> str | None:
    """The company website from a Lever board's "<Company> Home Page" footer link."""
    for href, text in page.anchors:
        if text.lower().endswith("home page"):
            site = clean_website(href)
            if site:
                return site
    return None


def ashby_organization(html: str) -> dict | None:
    """The organization block of an Ashby hosted page's window.__appData, if present."""
    marker = "window.__appData"
    idx = (html or "").find(marker)
    if idx < 0:
        return None
    start = html.find("{", idx)
    if start < 0:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(html[start:])
    except ValueError:
        return None
    org = data.get("organization") if isinstance(data, dict) else None
    return org if isinstance(org, dict) else None


def summarize_page(status: str, final_url: str, html: str) -> dict:
    """What we keep from a fetched page; raw HTML is not stored."""
    entry = {"status": status, "final_url": final_url, "fetched_at": utc_now()}
    if status != "ok":
        return entry
    page = parse_page(html)
    org = ashby_organization(html)
    entry.update({
        "title": page.title[:300],
        "site_name": site_name(page)[:200],
        "description": homepage_description(page),
        "linkedin_slugs": linkedin_company_slugs(page)[:10],
        "board_refs": board_refs(page)[:50],
        "lever_home_page": lever_home_page(page),
        "ashby_org": {
            "name": org.get("name"),
            "website": org.get("publicWebsite"),
        } if org else None,
        "ashby_appdata": "window.__appData" in html,
    })
    return entry


FINAL_STATUSES = ("ok", "http_404", "http_410")

_HREF_RE = re.compile(r'href="(https?://[^"]+)"')
CONTENT_JOBS_SCANNED = 30


def greenhouse_content_url(code: str) -> str:
    from urllib.parse import quote

    return f"https://boards-api.greenhouse.io/v1/boards/{quote(code, safe='')}/jobs?content=true"


def content_sites(payload) -> list[list]:
    """Company-site candidates linked from a board's own job ads, with how many ads link each."""
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    counts: Counter[str] = Counter()
    for job in (jobs or [])[:CONTENT_JOBS_SCANNED]:
        if not isinstance(job, dict):
            continue
        sites = set()
        for href in _HREF_RE.findall(unescape(job.get("content") or "")):
            site = clean_website(href)
            if site:
                sites.add(site)
        counts.update(sites)
    return [[site, n] for site, n in counts.most_common(5)]


def fetch_greenhouse_content(client: HttpClient, url: str) -> dict:
    try:
        resp = client.get(url)
    except RequestError as exc:
        return {"status": "error", "error": str(exc)[:200], "fetched_at": utc_now()}
    if resp.status != 200:
        return {"status": f"http_{resp.status}", "fetched_at": utc_now()}
    try:
        payload = resp.json()
    except ValueError:
        return {"status": "invalid_json", "fetched_at": utc_now()}
    return {"status": "ok", "content_sites": content_sites(payload), "fetched_at": utc_now()}


def content_website_candidates(board: "Board", content_page: dict | None) -> list[tuple[str, int]]:
    """(site, ads linking it), careers-style domains excluded, career subdomains replaced by parent."""
    out: list[tuple[str, int]] = []
    for site, n in (content_page or {}).get("content_sites") or []:
        host = host_of(site)
        parent = career_subdomain_parent(host)
        if parent:
            site, host = f"https://{parent}", parent
        if re.search(r"career|jobs", host.split(".")[0]):
            continue
        if site not in [s for s, _ in out]:
            out.append((site, n))
    return out[:3]


ENGLISH_HEADERS = {"Accept-Language": "en-US,en;q=0.9"}
ENGLISH_PATHS = ("/en", "/en-us", "/en-gb", "/us")


def english_variant_urls(site: str) -> list[str]:
    """Where an international site usually keeps its English homepage."""
    return [site.rstrip("/") + path for path in ENGLISH_PATHS]


def needs_english(page: dict | None) -> bool:
    """A fetched homepage whose description is missing English text or is garbled."""
    if not page or page.get("status") != "ok" or not page.get("description"):
        return False
    raw = page["description"]
    desc = clean_description(raw)
    return bool(_MOJIBAKE_RE.search(raw)) or (desc is not None and not is_english(desc))


def fetch_page(client: HttpClient, url: str, max_bytes: int = 3_000_000) -> dict:
    try:
        resp = client.get(url, headers=ENGLISH_HEADERS)
    except RequestError as exc:
        return {"status": "error", "error": str(exc)[:200], "final_url": "", "fetched_at": utc_now()}
    if resp.status == 200:
        return summarize_page("ok", resp.url, resp.text[:max_bytes])
    return {"status": f"http_{resp.status}", "final_url": resp.url, "fetched_at": utc_now()}


# ---------------------------------------------------------------------------
# Board resolution: which values each board gets, and from where
# ---------------------------------------------------------------------------


@dataclass
class Board:
    key: str
    ats: str
    code: str
    region: str | None
    careersite_url: str
    names: dict = field(default_factory=dict)


def board_from_validation(cand, outcome: dict) -> Board:
    code = outcome.get("code_validated") or cand.code
    # Only the host that actually answered decides the region; a source's "eu" hint
    # is just the order validation tried hosts in.
    region = "eu" if cand.ats in (LEVER, GREENHOUSE) and outcome.get("region_answered") == "eu" else None
    if cand.ats == PERSONIO and outcome.get("region_answered") == "com":
        region = "com"
    return Board(
        key=cand.key,
        ats=cand.ats,
        code=code,
        region=region,
        careersite_url=canonical_board_url(cand.ats, code, region),
    )


def vendor_websites(board: Board, outcome: dict, vendor_page: dict | None) -> list[tuple[str, str]]:
    """(website, basis) pairs attested by the ATS vendor itself, best first."""
    out: list[tuple[str, str]] = []
    if board.ats == ASHBY and vendor_page and vendor_page.get("ashby_org"):
        site = clean_website(vendor_page["ashby_org"].get("website"))
        if site:
            out.append((site, "ashby_public_website"))
    if board.ats == LEVER and vendor_page and vendor_page.get("lever_home_page"):
        out.append((vendor_page["lever_home_page"], "lever_home_page_link"))
    if board.ats == GREENHOUSE:
        site = clean_website(outcome.get("website_url"))
        if site:
            out.append((site, "greenhouse_job_url_origin"))
    expanded: list[tuple[str, str]] = []
    for site, basis in out:
        parent = career_subdomain_parent(host_of(site))
        if parent:
            expanded.append((f"https://{parent}", basis + "_parent"))
        expanded.append((site, basis))
    return expanded


def careersite_status(board: Board, vendor_page: dict | None) -> str:
    """ok, dead (vendor page gone) or unknown (not fetched / transient error)."""
    if board.ats == GREENHOUSE or board.ats in EXTRA_ATS:
        # These were validated against the board's own public endpoint, not a separate API.
        return "ok"
    if not vendor_page:
        return "unknown"
    status = vendor_page.get("status")
    if status in ("http_404", "http_410"):
        return "dead"
    if status != "ok":
        return "unknown"
    if board.ats == ASHBY and vendor_page.get("ashby_appdata") and not vendor_page.get("ashby_org"):
        return "dead"
    return "ok"


def homepage_supports(page: dict | None, board: Board, name: str | None) -> bool:
    """Does this homepage identify itself as the board's company?"""
    if not page or page.get("status") != "ok":
        return False
    if f"{board.ats}:{board.code.casefold()}" in (page.get("board_refs") or []):
        return True
    if name and (names_match(page.get("site_name"), name) or names_match(page.get("title"), name)):
        return True
    return False


def landing_site(url: str, page: dict | None) -> str | None:
    """The site a homepage request actually landed on (follows cross-domain redirects)."""
    if page and page.get("status") == "ok" and page.get("final_url"):
        final = clean_website(page["final_url"])
        if final:
            return final
        return None
    return clean_website(url)


def run_enrich(
    dirs: dict,
    client: HttpClient | None = None,
    max_workers: int = 10,
    timeout: float = 20.0,
    reference=None,
) -> dict:
    """Fetch vendor board pages and company homepages, caching one summary per URL.

    Cache entries with a final status (ok / 404 / 410) are reused; errors and other
    statuses are retried on the next run, so a transient failure never sticks.
    """
    from company_dataset import reference as reference_mod

    client = client or HttpClient(timeout=timeout, per_host_interval=0.25, max_attempts=2)
    cache_path = Path(dirs["cache"]) / "pages.json"
    pages: dict[str, dict] = {}
    if cache_path.exists():
        try:
            pages = json.loads(cache_path.read_text(encoding="utf-8"))
        except ValueError:
            pages = {}

    validation_path = Path(dirs["cache"]) / "validation.json"
    if not validation_path.exists():
        print("[enrich] no validation cache found; run validate stage first")
        return {}
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    boards: list[tuple[Board, dict]] = []
    for cand in load_candidates(dirs):
        outcome = validation.get(cand.key)
        if outcome and outcome.get("status") == "valid":
            board = board_from_validation(cand, outcome)
            board.names = {"source": cand.name or ""}
            boards.append((board, outcome))
    print(f"[enrich] {len(boards)} valid boards")

    def fetch_all(urls: list[str], label: str, fetcher=fetch_page):
        todo = [u for u in dict.fromkeys(urls) if pages.get(u, {}).get("status") not in FINAL_STATUSES]
        print(f"[enrich] {label}: {len(todo)} to fetch ({len(set(urls)) - len(todo)} cached)", flush=True)
        done = 0
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(fetcher, client, u): u for u in todo}
            for future in as_completed(futures):
                pages[futures[future]] = future.result()
                done += 1
                if done % 250 == 0:
                    _save(cache_path, pages)
                    print(f"[enrich] {label}: {done}/{len(todo)}", flush=True)
        _save(cache_path, pages)

    fetch_all([b.careersite_url for b, _ in boards if b.ats in (LEVER, ASHBY, BREEZY)], "vendor board pages")
    fetch_all(
        [greenhouse_content_url(b.code) for b, o in boards if b.ats == GREENHOUSE and not vendor_websites(b, o, None) and (o.get("jobs_count") or 0) > 0],
        "greenhouse job ads (website links)",
        fetcher=fetch_greenhouse_content,
    )

    ref = reference if reference is not None else reference_mod.load_or_build(dirs, boards, pages)

    homepage_urls: list[str] = []
    for board, outcome in boards:
        for site, _ in vendor_websites(board, outcome, pages.get(board.careersite_url)):
            homepage_urls.append(site)
        if board.ats == GREENHOUSE:
            for site, _ in content_website_candidates(board, pages.get(greenhouse_content_url(board.code))):
                homepage_urls.append(site)
        for site in ref.name_candidates(board_name_for_lookup(board, outcome, pages.get(board.careersite_url)), board.code):
            homepage_urls.append(site)
    fetch_all(homepage_urls, "company homepages")

    # Non-English or garbled homepage text: ask the same page for English (older cache
    # entries were fetched without Accept-Language), then try the usual /en paths.
    retry = [u for u in dict.fromkeys(homepage_urls) if needs_english(pages.get(u)) and not pages[u].get("english_requested")]
    print(f"[enrich] non-English or garbled homepages: {len(retry)} to refetch in English", flush=True)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for url, page in zip(retry, executor.map(lambda u: fetch_page(client, u), retry)):
            if page.get("status") == "ok" or pages[url].get("status") != "ok":
                pages[url] = page
            pages[url]["english_requested"] = True
    _save(cache_path, pages)
    variants = []
    for u in dict.fromkeys(homepage_urls):
        page = pages.get(u)
        site = landing_site(u, page) if page else None
        if site and needs_english(page):
            variants += english_variant_urls(site)
    fetch_all(variants, "English versions of homepages")

    if reference is None:
        # Rebuild so domains reached via homepage redirects are covered too.
        reference_mod.load_or_build(dirs, boards, pages)

    statuses = Counter(p.get("status") for p in pages.values())
    stats = {"generated_at": utc_now(), "pages_cached": len(pages), "page_statuses": dict(statuses)}
    (Path(dirs["intermediate"]) / "enrichment_stats.json").write_text(json.dumps(stats, indent=2))
    print(f"[enrich] done: {dict(statuses)}")
    return stats


def board_name_for_lookup(board: Board, outcome: dict, vendor_page: dict | None) -> str | None:
    """Best vendor/source name for a board, used to look it up in the reference dataset."""
    for name in vendor_names(board, outcome, vendor_page):
        return name
    return clean_name(board.names.get("source"), board.code)


def vendor_names(board: Board, outcome: dict, vendor_page: dict | None) -> list[str]:
    names: list[str] = []
    if board.ats in EXTRA_ATS or board.ats == SMARTRECRUITERS:
        n = clean_name(outcome.get("company_name"), board.code)
        if n:
            names.append(n)
    if board.ats == GREENHOUSE:
        n = clean_name(outcome.get("company_name"), board.code)
        if n:
            names.append(n)
    if board.ats == ASHBY and vendor_page and vendor_page.get("ashby_org"):
        n = clean_name(vendor_page["ashby_org"].get("name"), board.code)
        if n:
            names.append(n)
    if board.ats == LEVER and vendor_page and vendor_page.get("status") == "ok":
        n = clean_name(vendor_page.get("title"), board.code)
        if n:
            names.append(n)
    if board.ats == BREEZY and vendor_page and vendor_page.get("status") == "ok":
        # Breezy pages often have titles like "Openings at CompanyName" or "CompanyName - Careers"
        # Extract company name before cleaning
        raw_name = vendor_page.get("site_name") or vendor_page.get("title") or ""
        # Try to extract company name from "Openings at CompanyName" or "CompanyName - Careers" patterns
        import re
        m = re.match(r"Openings at\s+(.+)", raw_name, re.I)
        if m:
            raw_name = m.group(1).strip()
        else:
            m = re.match(r"(.+?)\s*[-–—|]\s*Careers?", raw_name, re.I)
            if m:
                raw_name = m.group(1).strip()
        n = clean_name(raw_name, board.code)
        if n:
            names.append(n)
    return names


def _save(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)
