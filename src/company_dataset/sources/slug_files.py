"""Board identifiers from files on GitHub that list ATS slugs without URLs.

    python -m company_dataset.sources.slug_files --raw data/raw

Job tools keep lists like {"greenhouse": ["stripe", ...]}, company_slugs.json or companies_ashby.txt,
which a URL scan cannot see. A word counts as a slug only when the file ties it to an ATS: a key naming
the ATS, a record with an ATS field, or the file's own name. An unrelated slugs.json yields nothing.
Files are only parsed as data. A finished file is saved at once, so a stopped harvest resumes.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path

from company_dataset.http import HttpClient, RequestError
from company_dataset.models import SourceMeta
from company_dataset.normalize import ASHBY, BREEZY, CORE_ATS, GREENHOUSE, LEVER, SMARTRECRUITERS, clean_code
from company_dataset.platforms import WORKABLE
from company_dataset.sources._harvest import boards_in_text, parse_tokens, plausible_slug, write_tokens
from company_dataset.sources.github_lists import MAX_FILE_BYTES, PER_PAGE, SECONDS_BETWEEN_SEARCHES, _gh, raw_url

QUERIES = [
    "filename:slugs.json greenhouse", "filename:slugs.json lever", "filename:slugs.json ashby", "filename:seed_slugs.json", "filename:company_slugs.json", "filename:known_slugs",
    "filename:vetted_slugs", "filename:harvested_slugs", "filename:companies_greenhouse", "filename:companies_ashby", "filename:companies_lever", "filename:greenhouse_slugs",
    "filename:ashby_slugs", "filename:lever_slugs", "filename:smartrecruiters_slugs", "filename:breezy_slugs", "filename:greenhouse_boards", "filename:lever_boards", "filename:ashby_boards",
    "filename:board_tokens", "filename:ats_companies", "filename:boards.json greenhouse ashby", "filename:companies.yaml greenhouse lever ashby", "filename:companies.yml greenhouse lever",
    "filename:companies.json greenhouse lever ashby", "filename:companies.csv greenhouse lever", "filename:targets.json greenhouse lever", "filename:sources.json greenhouse lever ashby",
    "greenhouse slugs filetype:json", "lever slugs filetype:json", "ashby slugs filetype:json", "breezy slugs filetype:json",
    "company_slugs filetype:yaml", "company_slugs filetype:csv", "ats_companies filetype:json",
]
PAGES_PER_QUERY = 5
_ATS_WORDS = {"greenhouse": GREENHOUSE, "gh": GREENHOUSE, "lever": LEVER, "ashby": ASHBY, "ashbyhq": ASHBY, "smartrecruiters": SMARTRECRUITERS, "smart_recruiters": SMARTRECRUITERS, "breezy": BREEZY, "breezyhr": BREEZY, "breezy_hr": BREEZY, "workable": WORKABLE}
VENDOR_WORDS = frozenset(("www", "apply", "resources", "help", "support", "jobs", "careers", "blog", "app", "api", "status", "docs", "mail", "marketing", "go", "info", "assets", "cdn", "static"))
_ATS_FIELDS = ("ats", "platform", "provider", "source", "ats_type", "atstype", "type", "vendor", "system", "ats_system")
_SLUG_FIELDS = ("slug", "token", "board", "board_token", "boardtoken", "board_slug", "code", "ats_code", "identifier", "handle", "company_slug", "site", "id", "company")
# Words a list of this kind often holds that are never a board.
_NOT_SLUGS = frozenset(("true", "false", "null", "none", "greenhouse", "lever", "ashby", "smartrecruiters", "breezy", "slug", "token", "name", "company", "url", "jobs", "careers", "boards"))

META = SourceMeta(
    name="slug_files",
    description="Files on public GitHub repositories listing ATS board slugs without URLs",
    download_url="https://github.com/search",
    filename="tokens.csv",
    license="Public repository data; licenses vary by repository",
    license_url="https://docs.github.com/en/site-policy/github-terms/github-terms-of-service",
    license_note="Only board identifiers are read; report.json names every file used. Every board is verified against its provider before use.",
    attribution="Public GitHub repositories, listed in report.json",
    downloadable=False,
)


def ats_in_name(text: str) -> str | None:
    """The one ATS a name mentions, whole word; with none or several, nothing can be said."""
    words = re.findall(r"[a-z]+", re.sub(r"[-_.]", " ", (text or "").lower()))
    found = {_ATS_WORDS[w] for w in words if w in _ATS_WORDS and w != "gh"}
    return found.pop() if len(found) == 1 else None


def _usable(ats: str, value) -> str | None:
    if not isinstance(value, str) or (ats not in CORE_ATS and ats != WORKABLE):
        return None
    candidate = value.strip()
    if " " in candidate or "/" in candidate or candidate.lower() in _NOT_SLUGS:
        return None
    code = clean_code(ats, candidate)
    return code if plausible_slug(code) else None


def _walk(node, ats: str | None, out: set[tuple[str, str]]) -> None:
    if isinstance(node, dict):
        lowered = {str(k).lower(): v for k, v in node.items()}
        record_ats = next((_ATS_WORDS[str(lowered[f]).strip().lower().replace(" ", "")] for f in _ATS_FIELDS if isinstance(lowered.get(f), str) and str(lowered[f]).strip().lower().replace(" ", "") in _ATS_WORDS), None)
        own = record_ats or ats
        if own:
            for field in _SLUG_FIELDS:
                code = _usable(own, lowered.get(field))
                if code:
                    out.add((own, code))
                    break
        for key, value in node.items():
            _walk(value, ats_in_name(str(key)) or record_ats or ats, out)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, str):
                code = _usable(ats, item) if ats else None
                if code:
                    out.add((ats, code))
            else:
                _walk(item, ats, out)


def slugs_in_file(path: str, text: str) -> set[tuple[str, str]]:
    """(ats, code) pairs a file ties to an ATS."""
    file_ats = ats_in_name(Path(path).name)
    out: set[tuple[str, str]] = set()
    suffix = Path(path).suffix.lower()
    if suffix == ".json":
        try:
            _walk(json.loads(text), file_ats, out)
        except ValueError:
            pass
    elif suffix in (".txt", ".lst", ""):
        if file_ats:
            for line in text.splitlines():
                code = _usable(file_ats, line.split("#", 1)[0])
                if code:
                    out.add((file_ats, code))
    elif suffix in (".yaml", ".yml"):
        current = file_ats
        for line in text.splitlines():
            key = re.match(r"^\s*([A-Za-z_]+)\s*:\s*(\S*)\s*$", line)
            if key and key.group(1).lower() in _ATS_WORDS and not key.group(2):
                current = _ATS_WORDS[key.group(1).lower()]
            elif key and key.group(1).lower() in _ATS_FIELDS and key.group(2).strip("'\"").lower() in _ATS_WORDS:
                current = _ATS_WORDS[key.group(2).strip("'\"").lower()]
            item = re.match(r"^\s*-\s*['\"]?([A-Za-z0-9._-]+)['\"]?\s*$", line)
            slug = key if key and key.group(1).lower() in _SLUG_FIELDS else None
            value = item.group(1) if item else (slug.group(2).strip("'\"") if slug else None)
            code = _usable(current, value) if current and value else None
            if code:
                out.add((current, code))
    return out


def discover(search=_gh, sleep=time.sleep, queries=QUERIES, pages: int = PAGES_PER_QUERY) -> list[str]:
    found: dict[str, None] = {}
    for query in queries:
        for page in range(1, pages + 1):
            try:
                result = search("search/code", q=query, per_page=PER_PAGE, page=page)
            except (subprocess.SubprocessError, ValueError):
                print(f"[slug-files] search failed: {query}", flush=True)
                break
            sleep(SECONDS_BETWEEN_SEARCHES)
            items = result.get("items") or []
            for item in items:
                url = raw_url(item.get("html_url") or "")
                if url and (item.get("file_size") or 0) <= MAX_FILE_BYTES:
                    found.setdefault(url)
            if len(items) < PER_PAGE:
                break
        print(f"[slug-files] {query}: {len(found)} files", flush=True)
    return list(found)


def harvest(raw_dir: Path, client: HttpClient, search=_gh, sleep=time.sleep) -> dict:
    out = Path(raw_dir) / META.name
    out.mkdir(parents=True, exist_ok=True)
    log = out / "files.jsonl"
    done: dict[str, dict] = {}
    if log.exists():
        with open(log, encoding="utf-8") as fh:
            for line in fh:
                entry = json.loads(line)
                done[entry["url"]] = entry
    with open(log, "a", encoding="utf-8") as fh:
        for url in discover(search, sleep):
            if url in done:
                continue
            entry = {"url": url, "tokens": []}
            try:
                resp = client.get(url)
            except RequestError:
                continue
            if resp.status == 200 and len(resp.text) <= MAX_FILE_BYTES:
                pairs = slugs_in_file(url, resp.text) | {(k[0], v[0]) for k, v in boards_in_text(resp.text, url).items()}
                entry["tokens"] = [[a, c] for a, c in sorted(pairs)]
            fh.write(json.dumps(entry) + "\n")
            fh.flush()
            done[url] = entry
    tokens: dict = {}
    for url in sorted(done):
        for ats, code in done[url]["tokens"]:
            tokens.setdefault((ats, code.casefold()), (code, "", url))
    write_tokens(out / META.filename, tokens)
    useful = sorted(({"url": e["url"], "boards": len(e["tokens"])} for e in done.values() if e["tokens"]), key=lambda e: -e["boards"])
    (out / "report.json").write_text(json.dumps({"files_read": len(done), "files": useful}, indent=1))
    return {"files_read": len(done), "files_with_boards": len(useful), "boards": len(tokens)}


def parse(path: Path):
    return parse_tokens(path, META.name)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="company_dataset.sources.slug_files")
    p.add_argument("--raw", default="data/raw")
    a = p.parse_args(argv)
    print(json.dumps(harvest(Path(a.raw), HttpClient(timeout=60, per_host_interval=0.1, max_attempts=2)), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
