"""ATS platforms added after the original five: URL parsing, canonical URLs and live validation.

Each platform is only here because it has a public endpoint that answers differently for an
unknown board (404, 422 or a redirect off the board's host), so a 200 is evidence the board exists.
"""
from __future__ import annotations

import html
import json
import re
from urllib.parse import quote, urlsplit

WORKABLE = "workable"
WORKDAY = "workday"
BAMBOOHR = "bamboohr"
RECRUITEE = "recruitee"
TEAMTAILOR = "teamtailor"
PERSONIO = "personio"
RIPPLING = "rippling"
ICIMS = "icims"
PINPOINT = "pinpoint"
JOIN = "join"
EXTRA_ATS = (BAMBOOHR, ICIMS, JOIN, PERSONIO, PINPOINT, RECRUITEE, RIPPLING, TEAMTAILOR, WORKABLE, WORKDAY)

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_SUBDOMAIN_HOSTS = {
    BAMBOOHR: re.compile(rf"^({_LABEL})\.bamboohr\.com$"),
    RECRUITEE: re.compile(rf"^({_LABEL})\.recruitee\.com$"),
    TEAMTAILOR: re.compile(rf"^({_LABEL})\.teamtailor\.com$"),
    PINPOINT: re.compile(rf"^({_LABEL})\.pinpointhq\.com$"),
    ICIMS: re.compile(rf"^({_LABEL})\.icims\.com$"),
}
_PERSONIO_HOST = re.compile(rf"^({_LABEL})\.jobs\.personio\.(de|com)$")
_WORKABLE_LEGACY_HOST = re.compile(rf"^({_LABEL})\.workable\.com$")
_WORKDAY_HOST = re.compile(rf"^({_LABEL})\.(wd\d{{1,3}})\.myworkdayjobs\.com$")
_WORKDAY_SITE_HOST = re.compile(r"^(wd\d{1,3})\.myworkdaysite\.com$")
_WORKDAY_SITE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_LOCALE = re.compile(r"^[a-z]{2}-[A-Za-z]{2}$")
_RESERVED = {"www", "api", "app", "help", "support", "status", "blog", "careers", "jobs", "apply", "static", "cdn", "assets", "info", "mail", "login", "admin", "developers", "docs"}
_ICIMS_RESERVED = {"www", "api", "login", "community", "care", "developer", "developers", "support", "help", "cdn", "static"}
_PATH_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def parse_extra(host: str, segs: list[str]):
    """(ats, code, region) for a URL on one of the extra platforms, else None."""
    if host == "apply.workable.com":
        if segs and segs[0] not in ("api", "j") and _PATH_SLUG.match(segs[0]):
            return WORKABLE, segs[0].lower(), None
        return None
    m = _WORKABLE_LEGACY_HOST.match(host)
    if m and m.group(1) not in _RESERVED:
        return WORKABLE, m.group(1), None
    m = _WORKDAY_HOST.match(host)
    if m:
        site = segs[1] if segs and _LOCALE.match(segs[0]) and len(segs) > 1 else (segs[0] if segs else None)
        if site and site != "wday" and _WORKDAY_SITE.match(site):
            return WORKDAY, f"{m.group(1)}.{m.group(2)}/{site}", None
        return None
    m = _WORKDAY_SITE_HOST.match(host)
    if m and len(segs) >= 3 and segs[0] == "recruiting" and re.fullmatch(_LABEL, segs[1]) and _WORKDAY_SITE.match(segs[2]):
        return WORKDAY, f"{segs[1]}.{m.group(1)}/{segs[2]}", None
    m = _PERSONIO_HOST.match(host)
    if m and m.group(1) not in _RESERVED:
        return PERSONIO, m.group(1), ("com" if m.group(2) == "com" else None)
    if host == "ats.rippling.com":
        rest = segs[1:] if segs and _LOCALE.match(segs[0]) else segs
        if rest and rest[0] not in ("api", "static") and _PATH_SLUG.match(rest[0]):
            return RIPPLING, rest[0], None
        return None
    if host == "join.com":
        if len(segs) >= 2 and segs[0] == "companies" and _PATH_SLUG.match(segs[1]):
            return JOIN, segs[1], None
        return None
    for ats, rx in _SUBDOMAIN_HOSTS.items():
        m = rx.match(host)
        if m:
            label = m.group(1)
            reserved = _ICIMS_RESERVED if ats == ICIMS else _RESERVED
            if label in reserved:
                return None
            return ats, label, None
    return None


def clean_extra_code(ats: str, code) -> str | None:
    if not isinstance(code, str):
        return None
    code = code.strip()
    if ats == WORKDAY:
        # tenant.wdN/site: the host part is case-insensitive, the site path is not.
        m = re.fullmatch(rf"([A-Za-z0-9-]+)\.(wd\d{{1,3}})/([A-Za-z0-9_-]{{1,100}})", code, re.I)
        if not m or not re.fullmatch(_LABEL, m.group(1).lower()):
            return None
        return f"{m.group(1).lower()}.{m.group(2).lower()}/{m.group(3)}"
    if ats in (WORKABLE, RIPPLING, JOIN):
        return code if _PATH_SLUG.match(code) else None
    low = code.lower()
    return low if re.fullmatch(_LABEL, low) else None


def _workday_parts(code: str):
    host, site = code.split("/", 1)
    tenant, wd = host.split(".", 1)
    return tenant, wd, site


def extra_board_url(ats: str, code: str, region: str | None = None) -> str:
    q = quote(code, safe="")
    if ats == WORKABLE:
        return f"https://apply.workable.com/{q}/"
    if ats == WORKDAY:
        tenant, wd, site = _workday_parts(code)
        return f"https://{tenant}.{wd}.myworkdayjobs.com/{quote(site, safe='')}"
    if ats == BAMBOOHR:
        return f"https://{code}.bamboohr.com/careers"
    if ats == RECRUITEE:
        return f"https://{code}.recruitee.com/"
    if ats == TEAMTAILOR:
        return f"https://{code}.teamtailor.com/jobs"
    if ats == PERSONIO:
        return f"https://{code}.jobs.personio.{'com' if region == 'com' else 'de'}/"
    if ats == RIPPLING:
        return f"https://ats.rippling.com/{q}/jobs"
    if ats == ICIMS:
        return f"https://{code}.icims.com/jobs/search"
    if ats == PINPOINT:
        return f"https://{code}.pinpointhq.com/"
    if ats == JOIN:
        return f"https://join.com/companies/{q}"
    raise ValueError(f"unsupported ats: {ats}")


def extra_api_url(ats: str, code: str, region: str | None = None) -> str:
    q = quote(code, safe="")
    if ats == WORKABLE:
        return f"https://apply.workable.com/api/v1/widget/accounts/{q}"
    if ats == WORKDAY:
        tenant, wd, site = _workday_parts(code)
        return f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{quote(site, safe='')}/jobs"
    if ats == BAMBOOHR:
        return f"https://{code}.bamboohr.com/careers/list"
    if ats == RECRUITEE:
        return f"https://{code}.recruitee.com/api/offers/"
    if ats == TEAMTAILOR:
        return f"https://{code}.teamtailor.com/jobs.json"
    if ats == PERSONIO:
        return f"https://{code}.jobs.personio.{'com' if region == 'com' else 'de'}/xml"
    if ats == RIPPLING:
        return f"https://api.rippling.com/platform/api/ats/v1/board/{q}/jobs"
    if ats == ICIMS:
        return f"https://{code}.icims.com/jobs/search?ss=1&in_iframe=1"
    if ats == PINPOINT:
        return f"https://{code}.pinpointhq.com/postings.json"
    if ats == JOIN:
        return f"https://join.com/companies/{q}"
    raise ValueError(f"unsupported ats: {ats}")


WORKDAY_QUERY = {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}

_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_OG_TITLE = re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', re.I)


def page_title(text: str) -> str | None:
    for rx in (_OG_TITLE, _TITLE):
        m = rx.search(text or "")
        if m:
            t = html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()
            if t:
                return t
    return None


def name_from_title(ats: str, title: str | None) -> str | None:
    """Company name from a hosted board's title, or None when the title is generic."""
    if not title:
        return None
    t = title.strip()
    patterns = {
        JOIN: [r"^Jobs at (.+?) \| JOIN$"],
        PERSONIO: [r"^(?:Jobs bei|Jobs at|Stellenangebote bei|Vacatures bij|Ofertas de empleo en|Offres d'emploi chez) (.+)$", r"^(.+?) (?:Jobs|Karriere|Careers)$"],
        ICIMS: [r"^(.+?) Careers(?: \|.*)?$", r"^Careers at (.+?)(?: \|.*)?$", r"^(.+?) Jobs(?: \|.*)?$"],
    }
    for p in patterns.get(ats, []):
        m = re.match(p, t, re.I)
        if m:
            name = m.group(1).strip(" -|")
            if name and name.casefold() not in {"personio", "icims", "join", "careers", "jobs", "home"}:
                return name
    return None


def count_join_jobs(text: str, code: str) -> int:
    return len(set(re.findall(rf"/companies/{re.escape(code)}/(\d+)-", text or "", re.I)))


def count_icims_jobs(text: str) -> int:
    return len(set(re.findall(r"icims\.com/jobs/(\d+)/", text or "")))


def count_personio_jobs(text: str) -> int:
    return len(re.findall(r"<position>", text or ""))


def strip_tags(text: str | None) -> str | None:
    if not text:
        return None
    t = html.unescape(re.sub(r"<[^>]+>", " ", text))
    t = re.sub(r"\s+", " ", t).strip()
    return t or None


def same_host(url: str, expected_host: str) -> bool:
    try:
        return (urlsplit(url).hostname or "").lower() == expected_host.lower()
    except ValueError:
        return False


def loads(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return None
