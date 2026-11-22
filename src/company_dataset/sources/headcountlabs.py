"""Headcount Labs job postings (CC BY 4.0): board slugs from posting URLs.

The dataset is ~3 GB of parquet. Download fetches the postings files listed in the
dataset's MANIFEST, then extracts the distinct Greenhouse / Lever / Ashby boards from
`posting_url` / `application_url` into a small CSV that `parse` reads.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from company_dataset.models import Reject, SourceMeta, SourceRecord
from company_dataset.normalize import SUPPORTED_ATS, clean_code, parse_board_url

REPO = "https://huggingface.co/datasets/HeadcountLabs/job-postings/resolve/main/"

META = SourceMeta(
    name="headcountlabs",
    description="Headcount Labs Job Postings snapshot 2026-10-14 (5.7M postings read from employers' ATS boards); boards taken from posting URLs",
    download_url="https://huggingface.co/datasets/HeadcountLabs/job-postings",
    filename="boards.csv",
    license="CC-BY-4.0",
    license_url="https://creativecommons.org/licenses/by/4.0/",
    license_note="Only board identifiers parsed from posting URLs and the employer name are used.",
    attribution="Headcount Labs (headcountlabs.com), Job Postings dataset on Hugging Face, snapshot 2026-10-14",
)


def run_download(dest_dir: Path, client) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    parquet_dir = dest_dir / "postings"
    parquet_dir.mkdir(exist_ok=True)
    resp = client.get(REPO + "MANIFEST.json")
    if resp.status != 200:
        raise RuntimeError(f"headcountlabs MANIFEST: HTTP {resp.status}")
    (dest_dir / "MANIFEST.json").write_text(resp.text, encoding="utf-8")
    files = [f["path"] for f in json.loads(resp.text).get("files", []) if f.get("path", "").startswith("data/postings/")]
    if not files:
        raise RuntimeError("headcountlabs MANIFEST lists no postings files")
    from company_dataset.http import stream_to

    for path in files:
        target = parquet_dir / Path(path).name
        if target.exists() and target.stat().st_size > 0:
            continue
        status = stream_to(client, REPO + path, target)
        if status != 200:
            raise RuntimeError(f"headcountlabs {path}: HTTP {status}")
    extract_boards(parquet_dir, dest_dir / META.filename, client=client)


def resolve_company_domain_boards(con, parquet_dir: Path, client) -> dict[tuple[str, str], str]:
    """Greenhouse boards known only by postings on the company's own site (…?gh_jid=…).

    Those career pages embed the board (boards.greenhouse.io/embed/job_app?for=<slug>),
    so one page per board reveals the slug.
    """
    from concurrent.futures import ThreadPoolExecutor

    from company_dataset.enrichment import board_refs, parse_page

    rows = con.execute(
        f"""SELECT board_key, posting_url, application_url, json_extract_string(employer_name_json, '$.value')
            FROM read_parquet('{parquet_dir}/*.parquet') WHERE source_type = 'greenhouse'"""
    ).fetchall()
    with_slug: set[str] = set()
    sample: dict[str, tuple[str, str]] = {}
    for board_key, posting_url, application_url, employer in rows:
        if any(parse_board_url(u) for u in (posting_url, application_url) if u):
            with_slug.add(board_key)
        elif posting_url and board_key not in sample:
            sample[board_key] = (posting_url, (employer or "").strip())
    todo = [v for k, v in sample.items() if k not in with_slug]

    def resolve(item):
        url, employer = item
        try:
            resp = client.get(url)
        except Exception:
            return []
        if resp.status != 200:
            return []
        return [(ref.split(":", 1)[1], employer) for ref in board_refs(parse_page(resp.text)) if ref.startswith("greenhouse:")]

    found: dict[tuple[str, str], str] = {}
    with ThreadPoolExecutor(max_workers=16) as executor:
        for refs in executor.map(resolve, todo):
            for code, employer in refs:
                code = clean_code("greenhouse", code)
                if code:
                    found.setdefault(("greenhouse", code), employer)
    print(f"[headcountlabs] resolved {len(found)} boards from {len(todo)} company-site postings")
    return found


def extract_boards(parquet_dir: Path, out: Path, client=None) -> int:
    import duckdb

    con = duckdb.connect()
    con.execute("SET enable_progress_bar=false")
    rows = con.execute(
        f"""SELECT posting_url, application_url, json_extract_string(employer_name_json, '$.value') AS employer
            FROM read_parquet('{parquet_dir}/*.parquet')
            WHERE source_type IN ('greenhouse', 'lever', 'ashby')
               OR regexp_matches(coalesce(posting_url, ''), 'greenhouse\\.io|lever\\.co|ashbyhq\\.com')"""
    ).fetchall()
    boards: dict[tuple[str, str], str] = {}
    for posting_url, application_url, employer in rows:
        for url in (posting_url, application_url):
            parsed = parse_board_url(url) if url else None
            if parsed:
                boards.setdefault((parsed.ats, parsed.code), (employer or "").strip())
    if client is not None:
        boards.update({k: v for k, v in resolve_company_domain_boards(con, parquet_dir, client).items() if k not in boards})
    tmp = out.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ats", "code", "employer_name"])
        for (ats, code), employer in sorted(boards.items()):
            writer.writerow([ats, code, employer])
    tmp.replace(out)
    print(f"[headcountlabs] extracted {len(boards)} boards")
    return len(boards)


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
            records.append(SourceRecord(source=META.name, ats=ats, code=code, name=(row.get("employer_name") or "").strip() or None, row_ref=ref))
    return records, rejects
