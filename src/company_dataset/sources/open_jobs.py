"""Board slugs from elliottdehn/open-jobs (CC0): the boards its crawler ran as of September 2026.

    python -m company_dataset.sources.open_jobs --raw data/raw

Boards it lists as gone are left out. Listed boards are candidates: the list is weeks old, so each
still goes through normal validation.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import requests

from company_dataset.models import SourceMeta
from company_dataset.normalize import CORE_ATS, clean_code
from company_dataset.platforms import WORKABLE
from company_dataset.sources._harvest import parse_tokens, plausible_slug, write_tokens

COMMIT = "6f47bc050c9536c59ac771ca14aa507a25253348"
URL = f"https://raw.githubusercontent.com/elliottdehn/open-jobs/{COMMIT}/slugs.json"
ALLOWED = (*CORE_ATS, WORKABLE)

META = SourceMeta(
    name="open_jobs",
    description="Live board slugs from the open-jobs crawler",
    download_url=URL,
    filename="tokens.csv",
    license="CC0-1.0",
    license_url="https://github.com/elliottdehn/open-jobs/blob/main/LICENSE",
    license_note="Only board identifiers are read; every board is verified against its vendor before use.",
    attribution=f"elliottdehn/open-jobs, commit {COMMIT[:12]}",
    downloadable=False,
)


def tokens_from(data: dict, evidence: str) -> dict:
    tokens: dict = {}
    for ats in ALLOWED:
        for raw in (data.get("ats") or {}).get(ats) or []:
            code = clean_code(ats, raw) if isinstance(raw, str) else None
            if plausible_slug(code):
                tokens.setdefault((ats, code.casefold()), (code, "", evidence))
    return tokens


def parse(path: Path):
    return parse_tokens(path, META.name, allowed=ALLOWED)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="company_dataset.sources.open_jobs")
    p.add_argument("--raw", default="data/raw")
    a = p.parse_args(argv)
    resp = requests.get(URL, timeout=120, headers={"User-Agent": "company-dataset/1.0 (open dataset builder; polite; identifies itself)"})
    resp.raise_for_status()
    count = write_tokens(Path(a.raw) / META.name / META.filename, tokens_from(resp.json(), URL))
    print(json.dumps({"boards": count}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
