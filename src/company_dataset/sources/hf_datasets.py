"""Board identifiers from public job datasets on Hugging Face.

    python -m company_dataset.sources.hf_datasets --raw data/raw

Finds datasets by search and reads the Parquet files of those under a permissive license, for URLs
only. Datasets with non-commercial, custom or missing licenses are listed in report.json and not read.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb

from company_dataset.http import HttpClient, RequestError
from company_dataset.models import SourceMeta
from company_dataset.sources._harvest import boards_in_text, parse_tokens, write_tokens

API = "https://huggingface.co/api/datasets"
PARQUET = "https://datasets-server.huggingface.co/parquet"
QUERIES = ["job postings", "jobs ats", "greenhouse lever", "job listings", "ashby jobs", "job board", "careers jobs dataset", "company careers"]
PERMISSIVE = frozenset(("cc0-1.0", "cc-by-4.0", "cc-by-3.0", "mit", "apache-2.0", "odc-by", "pddl", "unlicense", "cdla-permissive-2.0"))
MAX_DATASET_BYTES = 2_000_000_000
BATCH_ROWS = 5000

META = SourceMeta(
    name="hf_datasets",
    description="Permissively licensed Hugging Face job datasets that contain ATS board URLs",
    download_url=API,
    filename="tokens.csv",
    license="Per dataset, permissive only; report.json lists each",
    license_url="https://huggingface.co/datasets",
    license_note="Only URLs (factual board identifiers) are read; every board is verified against its provider before use.",
    attribution="Dataset authors on Hugging Face, listed in report.json",
    downloadable=False,
)


def license_of(dataset: dict) -> str:
    return next((t.split(":", 1)[1] for t in dataset.get("tags") or [] if t.startswith("license:")), "none")


def candidates(client: HttpClient) -> dict[str, str]:
    """dataset id -> license."""
    found: dict[str, str] = {}
    for query in QUERIES:
        resp = client.get(f"{API}?search={query.replace(' ', '%20')}&limit=100&sort=downloads")
        if resp.status == 200:
            for d in resp.json():
                found.setdefault(d["id"], license_of(d))
    return found


def parquet_urls(client: HttpClient, dataset: str) -> list[str]:
    try:
        resp = client.get(f"{PARQUET}?dataset={dataset}")
    except RequestError:
        return []
    if resp.status != 200:
        return []
    files = resp.json().get("parquet_files") or []
    if sum(f.get("size") or 0 for f in files) > MAX_DATASET_BYTES:
        return []
    return [f["url"] for f in files]


def scan_rows(batches, evidence: str) -> dict:
    found: dict = {}
    for text in batches:
        for key, value in boards_in_text(text, evidence).items():
            found.setdefault(key, value)
    return found


def _rows_as_text(con, url: str):
    cur = con.execute("SELECT CAST(to_json(t) AS VARCHAR) FROM read_parquet(?) t", [url])
    while True:
        rows = cur.fetchmany(BATCH_ROWS)
        if not rows:
            return
        yield "\n".join(r[0] for r in rows).replace("\\/", "/")


def harvest(raw_dir: Path, client: HttpClient) -> dict:
    out = Path(raw_dir) / META.name
    out.mkdir(parents=True, exist_ok=True)
    log = out / "datasets.jsonl"
    done, tokens = {}, {}
    if log.exists():
        with open(log, encoding="utf-8") as fh:
            for line in fh:
                entry = json.loads(line)
                done[entry["dataset"]] = entry
                for ats, code, region in entry["tokens"]:
                    tokens.setdefault((ats, code.casefold()), (code, region, f"https://huggingface.co/datasets/{entry['dataset']}"))
    found = candidates(client)
    skipped = sorted(f"{d} ({lic})" for d, lic in found.items() if lic not in PERMISSIVE)
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET threads=4;")
    with open(log, "a", encoding="utf-8") as fh:
        for dataset, lic in sorted(found.items()):
            if lic not in PERMISSIVE or dataset in done:
                continue
            evidence = f"https://huggingface.co/datasets/{dataset}"
            boards: dict = {}
            status = "read"
            try:
                for url in parquet_urls(client, dataset):
                    boards.update(scan_rows(_rows_as_text(con, url), evidence))
            except duckdb.Error as exc:
                status = f"failed: {str(exc)[:120]}"
            entry = {"dataset": dataset, "license": lic, "status": status, "tokens": [[k[0], v[0], v[1]] for k, v in boards.items()]}
            fh.write(json.dumps(entry) + "\n")
            fh.flush()
            done[dataset] = entry
            for key, value in boards.items():
                tokens.setdefault(key, value)
            print(f"[hf] {dataset} ({lic}): {len(boards)} boards, {len(tokens)} total", flush=True)
    write_tokens(out / META.filename, tokens)
    (out / "report.json").write_text(json.dumps({"read": [{"dataset": e["dataset"], "license": e["license"], "status": e["status"], "boards": len(e["tokens"])} for e in done.values()], "skipped_for_license": skipped}, indent=1))
    return {"datasets_read": len(done), "skipped_for_license": len(skipped), "boards": len(tokens)}


def parse(path: Path):
    return parse_tokens(path, META.name)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="company_dataset.sources.hf_datasets")
    p.add_argument("--raw", default="data/raw")
    a = p.parse_args(argv)
    print(json.dumps(harvest(Path(a.raw), HttpClient(timeout=60, per_host_interval=0.5, max_attempts=3)), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
