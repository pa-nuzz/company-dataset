"""Common Crawl gap-fill harvest: the (crawl, prefix) pairs the legacy harvest never finished,
plus the newer crawl CC-MAIN-2026-43.

Same method and format as sources/commoncrawl.py (URL path tokens only, no page content);
the delta contains only board tokens not already known to a source in the store.
"""
from __future__ import annotations

import csv
from pathlib import Path

from company_dataset.models import Reject, SourceMeta, SourceRecord
from company_dataset.normalize import CORE_ATS, clean_code

META = SourceMeta(
    name="commoncrawl_gapfill",
    description="Common Crawl CDX index harvest: resumed the crawl/prefix pairs missing from the legacy harvest (31 crawls for SmartRecruiters and Breezy HR) and added crawl CC-MAIN-2026-43 for all five target ATS",
    download_url="https://data.commoncrawl.org/cc-index/collections/",
    filename="tokens.csv",
    license="Common Crawl Terms of Use",
    license_url="https://commoncrawl.org/terms-of-use/",
    license_note="Only URL path segments (factual board identifiers) are extracted from the index; every token is independently live-verified against the vendor's public API before export. No page content is used.",
    attribution="Common Crawl Foundation, crawls 2020-10 through 2026-43 (resumed + new)",
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
            crawl = (row.get("crawl") or "").strip()
            ref = f"line {i}; first seen {crawl}"
            if ats not in CORE_ATS:
                rejects.append(Reject(source=META.name, reason="unsupported_ats", detail=ats, row_ref=ref))
                continue
            raw_code, code = code, clean_code(ats, code)
            if code is None:
                rejects.append(Reject(source=META.name, reason="invalid_slug", detail=raw_code, row_ref=ref, ats=ats))
                continue
            records.append(SourceRecord(source=META.name, ats=ats, code=code, row_ref=ref))
    return records, rejects
