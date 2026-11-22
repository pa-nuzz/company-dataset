"""Board identifiers for the 10 additional ATS, harvested from Common Crawl's CDX index, with provenance.

Same method as sources/commoncrawl.py (Range reads of the static cluster.idx and index
blocks on data.commoncrawl.org; URLs only, no page content), for the hosts of BambooHR,
iCIMS, JOIN, Personio, Pinpoint, Recruitee, Rippling, Teamtailor, Workable and Workday.

For every board the output keeps where it was seen: first and last capture (crawl,
CDX timestamp, URL), the number of crawls and captures, and whether any capture returned
HTTP 200. A discovered identifier is only a candidate: it says nothing about the board
being live or about which company runs it.

    python -m company_dataset.sources.commoncrawl_extra --out data/raw_cc_extra_2026-11-01/commoncrawl_extra
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from company_dataset.models import Reject, SourceMeta, SourceRecord
from company_dataset.normalize import parse_board_url
from company_dataset.platforms import EXTRA_ATS
from company_dataset.sources.commoncrawl import CRAWLS, INDEX_BASE, _range, cluster_size, find_blocks

PREFIXES = [
    "com,bamboohr,", "com,icims,", "com,join)/companies/", "de,personio,jobs,", "com,personio,jobs,", "com,pinpointhq,",
    "com,recruitee,", "com,rippling,ats)/", "com,teamtailor,", "com,workable,", "com,myworkdayjobs,", "com,myworkdaysite,",
]

META = SourceMeta(
    name="commoncrawl_extra",
    description=f"Common Crawl CDX index harvest of board URLs for the 10 additional ATS ({len(CRAWLS)} crawls, {CRAWLS[-1]} to {CRAWLS[0]})",
    download_url="https://data.commoncrawl.org/cc-index/collections/",
    filename="tokens.csv",
    license="Common Crawl Terms of Use",
    license_url="https://commoncrawl.org/terms-of-use/",
    license_note="Only URLs (factual board identifiers) are read from the index; every board is verified against its provider before use.",
    attribution="Common Crawl Foundation, crawls " + ", ".join(CRAWLS),
)

COLUMNS = ["ats", "code", "region", "first_crawl", "first_timestamp", "first_url", "last_crawl", "last_timestamp", "last_url",
           "crawls_seen", "captures", "any_http_200"]


def observations_in_block(data: bytes, prefix: str) -> dict[tuple, list]:
    """(ats, code, region) -> [first_ts, first_url, last_ts, last_url, captures, any_200] within one block."""
    found: dict[tuple, list] = {}
    for line in gzip.decompress(data).decode("utf-8", "ignore").splitlines():
        if not line.startswith(prefix):
            continue
        parts = line.split(" ", 2)
        if len(parts) < 3:
            continue
        try:
            rec = json.loads(parts[2])
        except ValueError:
            continue
        parsed = parse_board_url(rec.get("url", ""))
        if not parsed or parsed.ats not in EXTRA_ATS:
            continue
        ts, url, ok = parts[1], rec["url"], rec.get("status") == "200"
        o = found.get((parsed.ats, parsed.code, parsed.region))
        if o is None:
            found[(parsed.ats, parsed.code, parsed.region)] = [ts, url, ts, url, 1, ok]
            continue
        if ts < o[0]:
            o[0], o[1] = ts, url
        if ts > o[2]:
            o[2], o[3] = ts, url
        o[4] += 1
        o[5] = o[5] or ok
    return found


def harvest_key(client, crawl: str, prefix: str, size: int) -> dict:
    blocks = find_blocks(client, crawl, prefix, size)
    merged: dict[tuple, list] = {}
    for shard, offset, length in blocks:
        data = _range(client, INDEX_BASE.format(crawl=crawl) + shard, offset, offset + length - 1)
        for k, o in observations_in_block(data, prefix).items():
            m = merged.get(k)
            if m is None:
                merged[k] = o
                continue
            if o[0] < m[0]:
                m[0], m[1] = o[0], o[1]
            if o[2] > m[2]:
                m[2], m[3] = o[2], o[3]
            m[4] += o[4]
            m[5] = m[5] or o[5]
    return {"blocks": [list(b) for b in blocks], "observations": [[*k, *o] for k, o in sorted(merged.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] or ""))]}


def run_download(dest_dir: Path, client, crawls=CRAWLS, workers: int = 4) -> dict:
    """Resumable: each finished (crawl, prefix) is cached; failures are reported, never written as empty."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    cache_path = dest_dir / "harvest_cache.json.gz"
    cache = json.loads(gzip.decompress(cache_path.read_bytes())) if cache_path.exists() else {}
    failures = []
    sizes = {}
    for crawl in crawls:
        try:
            sizes[crawl] = cluster_size(client, crawl)
        except Exception as exc:
            failures.append(f"{crawl}: {exc}")
    todo = [(c, p) for c in crawls if c in sizes for p in PREFIXES if f"{c}|{p}" not in cache]

    def one(cp):
        crawl, prefix = cp
        try:
            return cp, harvest_key(client, crawl, prefix, sizes[crawl]), None
        except Exception as exc:
            return cp, None, str(exc)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, ((crawl, prefix), res, err) in enumerate(ex.map(one, todo), 1):
            if err:
                failures.append(f"{crawl}|{prefix}: {err}")
                continue
            cache[f"{crawl}|{prefix}"] = res
            print(f"[commoncrawl_extra] {i}/{len(todo)} {crawl} {prefix}: {len(res['observations'])} boards, {len(res['blocks'])} blocks", flush=True)
            if i % 10 == 0:
                cache_path.write_bytes(gzip.compress(json.dumps(cache).encode()))
    cache_path.write_bytes(gzip.compress(json.dumps(cache).encode()))
    (dest_dir / "harvest_failures.json").write_text(json.dumps(failures, indent=1))
    if failures:
        raise SystemExit(f"incomplete harvest ({len(failures)} failures), rerun to resume: {failures[:3]}")
    return {"boards": write_tokens(dest_dir / META.filename, cache, crawls), "keys": len(cache)}


def write_tokens(path: Path, cache: dict, crawls=CRAWLS) -> int:
    agg: dict[tuple, dict] = {}
    for key in sorted(cache):
        crawl = key.split("|", 1)[0]
        if crawl not in crawls:
            continue
        for ats, code, region, fts, furl, lts, lurl, n, ok in cache[key]["observations"]:
            a = agg.setdefault((ats, code, region or ""), {"first": (fts, crawl, furl), "last": (lts, crawl, lurl), "crawls": set(), "captures": 0, "ok": False})
            a["first"] = min(a["first"], (fts, crawl, furl))
            a["last"] = max(a["last"], (lts, crawl, lurl))
            a["crawls"].add(crawl)
            a["captures"] += n
            a["ok"] = a["ok"] or ok
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(COLUMNS)
        for (ats, code, region), a in sorted(agg.items()):
            w.writerow([ats, code, region, a["first"][1], a["first"][0], a["first"][2], a["last"][1], a["last"][0], a["last"][2],
                        len(a["crawls"]), a["captures"], int(a["ok"])])
    tmp.replace(path)
    return len(agg)


def parse(path: Path):
    records: list[SourceRecord] = []
    rejects: list[Reject] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for i, row in enumerate(csv.DictReader(fh), start=2):
            ref = f"line {i}; first seen {row['first_crawl']} {row['first_timestamp']}; last seen {row['last_crawl']} {row['last_timestamp']}; crawls {row['crawls_seen']}"
            records.append(SourceRecord(source=META.name, ats=row["ats"], code=row["code"], region=row["region"] or None, row_ref=ref,
                                        careersite_url=row["last_url"]))
    return records, rejects


def main(argv=None) -> int:
    from company_dataset.http import HttpClient

    p = argparse.ArgumentParser(prog="company_dataset.sources.commoncrawl_extra")
    p.add_argument("--out", required=True)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--crawls", default=",".join(CRAWLS), help="comma-separated crawl ids (default: all)")
    a = p.parse_args(argv)
    # data.commoncrawl.org answers most ranges in ~1 s but stalls some for minutes: fail fast, retry often.
    client = HttpClient(timeout=20, per_host_interval=0.1, max_attempts=10)
    print(json.dumps(run_download(Path(a.out), client, crawls=a.crawls.split(","), workers=a.workers)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
