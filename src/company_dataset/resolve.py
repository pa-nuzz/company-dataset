"""Decide each board's field values from cached evidence, recording where each came from.

Precedence (first usable value wins; nothing is ever constructed from a name):
  name        vendor (Greenhouse board / Ashby org / Lever board title) > source dataset
              > reference (domain match) > homepage og:site_name / title
  website     vendor-attested website > reference name match confirmed by the homepage
  linkedin    homepage link that agrees with the reference handle > homepage link that
              matches the company name/domain > reference handle (domain match)
  description homepage meta description > Greenhouse board intro (both filtered)
  country     reference HQ country > country shared by every posting on the board
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from company_dataset.enrichment import (
    Board,
    careersite_status,
    clean_description,
    clean_name,
    content_website_candidates,
    english_variant_urls,
    greenhouse_content_url,
    homepage_supports,
    is_english,
    landing_site,
    linkedin_url,
    name_key,
    vendor_names,
    vendor_websites,
)
from company_dataset.normalize import host_of


@dataclass
class Resolved:
    name: str = ""
    description: str = ""
    linkedin_url: str = ""
    website_url: str = ""
    country: str = ""
    careersite_status: str = ""
    sources: dict = field(default_factory=dict)


def slug_matches(slug: str, name: str | None, website: str | None) -> bool:
    """Is a LinkedIn slug plausibly this company's (by name or domain label)?"""
    k = name_key(slug.replace("-", " ").replace("_", " "))
    if len(k) < 3 or k.isdigit():
        return False
    keys = [name_key(name)] if name else []
    if website:
        keys.append(re.sub(r"[^a-z0-9]", "", host_of(website).split(".")[0]))
    for other in keys:
        if len(other) < 3:
            continue
        if k == other or k in other or other in k:
            return True
        prefix = 0
        for a, b in zip(k, other):
            if a != b:
                break
            prefix += 1
        if prefix >= 4:
            return True
    return False


def resolve_board(board: Board, outcome: dict, pages: dict, ref, source_name: str = "", source_name_origin: str = "") -> Resolved:
    out = Resolved()
    vendor_page = pages.get(board.careersite_url)
    out.careersite_status = careersite_status(board, vendor_page)

    names = vendor_names(board, outcome, vendor_page)
    name = names[0] if names else None
    name_src = "vendor" if name else ""
    if not name:
        name = clean_name(source_name, board.code)
        name_src = source_name_origin if name else ""

    website = None
    website_src = ""
    homepage = None
    for site, basis in vendor_websites(board, outcome, vendor_page):
        page = pages.get(site)
        status = (page or {}).get("status")
        if basis.endswith("_parent"):
            if status == "ok" and landing_site(site, page):
                website, website_src, homepage = landing_site(site, page), basis, page
                break
            continue
        if status in ("http_404", "http_410"):
            continue
        if status == "ok":
            landed = landing_site(site, page)
            if not landed:
                continue
            website, website_src, homepage = landed, basis, page
        else:
            website, website_src = site, basis
        break
    if not website and board.ats == "greenhouse":
        slug_key = re.sub(r"[^a-z0-9]", "", board.code.lower())
        for site, ads in content_website_candidates(board, pages.get(greenhouse_content_url(board.code))):
            page = pages.get(site)
            if (page or {}).get("status") in ("http_404", "http_410"):
                continue
            landed = landing_site(site, page) if (page or {}).get("status") == "ok" else site
            if not landed:
                continue
            label = re.sub(r"[^a-z0-9]", "", host_of(site).split(".")[0])
            agrees = len(label) >= 3 and (label == slug_key or label == name_key(name) or (len(label) >= 4 and slug_key.startswith(label)))
            if homepage_supports(page, board, name):
                website, website_src, homepage = landed, "greenhouse_job_ads_homepage_verified", page
                break
            if agrees and ads >= 2:
                website, website_src = landed, "greenhouse_job_ads_slug_match"
                homepage = page if (page or {}).get("status") == "ok" else None
                break
    if not website:
        for site in ref.name_candidates(name, board.code):
            page = pages.get(site)
            if homepage_supports(page, board, name):
                landed = landing_site(site, page)
                if landed:
                    website, website_src, homepage = landed, "reference_name_match_homepage_verified", page
                    break

    if not name and website:
        ref_name = clean_name(ref.name(website), board.code)
        if ref_name:
            name, name_src = ref_name, "reference"
    if not name and homepage:
        site_name = clean_name(homepage.get("site_name"), board.code)
        if site_name:
            name, name_src = site_name, "homepage"

    linkedin = ""
    linkedin_src = ""
    if website:
        slugs = (homepage or {}).get("linkedin_slugs") or []
        ref_slug = ref.linkedin_slug(website, name)
        lowered = {s.lower(): s for s in slugs}
        if ref_slug and ref_slug.lower() in lowered:
            linkedin, linkedin_src = linkedin_url(lowered[ref_slug.lower()]), "homepage_and_reference"
        else:
            matching = [s for s in slugs if slug_matches(s, name, website)]
            if matching:
                linkedin, linkedin_src = linkedin_url(matching[0]), "homepage_link"
            elif ref_slug:
                linkedin, linkedin_src = linkedin_url(ref_slug), "reference_domain_match"
            elif len(slugs) == 1 and slugs[0].isdigit():
                # A numeric company ID can't be name-checked, but when it is the only
                # company page the company's own homepage links to, it is theirs.
                linkedin, linkedin_src = linkedin_url(slugs[0]), "homepage_link_numeric_id"

    description = ""
    description_src = ""
    # Re-clean cached text so filter improvements apply without refetching.
    # The list is for an English-speaking audience: a description only counts in English.
    home_desc = clean_description(homepage.get("description")) if homepage else None
    if home_desc and is_english(home_desc):
        description, description_src = home_desc, "homepage_meta"
    elif website:
        for url in english_variant_urls(website):
            page = pages.get(url)
            variant = clean_description((page or {}).get("description")) if (page or {}).get("status") == "ok" else None
            if variant and is_english(variant) and host_of(page.get("final_url") or url).removeprefix("www.") == host_of(website).removeprefix("www."):
                description, description_src = variant, "homepage_meta_english_page"
                break
    if not description and board.ats == "workable":
        wk = clean_description(outcome.get("description"))
        if wk:
            description, description_src = wk, "workable_account_description"
    if not description and board.ats == "greenhouse":
        gh = clean_description(outcome.get("description"))
        if gh and is_english(gh):
            description, description_src = gh, "greenhouse_board_intro"

    country = ""
    country_src = ""
    if website:
        c = ref.country(website, name)
        if c:
            country, country_src = c, "reference_hq"
    if not country and outcome.get("country"):
        country, country_src = outcome["country"], outcome.get("country_basis") or "postings"

    out.name = name or ""
    out.description = description
    out.linkedin_url = linkedin
    out.website_url = website or ""
    out.country = country
    out.sources = {
        "name_source": name_src,
        "website_source": website_src,
        "linkedin_source": linkedin_src,
        "description_source": description_src,
        "country_source": country_src,
    }
    return out
