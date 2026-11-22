"""Africa ATS directory (github.com/Thelastpoet/africa-ats-directory): company-mapped boards.

Public GitHub repository, catalogued 2026-03; no LICENSE file is present in the repository,
so the provenance is recorded as public repository data. Only the five target ATS are kept.
"""
from __future__ import annotations

import csv
from pathlib import Path

from company_dataset.models import Reject, SourceMeta, SourceRecord
from company_dataset.normalize import clean_code

META = SourceMeta(
    name="africa_ats",
    description="Thelastpoet/africa-ats-directory: African companies mapped to ATS board URLs",
    download_url="https://github.com/Thelastpoet/africa-ats-directory",
    filename="tokens.csv",
    license="Repository public data (no LICENSE file)",
    license_url="https://github.com/Thelastpoet/africa-ats-directory",
    license_note="Public GitHub repository; no license file. Every board is independently verified against its provider before use.",
    attribution="Thelastpoet/africa-ats-directory (GitHub)",
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
            country = (row.get("country") or "").strip() or None
            records.append(SourceRecord(source=META.name, ats=ats, code=code, name=name, row_ref=ref + (f"; country={country}" if country else "")))
    return records, rejects
