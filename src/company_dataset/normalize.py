from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

from company_dataset.platforms import EXTRA_ATS, clean_extra_code, extra_api_url, extra_board_url, parse_extra

GREENHOUSE = "greenhouse"
LEVER = "lever"
ASHBY = "ashby"
SMARTRECRUITERS = "smartrecruiters"
BREEZY = "breezy"
CORE_ATS = (ASHBY, BREEZY, GREENHOUSE, LEVER, SMARTRECRUITERS)
SUPPORTED_ATS = tuple(sorted(CORE_ATS + EXTRA_ATS))

_HOSTS = {
    "job-boards.greenhouse.io": GREENHOUSE,
    "boards.greenhouse.io": GREENHOUSE,
    "boards-api.greenhouse.io": GREENHOUSE,
    "job-boards.eu.greenhouse.io": GREENHOUSE,
    "boards.eu.greenhouse.io": GREENHOUSE,
    "jobs.lever.co": LEVER,
    "jobs.eu.lever.co": LEVER,
    "api.lever.co": LEVER,
    "api.eu.lever.co": LEVER,
    "jobs.ashbyhq.com": ASHBY,
    "api.ashbyhq.com": ASHBY,
    "careers.smartrecruiters.com": SMARTRECRUITERS,
    "jobs.smartrecruiters.com": SMARTRECRUITERS,
    "api.smartrecruiters.com": SMARTRECRUITERS,
}

BREEZY_HOST_SUFFIX = ".breezy.hr"
BREEZY_RESERVED_LABELS = frozenset({"api", "app", "developer", "help", "status", "www"})


def _breezy_host_code(host: str) -> str | None:
    labels = host.split(".")
    if len(labels) == 3 and host.endswith(BREEZY_HOST_SUFFIX):
        return labels[0]
    return None


@dataclass(frozen=True)
class ParsedBoard:
    ats: str
    code: str
    region: str | None = None


def _query_param(query: str, name: str) -> str | None:
    for part in query.split("&"):
        if not part:
            continue
        key, _, value = part.partition("=")
        if unquote(key) == name:
            return unquote(value)
    return None


def is_valid_slug(code: str) -> bool:
    if not code or len(code) > 128:
        return False
    if code.startswith("."):
        return False
    if "/" in code:
        return False
    if not all(32 < ord(ch) < 127 for ch in code):
        return False
    # A trailing ";" or "\" never occurs in a real board slug; it is the signature
    # of a scraped CSS/JS fragment leaking into a source dataset's URL column
    # (e.g. "100vw;", "Abridge\\") rather than a company-chosen identifier.
    if code.endswith(";") or code.endswith("\\"):
        return False
    return any(ch.isascii() and ch.isalnum() for ch in code)


def parse_board_url(raw) -> ParsedBoard | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
        raw_host = (parts.hostname or "").lower()
    except ValueError:
        return None
    host = raw_host[4:] if raw_host.startswith("www.") else raw_host
    ats = _HOSTS.get(host)
    breezy_code = None
    if ats is None:
        breezy_code = _breezy_host_code(raw_host)
        if breezy_code is not None:
            ats = BREEZY
    segs = [unquote(seg) for seg in parts.path.split("/") if seg != ""]
    if ats is None:
        extra = parse_extra(host, segs)
        if extra is None:
            return None
        ats, code, region = extra
        code = clean_extra_code(ats, code)
        return ParsedBoard(ats=ats, code=code, region=region) if code else None
    code = None
    region = None
    if ats == GREENHOUSE:
        region = "eu" if host in ("job-boards.eu.greenhouse.io", "boards.eu.greenhouse.io") else None
        if segs and segs[0] == "embed":
            code = _query_param(parts.query, "for")
        elif len(segs) >= 3 and segs[0] == "v1" and segs[1] == "boards":
            code = segs[2]
        elif segs:
            code = segs[0]
    elif ats == LEVER:
        if host in ("api.lever.co", "api.eu.lever.co"):
            region = "eu" if host == "api.eu.lever.co" else None
            if len(segs) >= 3 and segs[0] == "v0" and segs[1] == "postings":
                code = segs[2]
        else:
            region = "eu" if host == "jobs.eu.lever.co" else None
            if segs:
                code = segs[0]
    elif ats == ASHBY:
        if host == "api.ashbyhq.com":
            if len(segs) >= 3 and segs[0] == "posting-api" and segs[1] == "job-board":
                code = segs[2]
        elif segs:
            code = segs[0]
    elif ats == SMARTRECRUITERS:
        if host == "api.smartrecruiters.com":
            if len(segs) >= 3 and segs[0] == "v1" and segs[1] == "companies":
                code = segs[2]
        elif segs:
            code = segs[0]
    elif ats == BREEZY:
        code = breezy_code
    if code is None:
        return None
    code = clean_code(ats, code)
    if code is None:
        return None
    return ParsedBoard(ats=ats, code=code, region=region)


def clean_code(ats: str, code) -> str | None:
    """A board slug as the vendor would accept it, or None.

    Scraped columns often carry the tail of a JS string ("abridge\\", "camber.\\r\\n…",
    "block;"); the slug is what comes before it. Ashby organization slugs may contain
    single spaces ("Applied Compute"). Validation decides whether the board is real.
    """
    if ats in EXTRA_ATS:
        return clean_extra_code(ats, code.split("\\", 1)[0] if isinstance(code, str) else code)
    if not isinstance(code, str):
        return None
    code = code.split("\\", 1)[0].strip().rstrip(";").strip()
    if ats == BREEZY and code.casefold() in BREEZY_RESERVED_LABELS:
        return None
    if ats == ASHBY and " " in code:
        if not re.fullmatch(r"[^\s]+( [^\s]+)*", code) or not is_valid_slug(code.replace(" ", "-")):
            return None
        return code
    return code if is_valid_slug(code) else None


def canonical_board_url(ats: str, code: str, region: str | None = None) -> str:
    q = quote(code, safe="")
    if ats == GREENHOUSE:
        host = "job-boards.eu.greenhouse.io" if region == "eu" else "job-boards.greenhouse.io"
        return f"https://{host}/{q}"
    if ats == LEVER:
        host = "jobs.eu.lever.co" if region == "eu" else "jobs.lever.co"
        return f"https://{host}/{q}"
    if ats == ASHBY:
        return f"https://jobs.ashbyhq.com/{q}"
    if ats == SMARTRECRUITERS:
        return f"https://careers.smartrecruiters.com/{q}"
    if ats == BREEZY:
        return f"https://{q}.breezy.hr"
    return extra_board_url(ats, code, region)


def canonical_api_url(ats: str, code: str, region: str | None = None) -> str:
    q = quote(code, safe="")
    if ats == GREENHOUSE:
        return f"https://boards-api.greenhouse.io/v1/boards/{q}/jobs"
    if ats == LEVER:
        host = "api.eu.lever.co" if region == "eu" else "api.lever.co"
        return f"https://{host}/v0/postings/{q}?mode=json"
    if ats == ASHBY:
        return f"https://api.ashbyhq.com/posting-api/job-board/{q}"
    if ats == SMARTRECRUITERS:
        return f"https://api.smartrecruiters.com/v1/companies/{q}/postings?limit=100"
    if ats == BREEZY:
        return f"https://{q}.breezy.hr/json"
    return extra_api_url(ats, code, region)


def greenhouse_board_info_url(code: str) -> str:
    return f"https://boards-api.greenhouse.io/v1/boards/{quote(code, safe='')}"


# Hosts that are never a company's own website: ATS / recruiting platforms, link
# shorteners, site builders' marketing domains, and big platforms whose links show up
# in page chrome. A website on one of these says nothing about the company itself.
_NON_COMPANY_HOSTS = (
    "greenhouse.io", "greenhouse.com", "lever.co", "ashbyhq.com", "ashbyprd.com",
    "careerpuck.com", "myworkdayjobs.com", "workday.com", "icims.com", "adp.com",
    "ultipro.com", "ukg.com", "applytojob.com", "jazzhr.com", "bamboohr.com",
    "smartrecruiters.com", "jobvite.com", "workable.com", "recruitee.com",
    "breezy.hr", "teamtailor.com", "pinpointhq.com", "rippling.com", "paylocity.com",
    "taleo.net", "successfactors.com", "dayforcehcm.com", "paycomonline.net",
    "indeed.com", "glassdoor.com", "linkedin.com", "lnkd.in", "bit.ly", "linktr.ee",
    "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com", "tiktok.com",
    "google.com", "goo.gl", "squarespace.com", "wix.com", "webflow.io", "github.io",
    "notion.site", "medium.com", "outlook.com", "safelinks.protection.outlook.com",
    "typeform.com", "hubspot.com", "calendly.com", "cloudflare.com", "gstatic.com",
    "googleapis.com", "w3.org", "schema.org", "apple.com",
    # Vendors and widgets that hosted boards link to, never a company's own site.
    "join.com", "personio.de", "personio.com", "workablemail.com", "pinterest.com", "vimeo.com",
    "onetrust.com", "cookielaw.org", "cookiebot.com", "iubenda.com", "termly.io", "usercentrics.eu",
    "play.google.com", "apps.apple.com", "maps.app.goo.gl", "xing.com", "kununu.com", "threads.net",
    "wa.me", "t.me", "spotify.com",
    # Careers-site platforms: a company's jobs pages hosted for it, never its main site.
    "jibeapply.com", "careerplug.com", "applicantpro.com", "hrmdirect.com", "zohorecruit.com", "zohorecruit.eu", "freshteam.com",
    "recruiterbox.com", "hirebridge.com", "softgarden.io", "softgarden.de", "jobylon.com", "homerun.co", "talentlyft.com",
    # Domain marketplaces and parking pages: a "homepage" there is a domain for sale.
    "hugedomains.com", "saw.com", "sedo.com", "dan.com", "afternic.com", "undeveloped.com", "bodis.com",
    "parkingcrew.net", "above.com", "buydomains.com", "efty.com", "atom.com", "squadhelp.com", "brandbucket.com", "recaptcha.net", "hcaptcha.com", "sentry.io", "intercom.io",
)
_HOST_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
CAREER_SUBDOMAINS = ("careers", "career", "jobs", "job", "join", "work", "talent", "apply", "recruiting", "hiring")


def host_of(url) -> str:
    """Lower-cased hostname without a leading www., or "" when unparseable."""
    if not isinstance(url, str) or not url.strip():
        return ""
    text = url.strip()
    if "://" not in text:
        text = "https://" + text
    try:
        host = (urlsplit(text).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def is_non_company_host(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in _NON_COMPANY_HOSTS)


def normalize_linkedin(url) -> str | None:
    """https://www.linkedin.com/<kind>/<handle> with no trailing slash, query or fragment, or None if it is not one."""
    if not isinstance(url, str) or not url.strip():
        return None
    text = url.strip()
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return None
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) != 2:
        return None
    return f"https://www.linkedin.com/{segments[0]}/{segments[1]}"


def clean_website(url) -> str | None:
    """Normalize a website to scheme://host, or None if it cannot be a company site.

    Rejects malformed values seen in sources ("https://https:", "javascript:void(0)",
    "http://Beaver Process Equipment") and hosts that belong to platforms rather than
    the company.
    """
    if not isinstance(url, str):
        return None
    text = url.strip()
    if not text:
        return None
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
        raw_host = parts.hostname or ""
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    host = raw_host.lower().rstrip(".")
    if not _HOST_RE.match(host) or is_non_company_host(host.removeprefix("www.")):
        return None
    return f"{parts.scheme}://{host}"


def career_subdomain_parent(host: str) -> str | None:
    """careers.acme.com -> acme.com; None when the host has no career-style first label."""
    labels = host.split(".")
    if len(labels) >= 3 and labels[0] in CAREER_SUBDOMAINS:
        return ".".join(labels[1:])
    return None


def validation_endpoint(ats: str, code: str, outcome: dict) -> str:
    """Return the actual URL that was validated, using the code that validated.
    
    For SmartRecruiters: use the original token case in the API URL.
    For Lever: use the EU host if the EU host answered.
    For others: use the standard canonical API URL with the validated code.
    """
    validated_code = outcome.get("code_validated") or code
    if ats == SMARTRECRUITERS:
        return canonical_api_url(SMARTRECRUITERS, validated_code)
    if ats == LEVER:
        region = outcome.get("region_answered")
        return canonical_api_url(LEVER, validated_code, region)
    return canonical_api_url(ats, validated_code)
