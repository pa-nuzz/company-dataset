"""Board identifiers from public job datasets on Kaggle.

    python -m company_dataset.sources.kaggle_datasets --raw data/raw

Lists datasets by search and reads those under a permissive license, for URLs only. An archive is read
in memory, text files only: nothing is extracted to disk or run. Others are listed in report.json.
"""
from __future__ import annotations

import argparse
import io
import json
import zipfile
from pathlib import Path
from urllib.parse import urlencode

from company_dataset.http import HttpClient, RequestError
from company_dataset.models import SourceMeta
from company_dataset.sources._harvest import boards_in_text, parse_tokens, write_tokens

LIST = "https://www.kaggle.com/api/v1/datasets/list"
DOWNLOAD = "https://www.kaggle.com/api/v1/datasets/download/"
QUERIES = ["job postings", "job listings", "jobs ats", "greenhouse jobs", "lever jobs", "ashby jobs", "smartrecruiters", "remote jobs", "job board", "tech jobs", "linkedin jobs", "careers"]
PAGES_PER_QUERY = 5
PERMISSIVE = frozenset(("CC0: Public Domain", "ODC Attribution License (ODC-By)", "Attribution 4.0 International (CC BY 4.0)", "Apache 2.0", "MIT", "Community Data License Agreement - Permissive - Version 1.0",
                        "ODC Public Domain Dedication and Licence (PDDL)", "CC-BY-4.0", "CC-BY-3.0"))
TEXT_SUFFIXES = (".csv", ".tsv", ".json", ".jsonl", ".txt", ".md", ".ndjson")
MAX_DATASET_BYTES = 500_000_000
MAX_FILE_BYTES = 1_000_000_000
CHUNK_CHARS = 8_000_000

META = SourceMeta(
    name="kaggle_datasets",
    description="Permissively licensed Kaggle job datasets that contain ATS board URLs",
    download_url=LIST,
    filename="tokens.csv",
    license="Per dataset, permissive only; report.json lists each",
    license_url="https://www.kaggle.com/datasets",
    license_note="Only URLs (factual board identifiers) are read; every board is verified against its provider before use.",
    attribution="Dataset authors on Kaggle, listed in report.json",
    downloadable=False,
)


def candidates(client: HttpClient) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for query in QUERIES:
        for page in range(1, PAGES_PER_QUERY + 1):
            try:
                resp = client.get(f"{LIST}?{urlencode({'search': query, 'page': page, 'sortBy': 'hottest'})}")
            except RequestError:
                break
            if resp.status != 200:
                break
            items = resp.json()
            if not items:
                break
            for d in items:
                found.setdefault(d["ref"], {"license": d.get("licenseName") or d.get("licenseNameNullable") or "Unknown", "bytes": d.get("totalBytes") or d.get("totalBytesNullable") or 0})
    return found


def boards_in_archive(data: bytes, evidence: str) -> dict:
    """Text files only, read in chunks, from memory."""
    found: dict = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            if info.is_dir() or not info.filename.lower().endswith(TEXT_SUFFIXES) or info.file_size > MAX_FILE_BYTES:
                continue
            with archive.open(info) as raw:
                reader = io.TextIOWrapper(raw, encoding="utf-8", errors="ignore")
                carry = ""
                while True:
                    chunk = reader.read(CHUNK_CHARS)
                    text = carry + chunk
                    # Only scan up to the last whitespace: a URL cut by the chunk would read as a shorter, wrong slug.
                    cut = max(text.rfind(" "), text.rfind("\n")) + 1 if chunk else len(text)
                    for key, value in boards_in_text(text[:cut].replace("\\/", "/"), evidence).items():
                        found.setdefault(key, value)
                    carry = text[cut:]
                    if not chunk:
                        break
    return found


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
    found = candidates(client)
    skipped = sorted(f"{d} ({m['license']})" for d, m in found.items() if m["license"] not in PERMISSIVE)
    too_big = sorted(d for d, m in found.items() if m["license"] in PERMISSIVE and m["bytes"] > MAX_DATASET_BYTES)
    with open(log, "a", encoding="utf-8") as fh:
        for dataset, meta in sorted(found.items()):
            if dataset in done or meta["license"] not in PERMISSIVE or meta["bytes"] > MAX_DATASET_BYTES:
                continue
            evidence = f"https://www.kaggle.com/datasets/{dataset}"
            boards, status = {}, "read"
            try:
                resp = client.get(DOWNLOAD + dataset, binary=True)
                if resp.status == 200:
                    boards = boards_in_archive(resp.content, evidence)
                else:
                    status = f"http_{resp.status}"
            except (RequestError, zipfile.BadZipFile, OSError) as exc:
                status = f"failed: {type(exc).__name__}"
            entry = {"dataset": dataset, "license": meta["license"], "status": status, "tokens": [[k[0], v[0], v[1]] for k, v in boards.items()]}
            fh.write(json.dumps(entry) + "\n")
            fh.flush()
            done[dataset] = entry
            print(f"[kaggle] {dataset}: {status}, {len(boards)} boards", flush=True)
    for entry in done.values():
        for ats, code, region in entry["tokens"]:
            tokens.setdefault((ats, code.casefold()), (code, region, f"https://www.kaggle.com/datasets/{entry['dataset']}"))
    write_tokens(out / META.filename, tokens)
    (out / "report.json").write_text(json.dumps({"read": [{k: e[k] for k in ("dataset", "license", "status")} | {"boards": len(e["tokens"])} for e in done.values()], "skipped_for_license": skipped, "skipped_for_size": too_big}, indent=1))
    return {"datasets_read": len(done), "skipped_for_license": len(skipped), "skipped_for_size": len(too_big), "boards": len(tokens)}


def rebuild_tokens(raw_dir: Path) -> int:
    """tokens.csv and report.json from the datasets read so far, for a harvest stopped before the end."""
    out = Path(raw_dir) / META.name
    tokens: dict = {}
    read = []
    with open(out / "datasets.jsonl", encoding="utf-8") as fh:
        for line in fh:
            entry = json.loads(line)
            read.append({k: entry[k] for k in ("dataset", "license", "status")} | {"boards": len(entry["tokens"])})
            for ats, code, region in entry["tokens"]:
                tokens.setdefault((ats, code.casefold()), (code, region, f"https://www.kaggle.com/datasets/{entry['dataset']}"))
    (out / "report.json").write_text(json.dumps({"read": read, "stopped_early": True}, indent=1))
    return write_tokens(out / META.filename, tokens)


def parse(path: Path):
    return parse_tokens(path, META.name)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="company_dataset.sources.kaggle_datasets")
    p.add_argument("--raw", default="data/raw")
    p.add_argument("--rebuild", action="store_true", help="write tokens.csv from datasets read so far and stop")
    a = p.parse_args(argv)
    if a.rebuild:
        print(json.dumps({"boards": rebuild_tokens(Path(a.raw))}))
        return 0
    print(json.dumps(harvest(Path(a.raw), HttpClient(timeout=600, per_host_interval=1.0, max_attempts=2)), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
