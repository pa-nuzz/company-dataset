"""zachproffitt/builder-jobs-scraper active companies (data/companies.json).

The repository curates company names from Y Combinator batches, VC portfolio pages and
industry curation, then detects each company's ATS and board slug and re-checks the board
with the provider (status active). Only the five target ATS are kept. Workday, BambooHR,
Workable and Eightfold rows are out of scope.
"""
from __future__ import annotations

import csv
from pathlib import Path

from company_dataset.models import Reject, SourceMeta, SourceRecord
from company_dataset.normalize import clean_code

META = SourceMeta(
    name="builder_jobs",
    description="zachproffitt/builder-jobs-scraper: curated company list with detected ATS board slugs (active boards)",
    download_url="https://github.com/zachproffitt/builder-jobs-scraper",
    filename="tokens.csv",
    license="Repository public data (no LICENSE file)",
    license_url="https://github.com/zachproffitt/builder-jobs-scraper",
    license_note="Public GitHub repository; no license file. Every board is independently verified against its provider before use.",
    attribution="zachproffitt/builder-jobs-scraper (GitHub)",
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
            raw_code = (row.get("code") or "").strip()
            ref = f"line {i}"
            if ats not in ("greenhouse", "lever", "ashby", "smartrecruiters", "breezy"):
                rejects.append(Reject(source=META.name, reason="unsupported_ats", detail=ats, row_ref=ref))
                continue
            raw_code, code = raw_code, clean_code(ats, raw_code)
            if code is None:
                rejects.append(Reject(source=META.name, reason="invalid_slug", detail=raw_code, row_ref=ref, ats=ats))
                continue
            name = (row.get("name") or "").strip() or None
            records.append(SourceRecord(source=META.name, ats=ats, code=code, name=name, row_ref=ref))
    return records, rejects
