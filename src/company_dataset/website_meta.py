"""Tag-specific homepage/about-page description extraction (og, twitter, JSON-LD Organization).

Everything it returns is the literal value the page published; the caller decides whether the
value is acceptable with description_rules.acceptable(). Nothing here invents or rewrites text.
"""
from __future__ import annotations

import json
import re
from html import unescape
from urllib.parse import urljoin

META_RE = re.compile(
    r"<meta[^>]+?((?:property|name)\s*=\s*[\"'](?P<name>[^\"']+)[\"'])(?:[^>]+?content\s*=\s*[\"'](?P<content>[^\"']*)[\"']|content\s*=\s*[\"'](?P<content2>[^\"']*)[\"'][^>]+?(?:property|name)\s*=\s*[\"'](?P<name2>[^\"']+)[\"'])?",
    re.I,
)

_OG_KEYS = {"og:description", "og:title", "og:site_name", "og:url", "og:image"}
_TWITTER_KEYS = {"twitter:description", "description", "twitter:title"}

_LD_RE = re.compile(r"<script[^>]+?type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.I | re.S)
ABOUT_HREFS = re.compile(r"href\s*=\s*[\"']([^\"']*(about|company|team)[^\"']*)[\"']", re.I)

jld = None


def og_description(html: str) -> str:
    for m in META_RE.finditer(html or ""):
        name = (m.group("name") or m.group("name2") or "").lower()
        if name == "og:description":
            content = m.group("content") or m.group("content2") or ""
            return unescape(content).strip()
    return ""


def twitter_description(html: str) -> str:
    for m in META_RE.finditer(html or ""):
        name = (m.group("name") or m.group("name2") or "").lower()
        if name == "twitter:description":
            content = m.group("content") or m.group("content2") or ""
            return unescape(content).strip()
    return ""


def jsonld_org_description(html: str) -> str:
    for m in _LD_RE.finditer(html or ""):
        try:
            data = json.loads(m.group(1).strip() or "null")
        except (ValueError, TypeError):
            continue
        for node in _flatten(data):
            if not isinstance(node, dict):
                continue
            t = node.get("@type")
            types = t if isinstance(t, list) else [t]
            if any(isinstance(x, str) and x.casefold() == "organization" for x in types):
                d = node.get("description")
                if isinstance(d, str) and d.strip():
                    return unescape(d).strip()
    return ""


def about_meta(url: str, html: str) -> tuple[str, str]:
    """(description meta/<title> text of the about page, linkedin_url found on the company's own
    homepage/about page). Sharing the same LinkedIn rule as the homepage pass keeps this honest:
    the linkedin_url only comes from a link ON the company's own page."""
    desc = og_description(html) or twitter_description(html) or jsonld_org_description(html)
    return desc, _first_linkedin(html, url)


_LINKEDIN_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/(?:company|school)/([A-Za-z0-9_\-]+)/?", re.I)


def _first_linkedin(html: str, base_url: str) -> str:
    for m in _LINKEDIN_RE.finditer(html or ""):
        holder = f"https://www.linkedin.com/company/{m.group(1)}/"
        return holder
    return ""


def _flatten(data):
    if isinstance(data, list):
        for item in data:
            yield from _flatten(item)
    elif isinstance(data, dict):
        if "@graph" in data:
            yield from _flatten(data["@graph"])
        else:
            yield data
