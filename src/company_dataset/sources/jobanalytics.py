"""RichJacobs69/job-analytics per-ATS company mappings (config/*/company_*mapping*.json).

Pipeline configuration for a public job-analytics project: (company name, board slug) pairs
for Greenhouse, Lever, Ashby and SmartRecruiters. Only the five target ATS are kept; the
repository has no explicit license file, so provenance is recorded as public repository data.
"""
from __future__ import annotations

import csv
from pathlib import Path

from company_dataset.models import Reject, SourceMeta, SourceRecord
from company_dataset.normalize import clean_code

META = SourceMeta(
    name="jobanalytics",
    description="RichJacobs69/job-analytics: company-to-board-slug mappings used by a public job pipeline",
    download_url="https://github.com/RichJacobs69/job-analytics",
    filename="tokens.csv",
    license="Repository public data (no LICENSE file)",
    license_url="https://github.com/RichJacobs69/job-analytics",
    license_note="Public GitHub repository; no license file. Every board is independently verified against its provider before use.",
    attribution="RichJacobs69/job-analytics (GitHub)",
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
