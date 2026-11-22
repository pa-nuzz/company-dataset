from __future__ import annotations

import csv
from pathlib import Path

from company_dataset.models import Reject, SourceMeta, SourceRecord
from company_dataset.normalize import ASHBY, GREENHOUSE, LEVER, clean_code, parse_board_url
from company_dataset.platforms import EXTRA_ATS, PERSONIO, RECRUITEE, RIPPLING, WORKABLE, WORKDAY

META = SourceMeta(
    name="kaleb",
    description="Kaleb Confer Job Board Directory (12,237 board addresses across eight ATS platforms, live-confirmed)",
    download_url="https://kalebconfer-sys.github.io/job-board-directory/job-board-directory.csv",
    filename="job-board-directory.csv",
    license="CC0-1.0",
    license_url="https://creativecommons.org/publicdomain/zero/1.0/",
    license_note="No LICENSE file in the repository; the publisher's site declares CC0 1.0 in its schema.org Dataset metadata and states 'no signup, no key, no attribution required' (checked 2026-10-17). Attributed anyway.",
    attribution="kalebconfer-sys/job-board-directory (revivacatch.com), built 2026-10-15",
)

PLATFORM_MAP = {
    "greenhouse": GREENHOUSE,
    "lever": LEVER,
    "ashby": ASHBY,
    "workable": WORKABLE,
    "workday": WORKDAY,
    "recruitee": RECRUITEE,
    "personio": PERSONIO,
    "rippling": RIPPLING,
}


def parse(path: Path):
    records: list[SourceRecord] = []
    rejects: list[Reject] = []
    if not Path(path).exists():
        return records, rejects
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for i, row in enumerate(reader, start=2):
            platform = (row.get("platform") or "").strip().lower()
            ref = f"line {i}"
            ats = PLATFORM_MAP.get(platform)
            if ats is None:
                rejects.append(Reject(source=META.name, reason="unsupported_ats", detail=platform or "(empty)", row_ref=ref, raw=dict(row)))
                continue
            if ats in EXTRA_ATS:
                # Tokens on these platforms aren't the full board identifier (Workday needs
                # host and site), so the board URL is the source of truth.
                board_url = (row.get("board_url") or "").strip()
                parsed = parse_board_url(board_url) if board_url else None
                if parsed is None or parsed.ats != ats:
                    rejects.append(Reject(source=META.name, reason="unparseable_url", detail=board_url, row_ref=ref, ats=ats, raw=dict(row)))
                    continue
                name = (row.get("company") or "").strip() or None
                records.append(SourceRecord(source=META.name, ats=ats, code=parsed.code, region=parsed.region, name=name, careersite_url=board_url, row_ref=ref))
                continue
            token = (row.get("token") or "").strip()
            raw_token, token = token, clean_code(ats, token)
            if token is None:
                rejects.append(Reject(source=META.name, reason="invalid_slug", detail=raw_token, row_ref=ref, ats=ats, raw=dict(row)))
                continue
            region = "eu" if (row.get("region") or "").strip().upper() == "EU" else None
            board_url = (row.get("board_url") or "").strip() or None
            code = token
            parsed = parse_board_url(board_url) if board_url else None
            if parsed is not None and parsed.ats != ats:
                rejects.append(Reject(source=META.name, reason="platform_url_mismatch", detail=f"label={platform} url_host_ats={parsed.ats}", row_ref=ref, ats=ats, code=token, raw=dict(row)))
                continue
            if parsed is not None and parsed.code.casefold() == token.casefold():
                code = parsed.code
                region = parsed.region or region
            name = (row.get("company") or "").strip() or None
            records.append(
                SourceRecord(
                    source=META.name,
                    ats=ats,
                    code=code,
                    region=region,
                    name=name,
                    careersite_url=board_url,
                    row_ref=ref,
                )
            )
    return records, rejects
