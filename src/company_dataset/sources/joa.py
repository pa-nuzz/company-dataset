from __future__ import annotations

import json
from pathlib import Path

from company_dataset.models import Reject, SourceMeta, SourceRecord
from company_dataset.normalize import parse_board_url

META = SourceMeta(
    name="joa",
    description="Job Opportunities API employer-direct job postings sample (25k postings, October 2026); board tokens taken from apply_url",
    download_url="https://huggingface.co/datasets/JobOpportunitiesAPI/joa-employer-direct-job-postings-2026-10/resolve/main/joa_hf_25k.jsonl",
    filename="joa_hf_25k.jsonl",
    license="CC-BY-4.0",
    license_url="https://creativecommons.org/licenses/by/4.0/",
    license_note="CC BY 4.0 requires attribution. Only ATS board identifiers are taken from apply URLs; posting text is not redistributed. Each board is live-verified before export.",
    attribution="JobOpportunitiesAPI/joa-employer-direct-job-postings-2026-10 on Hugging Face (CC BY 4.0)",
)


def parse(path: Path):
    records: list[SourceRecord] = []
    rejects: list[Reject] = []
    if not Path(path).exists():
        return records, rejects
    seen: set[str] = set()
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                rejects.append(Reject(source=META.name, reason="invalid_json", row_ref=f"line {i}"))
                continue
            url = (row.get("apply_url") or "").strip()
            parsed = parse_board_url(url)
            if parsed is None:
                # Most postings apply on the company's own site; that is not an ATS board.
                continue
            key = f"{parsed.ats}:{parsed.code.casefold()}"
            if key in seen:
                continue
            seen.add(key)
            name = (row.get("employer") or "").strip() or None
            records.append(SourceRecord(source=META.name, ats=parsed.ats, code=parsed.code, region=parsed.region, name=name, careersite_url=url, row_ref=f"line {i}"))
    return records, rejects
