"""Common Crawl catch-all band harvest: legacy-subdomain board hosts.

{code}.greenhouse.io and {code}.smartrecruiters.com (single extra host label only) are
legacy board host forms that the path-based harvest (sources/commoncrawl.py) never
extracted; scripts/cc_subdirs_harvest.py pulls their codes from the catch-all SURT
prefixes "io,greenhouse," and "com,smartrecruiters," for every crawl. Vendor-infra host
names (app2, status, ...) pass the slug rules and are simply verified dead.
"""
from __future__ import annotations

import csv
from pathlib import Path

from company_dataset.models import Reject, SourceMeta, SourceRecord
from company_dataset.normalize import SUPPORTED_ATS, clean_code
from company_dataset.sources.commoncrawl import CRAWLS

META = SourceMeta(
    name="legacy_subdomains",
    description=f"Common Crawl catch-all band harvest of legacy-subdomain board hosts ({{code}}.greenhouse.io, {{code}}.smartrecruiters.com) over {len(CRAWLS) + 1} crawls",
    download_url="https://data.commoncrawl.org/cc-index/collections/",
    filename="tokens.csv",
    license="Common Crawl Terms of Use",
    license_url="https://commoncrawl.org/terms-of-use/",
    license_note="Only host labels (factual board identifiers) are extracted from the index; every token is independently live-verified against the vendor's public API before export. No page content is used.",
    attribution="Common Crawl Foundation, crawls 2019-50 through CC-MAIN-2026-43",
    downloadable=False,
)


def parse(path: Path):
    records: list[SourceRecord] = []
    rejects: list[Reject] = []
    if not Path(path).exists():
        return records, rejects
    with open(path, newline="", encoding="utf-8") as fh:
        for i, row in enumerate(csv.DictReader(fh), start=2):
            ats = (row.get("ats") or "").strip()
            code = (row.get("code") or "").strip()
            ref = f"line {i}"
            if ats not in SUPPORTED_ATS:
                rejects.append(Reject(source=META.name, reason="unsupported_ats", detail=ats, row_ref=ref))
                continue
            raw_code, code = code, clean_code(ats, code)
            if code is None:
                rejects.append(Reject(source=META.name, reason="invalid_slug", detail=raw_code, row_ref=ref, ats=ats))
                continue
            records.append(SourceRecord(source=META.name, ats=ats, code=code, row_ref=ref))
    return records, rejects
