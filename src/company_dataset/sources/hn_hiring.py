"""Board identifiers from Hacker News comments, including every "Who is hiring?" thread.

    python -m company_dataset.sources.hn_hiring --raw data/raw

The Algolia search API returns at most 1,000 results a query, so each host is searched one quarter at a
time, back to OLDEST_YEAR. A finished window is saved at once, so a stopped harvest resumes.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.parse import urlencode

from company_dataset.http import HttpClient, RequestError
from company_dataset.models import SourceMeta
from company_dataset.sources._harvest import boards_in_text, parse_tokens, write_tokens

SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
HOSTS = ["jobs.lever.co", "boards.greenhouse.io", "job-boards.greenhouse.io", "jobs.ashbyhq.com", "jobs.smartrecruiters.com", "careers.smartrecruiters.com", "breezy.hr"]
OLDEST_YEAR = 2014
HITS_PER_PAGE = 100
SECONDS_BETWEEN_REQUESTS = 1.0

META = SourceMeta(
    name="hn_hiring",
    description="Hacker News comments linking to Greenhouse, Lever, Ashby, SmartRecruiters and Breezy HR boards",
    download_url=SEARCH,
    filename="tokens.csv",
    license="Hacker News content via the public Algolia API; only URLs are kept",
    license_url="https://hn.algolia.com/api",
    license_note="Only URLs (factual board identifiers) are read from comments; every board is verified against its provider before use.",
    attribution="Hacker News, via Algolia HN Search",
    downloadable=False,
)


def quarters(now: datetime, oldest_year: int = OLDEST_YEAR) -> list[tuple[int, int]]:
    """(start, end) epoch seconds of each quarter up to now, newest first."""
    out = []
    for year in range(oldest_year, now.year + 1):
        for month in (1, 4, 7, 10):
            start = datetime(year, month, 1, tzinfo=timezone.utc)
            end = datetime(year + 1, 1, 1, tzinfo=timezone.utc) if month == 10 else datetime(year, month + 3, 1, tzinfo=timezone.utc)
            if start < now:
                out.append((int(start.timestamp()), int(end.timestamp())))
    return out[::-1]


def search_url(host: str, start: int, end: int, page: int) -> str:
    return SEARCH + "?" + urlencode({"tags": "comment", "query": host, "hitsPerPage": HITS_PER_PAGE, "page": page, "numericFilters": f"created_at_i>={start},created_at_i<{end}"})


def _window(client: HttpClient, host: str, start: int, end: int) -> tuple[dict, bool]:
    found: dict = {}
    page, pages = 0, 1
    while page < pages:
        try:
            resp = client.get(search_url(host, start, end, page))
        except RequestError:
            return found, False
        if resp.status != 200:
            return found, False
        data = resp.json()
        pages = data.get("nbPages") or 0
        for hit in data.get("hits") or []:
            evidence = f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
            for key, value in boards_in_text(unescape(hit.get("comment_text") or ""), evidence).items():
                found.setdefault(key, value)
        page += 1
    return found, True


def harvest(raw_dir: Path, client: HttpClient, now: datetime | None = None, hosts=HOSTS) -> dict:
    out = Path(raw_dir) / META.name
    out.mkdir(parents=True, exist_ok=True)
    log = out / "windows.jsonl"
    done, tokens = set(), {}
    if log.exists():
        with open(log, encoding="utf-8") as fh:
            for line in fh:
                entry = json.loads(line)
                done.add((entry["host"], entry["start"]))
                for ats, code, region, evidence in entry["tokens"]:
                    tokens.setdefault((ats, code.casefold()), (code, region, evidence))
    failed = 0
    windows = quarters(now or datetime.now(timezone.utc))
    with open(log, "a", encoding="utf-8") as fh:
        for host in hosts:
            for start, end in windows:
                if (host, start) in done:
                    continue
                found, ok = _window(client, host, start, end)
                if not ok:
                    failed += 1
                    continue
                fh.write(json.dumps({"host": host, "start": start, "tokens": [[k[0], v[0], v[1], v[2]] for k, v in found.items()]}) + "\n")
                fh.flush()
                for key, value in found.items():
                    tokens.setdefault(key, value)
            print(f"[hn] {host}: {len(tokens)} boards", flush=True)
    write_tokens(out / META.filename, tokens)
    return {"boards": len(tokens), "failed_windows": failed}


def parse(path: Path):
    return parse_tokens(path, META.name)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="company_dataset.sources.hn_hiring")
    p.add_argument("--raw", default="data/raw")
    a = p.parse_args(argv)
    print(json.dumps(harvest(Path(a.raw), HttpClient(timeout=60, per_host_interval=SECONDS_BETWEEN_REQUESTS, max_attempts=3)), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
