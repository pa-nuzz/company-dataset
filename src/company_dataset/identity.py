"""Company identity for a live board, and which boards are the same company.

A live board is not yet a company. Identity needs the vendor's own naming of the board
plus at least one independent corroboration, or, for vendors that publish no name,
two independent kinds of evidence that agree. A third-party list never suffices alone.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from company_dataset import careersite
from company_dataset.enrichment import board_from_validation, clean_name, names_match, vendor_names
from company_dataset.models import Candidate
from company_dataset.normalize import host_of

CONFIRMED = "identity_confirmed"
UNRESOLVED = "identity_unresolved"
MISMATCH = "identity_mismatch"

# Evidence classes. vendor_api and vendor_page are one class: both are the vendor's record.
VENDOR_API = "vendor_api"
VENDOR_PAGE = "vendor_page"
HOMEPAGE = "homepage"          # a company site reached from the board (or linking to it) that names itself
SOURCE_LIST = "source_list"    # a third-party discovery list
STRUCTURED = "structured_profile"  # e.g. Wikidata item whose official website is the verified domain
AD_COPY = "ad_copy"            # tenant-authored "About" section on the job ad page; company-authored copy on ATS platform

_CLASS = {VENDOR_API: "vendor", VENDOR_PAGE: "vendor", HOMEPAGE: "homepage", SOURCE_LIST: "list", STRUCTURED: "structured", AD_COPY: "ad_copy"}


@dataclass
class NameClaim:
    name: str
    kind: str
    origin: str = ""
    raw: str = ""        # the value as extracted, before cleanup (kept for provenance)
    code_shaped: bool = False  # a list name made of the code's letters: may agree with a vendor name, never contradict it


@dataclass
class Identity:
    status: str
    name: str | None
    basis: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


def resolve_identity(claims: list[NameClaim], code: str = "", board_ownership: bool = False) -> Identity:
    """board_ownership: the company's own verified homepage links to exactly this board, and the
    board links to that homepage. That proves whose board it is, so it corroborates the vendor's name."""
    usable = []
    code_key = re.sub(r"[^a-z0-9]", "", (code or "").lower())
    for c in claims:
        n = clean_name(c.name, code)
        # A list "name" anyone could produce from the board code ("zbf", "Attendi", "Alcazar Energy"
        # for alcazar-energy) is not evidence about the company: it can neither confirm nor contradict.
        if n and c.kind == SOURCE_LIST and _derived_from_code(n, code):
            continue
        if n and c.origin.endswith("_job_hiring_organization") and strip_entity_code(n) != n:
            # Compare both spellings; whichever is corroborated is used ("84 Lumber" keeps its digits).
            usable.append(NameClaim(strip_entity_code(n), c.kind, c.origin, c.raw or c.name))
        if n:
            usable.append(NameClaim(n, c.kind, c.origin, c.raw or c.name, c.kind == SOURCE_LIST and _code_shaped(n, code_key)))
    vendor = [c for c in usable if _CLASS[c.kind] == "vendor"]
    others = [c for c in usable if _CLASS[c.kind] != "vendor"]
    # Spacing or casing a list adds to the code's letters ("10a Labs" for 10alabs) can back up the
    # vendor's own name. Without that name it says nothing, and it never outweighs one.
    informative = [c for c in others if not c.code_shaped]

    if vendor:
        ordered = [c for c in vendor if c.kind == VENDOR_API] + [c for c in vendor if c.kind != VENDOR_API]
        # With several vendor spellings, the one an independent source corroborates is the name.
        primary = next((v for v in ordered if any(names_compatible(o.name, v.name) for o in others)), ordered[0])
        agreeing = [c for c in others if names_compatible(c.name, primary.name)]
        if agreeing:
            return Identity(CONFIRMED, display_name([primary] + [c for c in vendor if names_compatible(c.name, primary.name)] + agreeing),
                            ["vendor"] + sorted({_CLASS[c.kind] for c in agreeing}))
        if board_ownership:
            return Identity(CONFIRMED, display_name([primary] + [c for c in vendor if names_compatible(c.name, primary.name)]), ["vendor", "board_ownership"])
        # Only a discovery list naming another company can contradict the vendor. Homepage title
        # segments and Wikidata labels are often taglines, products or parents: they may support a
        # match but never by themselves make one a mismatch.
        contradicting = [c for c in informative if c.kind == SOURCE_LIST]
        if contradicting:
            return Identity(MISMATCH, primary.name, ["vendor"], [f"{c.kind}:{c.origin}:{c.name}" for c in contradicting])
        if informative:
            return Identity(UNRESOLVED, primary.name, ["vendor_not_corroborated"], [f"{c.kind}:{c.origin}:{c.name}" for c in informative])
        return Identity(UNRESOLVED, primary.name, ["vendor_only"])

    # No vendor name (Rippling, Pinpoint, Workday, most iCIMS): two independent classes must agree,
    # and at least one must be something other than a discovery list.
    strong = [c for c in informative if c.kind != SOURCE_LIST]
    for c in strong:
        agreeing = [o for o in informative if o is not c and _CLASS[o.kind] != _CLASS[c.kind] and names_compatible(o.name, c.name)]
        if agreeing:
            return Identity(CONFIRMED, display_name([c] + agreeing), [_CLASS[c.kind]] + sorted({_CLASS[o.kind] for o in agreeing}))
    best = strong[0].name if strong else (informative[0].name if informative else None)
    return Identity(UNRESOLVED, best, ["insufficient_independent_evidence"])


_LEGAL_WORDS_FOR_NAMES = frozenset("inc incorporated llc llp lp ltd limited corp corporation co company plc pbc gmbh ag ab as asa oy oyj bv nv sa sas sarl srl spa kg ug se pty pte the and of".split())

# Words that qualify a name without changing which company it is: legal forms, places,
# and site furniture ("Official Website", "Home").
_QUALIFIERS = frozenset("""
inc incorporated llc llp lp ltd limited corp corporation co company plc pbc gmbh ag ab as asa oy oyj bv nv sa sas sarl srl spa kg ug
se pty pte the and official website site home homepage welcome page
uk us usa america americas canada europe eu emea apac asia nordic nordics norge norway sverige sweden danmark denmark suomi finland
deutschland germany austria osterreich schweiz switzerland nederland netherlands belgium belgie france espana spain italia italy
ireland australia india global international worldwide north south east west
""".split())


def _tokens(name: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (name or "").lower().replace("&", " and ").replace("+", " plus "))


def names_compatible(a, b) -> bool:
    """Conservative: the same company named two ways.

    True when the names are equal ignoring legal form, spacing and case; when the same
    distinctive words remain after dropping legal forms, places and site words ("XXL Norge" /
    "XXL", "Pearl Abyss Europe" / "Pearl Abyss Official Website", reordered words); or when
    one name's distinctive words begin the other's and are at least 5 letters long
    ("Lennox" / "Lennox International", "deister UK" / "deister electronic UK Ltd.").
    Never a mere substring: "Smart home with Samsung SmartThings" is not "SmartThings".
    """
    from company_dataset.enrichment import name_key

    if not a or not b:
        return False
    ka, kb = name_key(a), name_key(b)
    if len(ka) >= 2 and ka == kb:
        return True
    ta = [t for t in _tokens(a) if t not in _QUALIFIERS]
    tb = [t for t in _tokens(b) if t not in _QUALIFIERS]
    if not ta or not tb:
        return False
    if sorted(ta) == sorted(tb):
        return True
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return long_[: len(short)] == short and len("".join(short)) >= 5


_SEGMENT_SPLIT = re.compile(r"\s+[|\-–—:•·]\s+|\s*[|•·]\s*")


def brand_segments(page: dict) -> list[str]:
    """How a homepage names itself: og:site_name and each segment of its title."""
    out = []
    if page.get("site_name"):
        out.append(page["site_name"])
    for seg in _SEGMENT_SPLIT.split(page.get("title") or ""):
        seg = seg.strip()
        if seg and seg not in out:
            out.append(seg)
    return out


# Slugs that are never a company's own page: ATS vendors and theme/template placeholders.
_LINKEDIN_NEVER = frozenset({
    "recruitee", "tellenthq", "greenhouse", "lever", "ashbyhq", "smartrecruiters", "breezyhr", "workable", "workday", "bamboohr",
    "teamtailor", "personio", "rippling", "icims", "pinpoint", "pinpointhq", "join-com", "joincom", "linkedin",
    "fillyourhandle", "qode-themes", "elementor", "wordpress", "squarespace", "wix-com", "webflow-inc-", "hubspot",
})
_HANDLE_PREFIXES = ("buildwith", "weare", "hello", "lets", "meet", "join", "team", "with", "get", "use", "try", "hey", "the", "go", "my")
_HANDLE_SUFFIXES = ("official", "platform", "people", "global", "group", "dotcom", "dotai", "team", "corp", "gmbh", "inc", "llc", "ltd",
                    "app", "com", "hq", "ai", "io", "co", "ab")
_ACRONYM_SKIP = frozenset("and of the for de du des la le et und fur für".split())


def _fold(text: str, umlaut_e: bool = False) -> str:
    import unicodedata
    from urllib.parse import unquote

    t = unquote(text or "")
    if umlaut_e:
        t = t.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("Ä", "Ae").replace("Ö", "Oe").replace("Ü", "Ue")
    t = t.replace("ß", "ss")
    return "".join(ch for ch in unicodedata.normalize("NFKD", t) if not unicodedata.combining(ch))


def _strip_handle(key: str) -> set[str]:
    """acme with marketing affixes removed: getacme, acmehq, weareacme, acme3 -> acme."""
    out = {key, re.sub(r"\d{1,2}$", "", key)}
    for k in list(out):
        for p in _HANDLE_PREFIXES:
            if k.startswith(p) and len(k) - len(p) >= 3:
                out.add(k[len(p):])
    for k in list(out):
        for suf in _HANDLE_SUFFIXES:
            if k.endswith(suf) and len(k) - len(suf) >= 3:
                out.add(k[: -len(suf)])
    return out


def _initials(words: list[str], skip: bool) -> str:
    return "".join(w[0] for w in words if w and not (skip and w in _ACRONYM_SKIP))


def linkedin_compatible(slug: str, name: str | None, website: str | None) -> bool:
    """Does a LinkedIn company slug belong to this company (by its name or its domain)?

    Accepted: the slug equals or begins with the name/domain (whole words, "fortusadv" /
    Fortus); the same handle with marketing affixes removed ("getsona" / Sona, "icp_3" / ICP);
    an acronym spelled out ("cfc-underwriting-ltd" / CFC, "advanced-research-and-invention-agency"
    / ARIA) or abbreviated ("usdr" / U.S. Digital Response); accent and %-encoding variants.
    Rejected: a shared prefix only ("eyecare-partners.com" / EyeCare Associates), vendor and
    template pages, and translations or former names, which the slug alone cannot prove.
    """
    from company_dataset.enrichment import name_key

    raw = (slug or "").strip().rstrip("/")
    if raw.lower() in _LINKEDIN_NEVER or _fold(raw).lower() in _LINKEDIN_NEVER:
        return False
    words = [w for w in re.split(r"[^a-z0-9]+", _fold(raw).lower()) if w]
    sk = "".join(words)
    if len(sk) < 2 or sk.isdigit():
        return False
    name_keys = set()
    if name:
        for variant in (_fold(name), _fold(name, umlaut_e=True)):
            k = name_key(variant)
            if k:
                name_keys.add(k)
    keys = set(name_keys)
    if website:
        keys.add(re.sub(r"[^a-z0-9]", "", registrable_domain(website).split(".")[0]))
    handles = _strip_handle(sk)
    for k in keys:
        if len(k) < 2:
            continue
        if k in handles:
            return True
        if len(k) >= 4 and sk.startswith(k):
            return True
        if len(sk) >= 4 and k.startswith(sk) and len(sk) >= 0.6 * len(k):
            return True
        # Acronyms only against the name: a domain acronym (ucg.ac.uk) may itself be a parent's.
        if k in name_keys and 2 <= len(k) <= 6 and (words[0] == k or k in (_initials(words, True), _initials(words, False))):
            return True
    if name:
        # A name that leads with its acronym ("NGI", "MSF International", "TCI Technologies").
        lead = re.match(r"^([A-Z]{2,6})\b", _fold(name).strip())
        if lead:
            ac = lead.group(1).lower()
            if ac in words or ac in (_initials(words, True), _initials(words, False)) or ac in _strip_handle(sk):
                return True
        name_words = [w for w in re.split(r"[^a-z0-9]+", _fold(name).lower()) if w]
        if 2 <= len(sk) <= 6 and len(name_words) >= 2 and sk in (_initials(name_words, True), _initials(name_words, False)):
            return True
        return names_compatible(" ".join(words), _fold(name))
    return False


# Workday postings name the legal entity, often behind an internal code: "8974 DuPont Specialty
# Products", "B8115 Bob's Discount Furniture", "LE0002 U.S. Urology Partners".
_ENTITY_PREFIX_RE = re.compile(r"^(?:[A-Z]{1,4}\d{2,}[A-Z]?|\d{4,})\s+(?=\S)")


def strip_entity_code(name: str) -> str:
    return _ENTITY_PREFIX_RE.sub("", name).strip() or name
# Which agreeing claim to show as the company name: the vendor's board-level name first;
# homepage titles ("Home ID-ware English") and per-posting legal entities last.
_DISPLAY_ORDER = {VENDOR_API: 0, VENDOR_PAGE: 1, STRUCTURED: 2, SOURCE_LIST: 3, AD_COPY: 4, HOMEPAGE: 5}


def display_name(agreeing: list[NameClaim]) -> str:
    def rank(c):
        r = _DISPLAY_ORDER[c.kind]
        if c.origin.endswith("_job_hiring_organization"):
            r = 4
        return (r, len(c.name))

    return sorted(agreeing, key=rank)[0].name


def _derived_from_code(name: str, code: str) -> bool:
    """The name is the code itself, or the code with its separators read as spaces."""
    slug = (code or "").rsplit("/", 1)[-1].lower()  # ecc.wd5/careeropportunities -> careeropportunities
    spelled = " ".join(name.lower().split())
    return bool(slug) and spelled in (slug, " ".join(re.split(r"[-_.]+", slug)).strip())


def _code_shaped(name: str, code_key: str) -> bool:
    """The name's letters are the code's (or, for one word, part of it): "10a Labs" / 10alabs, "HubSpot" / hubspotjobs."""
    key = re.sub(r"[^a-z0-9]", "", name.lower())
    return bool(key) and (key == code_key or (" " not in name.strip() and key in code_key))


def claim_names(res: dict) -> list[str]:
    return [c.name for c in name_claims(res, homepage=None)]


def name_claims(res: dict, homepage: dict | None, homepage_independent: bool = False, wikidata_label: str | None = None) -> list[NameClaim]:
    b, v = res["board"], res["validation"]
    claims = []
    if v.get("company_name"):
        claims.append(NameClaim(v["company_name"], VENDOR_API, b["ats"] + "_api"))
    if (res.get("vendor_identity") or {}).get("name"):
        raw = res["vendor_identity"]["name"]
        claims.append(NameClaim(raw, VENDOR_API, b["ats"] + "_job_hiring_organization", raw))
    cand = Candidate(ats=b["ats"], codes=b["codes"] or [b["code"]], region=b["region"])
    board = board_from_validation(cand, v)
    if b["ats"] not in ("greenhouse",):
        for n in vendor_names(board, v, res.get("board_page")):
            if not v.get("company_name") or n != v["company_name"]:
                claims.append(NameClaim(n, VENDOR_PAGE, "hosted_page"))
    if res.get("board_extract"):
        for n, basis in careersite.board_page_names(res["board_extract"], b["code"]):
            claims.append(NameClaim(n, VENDOR_PAGE, basis))
    for source, n in b["names"]:
        claims.append(NameClaim(n, SOURCE_LIST, source))
    for text, method in (res.get("jobad_about") or []):
        # The jobad_about text already passed describes_company(text, vendor_company_name),
        # so the subject corroborates the vendor's company name.
        vendor_name = v.get("company_name")
        if vendor_name:
            claims.append(NameClaim(vendor_name, AD_COPY, method))
    if homepage and homepage_independent:
        for n in brand_segments(homepage):
            claims.append(NameClaim(n, HOMEPAGE, "homepage"))
    if wikidata_label:
        claims.append(NameClaim(wikidata_label, STRUCTURED, "wikidata"))
    return claims


def company_names_for_homepage(res: dict) -> list[str]:
    """Names a homepage is checked against: the provisional identity from vendor/page/list
    claims, so a page brand that differs from the vendor's ("Tripstack" on an "Etraveli Group"
    board) cannot pick the website. Without any vendor or page name, list names are used."""
    claims = name_claims(res, homepage=None)
    ident = resolve_identity(claims, res["board"]["code"])
    strong = [c for c in claims if c.kind in (VENDOR_API, VENDOR_PAGE)]
    if ident.name and strong:
        return [ident.name] + [c.name for c in strong if names_compatible(c.name, ident.name) and c.name != ident.name]
    return [c.name for c in claims]


# ---------------------------------------------------------------------------
# Company clusters
# ---------------------------------------------------------------------------

# Public suffixes with a second level, for the countries this dataset covers most.
SECOND_LEVEL = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "ltd.uk", "plc.uk", "me.uk", "com.au", "net.au", "org.au", "co.nz", "org.nz",
    "com.br", "co.jp", "com.mx", "co.za", "com.sg", "co.in", "co.il", "com.tr", "co.kr", "com.cn", "com.hk", "gc.ca", "qc.ca", "on.ca",
}


# Country and region words that, in a company name, usually mark a separate legal entity
# (a member firm, a national subsidiary). "International", "Global" and compass words are
# left out: they are as often part of one company's name ("Lennox International").
_GEO_PHRASES = {
    "united kingdom": "uk", "great britain": "uk", "united states": "us", "north america": "na", "new zealand": "nz",
    "hong kong": "hk", "south africa": "za", "middle east": "mena", "latin america": "latam", "south korea": "kr",
}
_GEO_WORDS = {
    "uk": "uk", "gb": "uk", "britain": "uk", "england": "uk", "scotland": "uk", "wales": "uk",
    "us": "us", "usa": "us", "america": "us", "americas": "na", "canada": "ca", "mexico": "mx",
    "europe": "eu", "eu": "eu", "emea": "emea", "apac": "apac", "asia": "apac", "dach": "dach", "benelux": "benelux",
    "latam": "latam", "mena": "mena", "africa": "africa", "nordic": "nordics", "nordics": "nordics",
    "norge": "no", "norway": "no", "sverige": "se", "sweden": "se", "danmark": "dk", "denmark": "dk", "suomi": "fi", "finland": "fi",
    "deutschland": "de", "germany": "de", "austria": "at", "osterreich": "at", "schweiz": "ch", "switzerland": "ch", "suisse": "ch",
    "nederland": "nl", "netherlands": "nl", "belgium": "be", "belgie": "be", "belgique": "be", "france": "fr", "espana": "es",
    "spain": "es", "italia": "it", "italy": "it", "portugal": "pt", "ireland": "ie", "poland": "pl", "polska": "pl",
    "australia": "au", "nz": "nz", "india": "in", "japan": "jp", "china": "cn", "singapore": "sg", "brasil": "br", "brazil": "br",
}


def geo_qualifiers(name: str | None) -> frozenset[str]:
    """Countries / regions a company name is qualified with: "KPMG Australia" -> {"au"}."""
    text = " " + " ".join(_tokens(_fold(name or ""))) + " "
    for phrase, code in _GEO_PHRASES.items():
        text = text.replace(f" {phrase} ", f" {code} ")
    return frozenset(_GEO_WORDS[t] for t in text.split() if t in _GEO_WORDS)


# A registrable domain built around a careers word is a recruiting site, not the company's
# main site (vistrycareers.co.uk, workatacme.com). "join" alone is common in product domains
# (joinhoney.com), so only "joinus" counts.
_CAREERS_DOMAIN_WORDS = ("careers", "career", "jobs", "karriere", "recruiting", "hiring", "joinus", "werkenbij", "workat", "workwith", "workfor")


def careers_branded_domain(url_or_host: str, names: list[str] | tuple = ()) -> bool:
    from company_dataset.enrichment import name_key

    label = registrable_domain(url_or_host).split(".")[0].replace("-", "")
    hits = [w for w in _CAREERS_DOMAIN_WORDS if label.startswith(w) or label.endswith(w)]
    # ...unless the company's own name carries the word (CareerBuilder, Jobsity).
    return bool(hits) and not any(w in name_key(n) for w in hits for n in names if n)


def domain_compatible(url_or_host: str, name: str | None) -> bool:
    """Does the registrable domain name the company? The LinkedIn handle rules, against the
    name only: excellentair.de / Excellent Air GmbH, ssi.inc / SSI; not dunigroup.com /
    Paper + Design, bn-biscuits.com / pladis France. Also: the TLD read as part of the name
    (vian.ai / Vianai); the name's initials (fpc-global.com / Facility Performance Consulting);
    the domain written in the name ("Agentur Hablesreiter - my-headhunter.at"); a group word
    around the name (beledad.com / Grupo Bel Edad); an acronym set off at the end ("... (BVG)",
    "... - UTEC"); the name's leading words joined (blacknova.vc / Black Nova Venture Capital).
    Each was checked on its own against known false matches and 300k random name/domain pairs
    (data/pilots/pilot3/PHASE1D_REPORT.md)."""
    if not name:
        return False
    reg = registrable_domain(url_or_host)
    label = re.sub(r"[^a-z0-9]", "", reg.split(".")[0])
    folded = _fold(name)
    if reg in folded.lower():
        return True
    trimmed = re.sub(r"^(?:grupo|gruppe|groep|groupe|gruppen|group)\s+|\s+(?:grupo|gruppe|groep|groupe|gruppen|group)\.?$", "", name.strip(), flags=re.I)
    if any(linkedin_compatible(x, n, None) for x in (reg.split(".")[0], reg.replace(".", "")) for n in {name, trimmed} if n):
        return True
    words = [w for w in re.split(r"[^a-z0-9]+", folded.lower()) if w and w not in _QUALIFIERS]
    initials = _initials(words, True)
    if len(initials) >= 3 and initials in _strip_handle(label):
        return True
    acronym = re.search(r"(?:\s[-–]\s|\()([A-Z]{3,6})\)?\s*$", folded)
    if acronym and label == acronym.group(1).lower():
        return True
    return len(label) >= 6 and any(label == "".join(words[:k]) for k in range(2, len(words) + 1))


# Second-level labels under a two-letter country code: utec.edu.pe, acme.com.eg, x.co.ke.
_CC_SECOND_LABELS = {"co", "com", "net", "org", "gov", "edu", "ac", "gob", "go", "or", "ne", "mil", "ltd", "plc", "sch", "asso", "nom", "gv"}


def registrable_domain(url_or_host: str) -> str:
    host = host_of(url_or_host) if "/" in (url_or_host or "") else (url_or_host or "").lower()
    parts = host.removeprefix("www.").split(".")
    if len(parts) >= 3 and (".".join(parts[-2:]) in SECOND_LEVEL or (len(parts[-1]) == 2 and parts[-2] in _CC_SECOND_LABELS)):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def cluster_companies(rows: list[dict]) -> list[list[int]]:
    """Group board rows into companies. Returns lists of row indexes.

    Rows: dicts with name, website_url, website_verified (bool), linkedin_url, ats_system,
    ats_code, provider_identifier (optional). Boards merge only on a strong shared key AND
    matching names; a name alone never merges, and a shared domain with different names
    (affiliates, franchise sites, a wrong website) does not merge either.
    """
    parent = list(range(len(rows)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    keys: dict[tuple, list[int]] = defaultdict(list)
    for i, r in enumerate(rows):
        if r.get("website_url") and r.get("website_verified"):
            keys[("domain", registrable_domain(r["website_url"]))].append(i)
        if r.get("linkedin_url"):
            keys[("linkedin", r["linkedin_url"].rstrip("/").lower())].append(i)
        if r.get("provider_identifier"):
            keys[("provider_id", r["ats_system"], r["provider_identifier"].casefold())].append(i)
    for key, members in keys.items():
        for x in range(len(members)):
            for y in range(x + 1, len(members)):
                a, b = members[x], members[y]
                if not names_match(rows[a]["name"], rows[b]["name"]):
                    continue
                # "KPMG Australia" and "KPMG" share kpmg.com but are separate member firms. Only the
                # same provider account is explicit evidence that differently qualified names are one entity.
                if key[0] != "provider_id" and geo_qualifiers(rows[a]["name"]) != geo_qualifiers(rows[b]["name"]):
                    continue
                parent[find(a)] = find(b)
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(rows)):
        groups[find(i)].append(i)
    return sorted(groups.values(), key=lambda g: g[0])
