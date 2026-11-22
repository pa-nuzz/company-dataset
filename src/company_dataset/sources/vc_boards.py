"""Board identifiers from venture-capital portfolio job sites, whose pages link to each company's ATS.

    python -m company_dataset.sources.vc_boards --raw data/raw

Reads /jobs and the pages a site's sitemap lists under /company, only where its robots.txt allows.
Sites that render jobs in the browser from an API have nothing to read, and the Getro API's robots.txt
disallows crawlers, so those are reported rather than worked around. A finished site is saved at once.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from company_dataset.http import HttpClient, RequestError
from company_dataset.models import SourceMeta
from company_dataset.sources._harvest import boards_in_text, parse_tokens, write_tokens

SITES = ["jobs.a16z.com", "jobs.sequoiacap.com", "jobs.greylock.com", "jobs.accel.com", "jobs.lsvp.com", "jobs.kleinerperkins.com", "jobs.bvp.com", "jobs.generalcatalyst.com",
         "jobs.khoslaventures.com", "careers.nea.com", "jobs.firstround.com", "jobs.8vc.com", "jobs.insightpartners.com", "jobs.techstars.com", "jobs.gv.com"]
USER_AGENT = "company-dataset/1.0"
MAX_PAGES_PER_SITE = 3000
MAX_SITEMAP_DEPTH = 2
SECONDS_BETWEEN_REQUESTS = 1.0
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")

META = SourceMeta(
    name="vc_boards",
    description="Venture-capital portfolio job sites linking to company ATS boards",
    download_url="https://jobs.a16z.com/",
    filename="tokens.csv",
    license="Public web pages; only URLs are kept",
    license_url="",
    license_note="Only pages robots.txt allows are read, and only URLs (factual board identifiers) kept; every board is verified against its provider before use.",
    attribution="Portfolio job sites, listed in report.json",
    downloadable=False,
)


def _get(client: HttpClient, url: str) -> str | None:
    try:
        resp = client.get(url, headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
    except RequestError:
        return None
    return resp.text if resp.status == 200 else None


def company_pages(client: HttpClient, site: str, allowed) -> list[str]:
    """Pages under /company from the sitemap, following one level of sitemap index."""
    pending, pages, seen = [(f"https://{site}/sitemap.xml", 0)], [], set()
    while pending:
        url, depth = pending.pop()
        if url in seen or depth > MAX_SITEMAP_DEPTH or not allowed(url):
            continue
        seen.add(url)
        # A site's job sitemaps list every posting; company pages already link to each board.
        if "sitemap_jobs" in url:
            continue
        for loc in _LOC_RE.findall(_get(client, url) or ""):
            if urlsplit(loc).path.endswith(".xml"):
                pending.append((loc, depth + 1))
            elif urlsplit(loc).path.startswith("/company/"):
                pages.append(loc)
    return sorted(set(pages))[:MAX_PAGES_PER_SITE]


def read_site(client: HttpClient, site: str) -> dict:
    robots = RobotFileParser()
    text = _get(client, f"https://{site}/robots.txt")
    robots.parse((text or "").splitlines())
    allowed = lambda url: robots.can_fetch(USER_AGENT, url)  # noqa: E731
    found: dict = {}
    pages = [f"https://{site}/jobs"] + company_pages(client, site, allowed)
    read = 0
    for url in pages:
        if not allowed(url):
            continue
        body = _get(client, url)
        if body is None:
            continue
        read += 1
        for key, value in boards_in_text(body.replace("\\/", "/").replace("\\u002F", "/"), url).items():
            found.setdefault(key, value)
    return {"site": site, "pages_read": read, "tokens": [[k[0], v[0], v[1], v[2]] for k, v in found.items()]}


def harvest(raw_dir: Path, client: HttpClient, sites=SITES) -> dict:
    out = Path(raw_dir) / META.name
    out.mkdir(parents=True, exist_ok=True)
    log = out / "sites.jsonl"
    done, tokens = {}, {}
    if log.exists():
        with open(log, encoding="utf-8") as fh:
            for line in fh:
                entry = json.loads(line)
                done[entry["site"]] = entry
    with open(log, "a", encoding="utf-8") as fh:
        for site in sites:
            if site in done:
                continue
            entry = read_site(client, site)
            fh.write(json.dumps(entry) + "\n")
            fh.flush()
            done[site] = entry
            print(f"[vc] {site}: {entry['pages_read']} pages, {len(entry['tokens'])} boards", flush=True)
    for entry in done.values():
        for ats, code, region, url in entry["tokens"]:
            tokens.setdefault((ats, code.casefold()), (code, region, url))
    write_tokens(out / META.filename, tokens)
    report = [{"site": e["site"], "pages_read": e["pages_read"], "boards": len(e["tokens"])} for e in done.values()]
    (out / "report.json").write_text(json.dumps(report, indent=1))
    return {"sites": len(done), "boards": len(tokens)}


def parse(path: Path):
    return parse_tokens(path, META.name)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="company_dataset.sources.vc_boards")
    p.add_argument("--raw", default="data/raw")
    a = p.parse_args(argv)
    print(json.dumps(harvest(Path(a.raw), HttpClient(timeout=45, per_host_interval=SECONDS_BETWEEN_REQUESTS, max_attempts=2)), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
