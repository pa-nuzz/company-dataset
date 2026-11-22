"""Board identifiers from the Portuguese web archive (Arquivo.pt), a second archive beyond Wayback.

    python -m company_dataset.sources.arquivo_cdx --raw data/raw

Its CDX index caps each answer, so hosts are read page by page. A finished page is saved at once.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlencode

from company_dataset.http import HttpClient, RequestError
from company_dataset.models import SourceMeta
from company_dataset.sources._harvest import board_from_url, parse_tokens, write_tokens

CDX = "https://arquivo.pt/wayback/cdx"
TARGETS = ["boards.greenhouse.io/*", "job-boards.greenhouse.io/*", "boards.eu.greenhouse.io/*", "jobs.lever.co/*", "jobs.eu.lever.co/*", "jobs.ashbyhq.com/*", "jobs.smartrecruiters.com/*", "careers.smartrecruiters.com/*", "*.breezy.hr"]
PAGE_SIZE = 5000
MAX_PAGES_PER_TARGET = 200

META = SourceMeta(
    name="arquivo_cdx",
    description="Arquivo.pt web archive index: URLs captured on the target ATS hosts",
    download_url=CDX,
    filename="tokens.csv",
    license="Arquivo.pt terms; only URLs are read",
    license_url="https://arquivo.pt/",
    license_note="Only URLs (factual board identifiers) are read from the index; every board is verified against its provider before use.",
    attribution="Arquivo.pt",
    downloadable=False,
)


def query(target: str, page: int) -> str:
    return f"{CDX}?{urlencode({'url': target, 'output': 'json', 'fl': 'url', 'limit': PAGE_SIZE, 'offset': page * PAGE_SIZE})}"


def harvest(raw_dir: Path, client: HttpClient, targets=TARGETS) -> dict:
    out = Path(raw_dir) / META.name
    out.mkdir(parents=True, exist_ok=True)
    tokens: dict = {}
    failed = []
    for target in targets:
        for page in range(MAX_PAGES_PER_TARGET):
            try:
                resp = client.get(query(target, page))
            except RequestError:
                failed.append(f"{target}#{page}")
                break
            if resp.status != 200:
                failed.append(f"{target}#{page}:{resp.status}")
                break
            lines = [line for line in resp.text.splitlines() if line.strip()]
            for line in lines:
                try:
                    url = json.loads(line).get("url", "")
                except (ValueError, AttributeError):
                    continue
                board = board_from_url(url)
                if board:
                    tokens.setdefault((board[0], board[1].casefold()), (board[1], board[2], url))
            if len(lines) < PAGE_SIZE:
                break
        print(f"[arquivo] {target}: {len(tokens)} boards", flush=True)
    write_tokens(out / META.filename, tokens)
    return {"boards": len(tokens), "failed": failed}


def parse(path: Path):
    return parse_tokens(path, META.name)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="company_dataset.sources.arquivo_cdx")
    p.add_argument("--raw", default="data/raw")
    a = p.parse_args(argv)
    print(json.dumps(harvest(Path(a.raw), HttpClient(timeout=120, per_host_interval=1.0, max_attempts=3)), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
