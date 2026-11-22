"""Board tokens harvested from Common Crawl's published CDX index shards.

The index *server* (index.commoncrawl.org) is frequently overloaded and times out,
which silently produced empty harvests before. Instead we read the static files on
data.commoncrawl.org: binary-search the sorted cluster.idx with HTTP Range requests
to find the index blocks for each ATS host, then fetch just those gzip blocks.
Only URL path segments (board tokens) are kept; no page content is used.
"""
from __future__ import annotations

import csv
import gzip
import json
from pathlib import Path

from company_dataset.models import Reject, SourceMeta, SourceRecord
from company_dataset.normalize import SUPPORTED_ATS, clean_code, parse_board_url

CRAWLS = [
    "CC-MAIN-2026-39", "CC-MAIN-2026-34", "CC-MAIN-2026-21", "CC-MAIN-2026-08",
    "CC-MAIN-2025-51", "CC-MAIN-2025-38", "CC-MAIN-2025-26", "CC-MAIN-2025-13",
    "CC-MAIN-2024-51", "CC-MAIN-2024-42", "CC-MAIN-2024-33", "CC-MAIN-2024-26", "CC-MAIN-2024-18", "CC-MAIN-2024-10",
    "CC-MAIN-2023-50", "CC-MAIN-2023-40", "CC-MAIN-2023-23", "CC-MAIN-2023-14", "CC-MAIN-2023-06",
    "CC-MAIN-2022-49", "CC-MAIN-2022-40", "CC-MAIN-2022-33", "CC-MAIN-2022-27", "CC-MAIN-2022-21", "CC-MAIN-2022-05",
    "CC-MAIN-2021-49", "CC-MAIN-2021-43", "CC-MAIN-2021-39", "CC-MAIN-2021-31", "CC-MAIN-2021-25", "CC-MAIN-2021-17",
    "CC-MAIN-2021-10", "CC-MAIN-2021-04",
    "CC-MAIN-2020-50", "CC-MAIN-2020-45", "CC-MAIN-2020-40", "CC-MAIN-2020-34", "CC-MAIN-2020-29", "CC-MAIN-2020-24",
    "CC-MAIN-2020-16", "CC-MAIN-2020-10",
]
# SURT prefixes of the public board hosts (SURT reverses the host labels).
PREFIXES = [
    "io,greenhouse,boards)/",
    "io,greenhouse,job-boards)/",
    "co,lever,jobs)/",
    "co,lever,eu,jobs)/",
    "com,ashbyhq,jobs)/",
    "com,smartrecruiters,careers)/",
    "com,smartrecruiters,jobs)/",
    "com,smartrecruiters,api)/",
    "hr,breezy,",
]

META = SourceMeta(
    name="commoncrawl",
    description=f"Common Crawl CDX index harvest of board URLs on Greenhouse, Lever, Ashby, SmartRecruiters and Breezy HR hosts ({len(CRAWLS)} crawls, {CRAWLS[-1]} to {CRAWLS[0]})",
    download_url="https://data.commoncrawl.org/cc-index/collections/",
    filename="tokens.csv",
    license="Common Crawl Terms of Use",
    license_url="https://commoncrawl.org/terms-of-use/",
    license_note="Only URL path segments (factual board identifiers) are extracted from the index; every token is independently live-verified against the vendor's public API before export. No page content is used.",
    attribution="Common Crawl Foundation, crawls " + ", ".join(CRAWLS),
)

INDEX_BASE = "https://data.commoncrawl.org/cc-index/collections/{crawl}/indexes/"
PROBE = 65536


class HarvestError(Exception):
    pass


def _range(client, url: str, start: int, end: int) -> bytes:
    resp = client.get(url, headers={"Range": f"bytes={start}-{end}"}, binary=True)
    if resp.status not in (200, 206):
        raise HarvestError(f"HTTP {resp.status} for {url} [{start}-{end}]")
    return resp.content


def _lines_at(client, url: str, offset: int, length: int) -> list[str]:
    lines = _range(client, url, offset, offset + length).decode("utf-8", "ignore").split("\n")
    # Drop the partial first line (unless at file start) and the partial last line.
    return (lines[1:-1] if offset else lines[:-1])


def find_blocks(client, crawl: str, prefix: str, total_size: int) -> list[tuple[str, int, int]]:
    """(shard, offset, length) of every index block that can hold keys with `prefix`."""
    url = INDEX_BASE.format(crawl=crawl) + "cluster.idx"
    lo, hi = 0, total_size
    while hi - lo > PROBE:
        mid = (lo + hi) // 2
        lines = _lines_at(client, url, mid, PROBE)
        if lines and lines[0].split(" ")[0] < prefix:
            lo = mid
        else:
            hi = mid
    blocks: list[tuple[str, int, int]] = []
    offset, prev = lo, None
    window = PROBE * 4
    while offset < total_size:
        lines = _lines_at(client, url, offset, window)
        if not lines:
            break
        for line in lines:
            key = line.split(" ")[0]
            fields = line.split("\t")
            if len(fields) < 4:
                continue
            block = (fields[1], int(fields[2]), int(fields[3]))
            if key < prefix:
                prev = block
                continue
            if prev is not None:
                blocks.append(prev)
                prev = None
            if not key.startswith(prefix):
                return list(dict.fromkeys(blocks))
            blocks.append(block)
        offset += window - 4096
    return list(dict.fromkeys(blocks))


def tokens_in_block(data: bytes, prefix: str) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for line in gzip.decompress(data).decode("utf-8", "ignore").splitlines():
        if not line.startswith(prefix):
            continue
        parts = line.split(" ", 2)
        if len(parts) < 3:
            continue
        try:
            url = json.loads(parts[2]).get("url", "")
        except ValueError:
            continue
        parsed = parse_board_url(url)
        if parsed:
            found.add((parsed.ats, parsed.code))
    return found


def cluster_size(client, crawl: str) -> int:
    url = INDEX_BASE.format(crawl=crawl) + "cluster.idx"
    resp = client.session.head(url, timeout=client.timeout)
    if resp.status_code != 200:
        raise HarvestError(f"HTTP {resp.status_code} for {url}")
    return int(resp.headers["Content-Length"])


def run_download(dest_dir: Path, client) -> None:
    """Harvest tokens per (crawl, prefix); cache successes only, never write a partial file."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    cache_path = dest_dir / "harvest_cache.json"
    cache: dict[str, list] = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except ValueError:
            cache = {}
    failures = []
    for crawl in CRAWLS:
        try:
            size = cluster_size(client, crawl)
        except Exception as exc:
            failures.append(f"{crawl}: {exc}")
            continue
        for prefix in PREFIXES:
            key = f"{crawl}|{prefix}"
            if key in cache:
                continue
            try:
                found: set[tuple[str, str]] = set()
                for shard, offset, length in find_blocks(client, crawl, prefix, size):
                    data = _range(client, INDEX_BASE.format(crawl=crawl) + shard, offset, offset + length - 1)
                    found |= tokens_in_block(data, prefix)
            except Exception as exc:
                failures.append(f"{key}: {exc}")
                continue
            cache[key] = sorted(found)
            cache_path.write_text(json.dumps(cache), encoding="utf-8")
            print(f"[commoncrawl] {key}: {len(found)} tokens")
    if failures:
        raise HarvestError("incomplete harvest, rerun download to resume: " + "; ".join(failures[:5]))
    write_tokens(dest_dir / META.filename, cache)


def write_tokens(path: Path, cache: dict[str, list]) -> int:
    first_seen: dict[tuple[str, str], str] = {}
    for key in sorted(cache):
        if "|" not in key:
            continue  # entry from an older cache format
        crawl = key.split("|", 1)[0]
        for ats, code in cache[key]:
            first_seen.setdefault((ats, code), crawl)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ats", "code", "crawl"])
        for (ats, code), crawl in sorted(first_seen.items()):
            writer.writerow([ats, code, crawl])
    tmp.replace(path)
    print(f"[commoncrawl] wrote {len(first_seen)} unique tokens")
    return len(first_seen)


def parse(path: Path):
    records: list[SourceRecord] = []
    rejects: list[Reject] = []
    if not Path(path).exists():
        return records, rejects
    with open(path, newline="", encoding="utf-8") as fh:
        for i, row in enumerate(csv.DictReader(fh), start=2):
            ats = (row.get("ats") or "").strip()
            code = (row.get("code") or "").strip()
            ref = f"line {i}"
            if ats not in SUPPORTED_ATS:
                rejects.append(Reject(source=META.name, reason="unsupported_ats", detail=ats, row_ref=ref))
                continue
            raw_code, code = code, clean_code(ats, code)
            if code is None:
                rejects.append(Reject(source=META.name, reason="invalid_slug", detail=raw_code, row_ref=ref, ats=ats))
                continue
            records.append(SourceRecord(source=META.name, ats=ats, code=code, row_ref=ref))
    return records, rejects
