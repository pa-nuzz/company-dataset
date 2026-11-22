"""Board identifiers from the Internet Archive's CDX index: every URL ever captured on the ATS hosts.

    python -m company_dataset.sources.wayback_cdx --raw data/raw

Pages are fetched one at a time and each finished page is appended to pages.jsonl, so a stopped
harvest resumes where it was. tokens.csv is written only when every page is done. A discovered
identifier is only a candidate: old captures are often of boards long gone.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from urllib.parse import urlencode

from company_dataset.http import HttpClient, RequestError
from company_dataset.models import SourceMeta
from company_dataset.sources._harvest import board_from_url, parse_tokens, write_tokens

CDX = "https://web.archive.org/cdx/search/cdx"
# (url, matchType). The collapse option is not used: on paged queries it times out with a 504.
TARGETS = [
    ("jobs.ashbyhq.com/*", None),
    ("boards.greenhouse.io/*", None),
    ("job-boards.greenhouse.io/*", None),
    ("boards.eu.greenhouse.io/*", None),
    ("job-boards.eu.greenhouse.io/*", None),
    ("jobs.lever.co/*", None),
    ("jobs.eu.lever.co/*", None),
    ("jobs.smartrecruiters.com/*", None),
    ("careers.smartrecruiters.com/*", None),
    ("breezy.hr", "domain"),
]
MIN_SECONDS_BETWEEN_REQUESTS = 4.0  # at most 15 requests a minute
BACKOFF_SECONDS = (30, 60, 120, 240, 480, 600)
RETRY_STATUSES = (429, 500, 502, 503, 504)
# The archive goes offline for spells; finished pages are on disk, so waiting costs nothing.
MAX_RESTARTS = 24
RESTART_WAIT_SECONDS = 1800

META = SourceMeta(
    name="wayback_cdx_2026_11_03",
    description="Internet Archive Wayback CDX index: URLs captured on Greenhouse, Lever, Ashby, SmartRecruiters and Breezy HR hosts",
    download_url=CDX,
    filename="tokens.csv",
    license="Internet Archive Terms of Use",
    license_url="https://archive.org/about/terms.php",
    license_note="Only URLs (factual board identifiers) are read from the index; every board is verified against its provider before use.",
    attribution="Internet Archive, Wayback Machine CDX Server",
    downloadable=False,
)


class HarvestStopped(Exception):
    """The archive kept refusing: finished pages are saved, run again to resume."""


def _query(target: tuple[str, str | None], **extra) -> str:
    url, match = target
    params = {"url": url, **extra}
    if match:
        params["matchType"] = match
    return f"{CDX}?{urlencode(params)}"


def _get(client: HttpClient, url: str, sleep) -> str:
    for wait in (*BACKOFF_SECONDS, None):
        try:
            resp = client.get(url)
        except RequestError:
            status = None
        else:
            if resp.status == 200:
                return resp.text
            status = resp.status
            if status not in RETRY_STATUSES:
                raise HarvestStopped(f"HTTP {status} for {url}")
        if wait is None:
            raise HarvestStopped(f"still failing (last status {status}) for {url}")
        sleep(wait)
    raise HarvestStopped(url)


def page_count(client: HttpClient, target: tuple[str, str | None], sleep) -> int:
    # Asked together with fl=..., the server answers "-" instead of a number.
    text = _get(client, _query(target, showNumPages="true"), sleep).strip()
    if not text.isdigit():
        raise HarvestStopped(f"no page count for {target[0]}: {text[:80]!r}")
    return int(text)


def harvest(raw_dir: Path, client: HttpClient, sleep=time.sleep, targets=TARGETS, max_pages: int | None = None) -> dict:
    """max_pages caps pages fetched in this call (for trial runs); tokens.csv waits for a complete harvest."""
    out = Path(raw_dir) / META.name
    out.mkdir(parents=True, exist_ok=True)
    log = out / "pages.jsonl"
    done, tokens = set(), {}
    if log.exists():
        with open(log, encoding="utf-8") as fh:
            for line in fh:
                entry = json.loads(line)
                done.add((entry["target"], entry["page"]))
                for ats, code, region, url in entry["tokens"]:
                    tokens.setdefault((ats, code.casefold()), (code, region, url))
    fetched = 0
    complete = True
    with open(log, "a", encoding="utf-8") as fh:
        for target in targets:
            pages = page_count(client, target, sleep)
            for page in range(pages):
                if (target[0], page) in done:
                    continue
                if max_pages is not None and fetched >= max_pages:
                    complete = False
                    break
                seen = {}
                for line in _get(client, _query(target, page=page, fl="original"), sleep).splitlines():
                    url = line.strip()
                    board = board_from_url(url) if url else None
                    if board:
                        seen.setdefault((board[0], board[1].casefold()), (board[0], board[1], board[2], url))
                fh.write(json.dumps({"target": target[0], "page": page, "tokens": list(seen.values())}) + "\n")
                fh.flush()
                for key, (ats, code, region, url) in seen.items():
                    tokens.setdefault(key, (code, region, url))
                fetched += 1
                print(f"[wayback] {target[0]} page {page + 1}/{pages}: {len(seen)} boards, {len(tokens)} total", flush=True)
            else:
                continue
            break
    if complete:
        write_tokens(out / META.filename, tokens)
    return {"pages_fetched": fetched, "boards": len(tokens), "complete": complete}


def rebuild_tokens(raw_dir: Path) -> int:
    """tokens.csv from the pages finished so far, for a harvest stopped before the end."""
    out = Path(raw_dir) / META.name
    tokens: dict = {}
    with open(out / "pages.jsonl", encoding="utf-8") as fh:
        for line in fh:
            for ats, code, region, url in json.loads(line)["tokens"]:
                tokens.setdefault((ats, code.casefold()), (code, region, url))
    return write_tokens(out / META.filename, tokens)


def parse(path: Path):
    return parse_tokens(path, META.name)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="company_dataset.sources.wayback_cdx")
    p.add_argument("--raw", default="data/raw")
    p.add_argument("--max-pages", type=int, default=None)
    p.add_argument("--rebuild", action="store_true", help="write tokens.csv from finished pages and stop")
    a = p.parse_args(argv)
    if a.rebuild:
        print(json.dumps({"boards": rebuild_tokens(Path(a.raw))}))
        return 0
    client = HttpClient(timeout=180, max_attempts=1, per_host_interval=MIN_SECONDS_BETWEEN_REQUESTS)
    for attempt in range(MAX_RESTARTS):
        try:
            print(json.dumps(harvest(Path(a.raw), client, max_pages=a.max_pages), indent=1))
            return 0
        except HarvestStopped as exc:
            print(f"[wayback] stopped ({exc}); waiting {RESTART_WAIT_SECONDS}s, attempt {attempt + 1}/{MAX_RESTARTS}", flush=True)
            time.sleep(RESTART_WAIT_SECONDS)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
