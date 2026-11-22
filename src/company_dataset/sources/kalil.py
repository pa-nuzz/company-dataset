from __future__ import annotations

import csv
import io
from pathlib import Path

from company_dataset.models import Reject, SourceMeta, SourceRecord
from company_dataset.normalize import SUPPORTED_ATS, parse_board_url

COMMIT = "6b44a1badc9bfbf5cf176f75265cc5729e520e99"

META = SourceMeta(
    name="kalil",
    description="ats-scrapers per-ATS company directories (name, slug, board URL) for 40+ ATS platforms",
    download_url=f"https://github.com/kalil0321/ats-scrapers/tree/{COMMIT}/ats-companies",
    filename="companies.csv",
    license="MIT",
    license_url=f"https://github.com/kalil0321/ats-scrapers/blob/{COMMIT}/LICENSE",
    license_note="MIT (c) 2026 Kalil Bouzigues covers the repository including ats-companies/*.csv. Each board is live-verified against the vendor before export.",
    attribution=f"kalil0321/ats-scrapers, commit {COMMIT[:12]} (2026-09-24)",
)

# Upstream file name -> our ATS id. Files for platforms we can't verify are not downloaded.
FILES = {
    "greenhouse": "greenhouse",
    "lever": "lever",
    "ashby": "ashby",
    "smartrecruiters": "smartrecruiters",
    "breezy": "breezy",
    "workable": "workable",
    "workday": "workday",
    "bamboohr": "bamboohr",
    "recruitee": "recruitee",
    "teamtailor": "teamtailor",
    "personio": "personio",
    "rippling": "rippling",
    "icims": "icims",
    "pinpoint": "pinpoint",
    "join_com": "join",
}
_BASE = f"https://raw.githubusercontent.com/kalil0321/ats-scrapers/{COMMIT}/ats-companies/"


def run_download(dest_dir: Path, client) -> None:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["file_ats", "name", "slug", "url"])
    for fname, ats in FILES.items():
        resp = client.get(_BASE + fname + ".csv")
        if resp.status != 200:
            raise RuntimeError(f"kalil {fname}.csv: HTTP {resp.status}")
        for row in csv.DictReader(io.StringIO(resp.text)):
            writer.writerow([ats, row.get("name") or "", row.get("slug") or "", row.get("url") or ""])
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / META.filename).write_text(out.getvalue(), encoding="utf-8")


def parse(path: Path):
    records: list[SourceRecord] = []
    rejects: list[Reject] = []
    if not Path(path).exists():
        return records, rejects
    with open(path, newline="", encoding="utf-8") as fh:
        for i, row in enumerate(csv.DictReader(fh), start=2):
            ref = f"line {i}"
            ats = row["file_ats"]
            if ats not in SUPPORTED_ATS:
                rejects.append(Reject(source=META.name, reason="unsupported_ats", detail=ats, row_ref=ref, raw=dict(row)))
                continue
            url = (row.get("url") or "").strip()
            parsed = parse_board_url(url)
            if parsed is None:
                rejects.append(Reject(source=META.name, reason="unparseable_url", detail=url, row_ref=ref, ats=ats, raw=dict(row)))
                continue
            if parsed.ats != ats:
                rejects.append(Reject(source=META.name, reason="platform_url_mismatch", detail=f"file={ats} url_host_ats={parsed.ats}", row_ref=ref, ats=ats, raw=dict(row)))
                continue
            name = (row.get("name") or "").strip() or None
            records.append(SourceRecord(source=META.name, ats=ats, code=parsed.code, region=parsed.region, name=name, careersite_url=url, row_ref=ref))
    return records, rejects
