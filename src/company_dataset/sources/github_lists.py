"""Board identifiers from public GitHub files: curated job lists, and files found by searching GitHub.

    python -m company_dataset.sources.github_lists --raw data/raw

Starts from known lists, then asks GitHub's code search which other files mention a board host and
its repository search for job-list repositories. Files are only scanned for URLs. Search goes through
the logged-in gh CLI, since code search needs an account; a finished file is saved at once, so a
stopped harvest resumes. report.json records what every file contributed.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from company_dataset.http import HttpClient, RequestError
from company_dataset.models import SourceMeta
from company_dataset.sources._harvest import boards_in_text, parse_tokens, write_tokens

SEEDS = [
    "https://raw.githubusercontent.com/jyb/awesome-tech-jobs/main/README.md",
    "https://raw.githubusercontent.com/poteto/hiring-without-whiteboards/master/README.md",
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/README.md",
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md",
    "https://raw.githubusercontent.com/remoteintech/remote-jobs/main/README.md",
    "https://raw.githubusercontent.com/dreamworkhq/Remote-Tech-Jobs/main/README.md",
    # The structured data behind the two SimplifyJobs READMEs lists many more boards than they show.
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json",
]
BOARD_HOSTS = ["boards.greenhouse.io", "job-boards.greenhouse.io", "jobs.lever.co", "jobs.ashbyhq.com", "jobs.smartrecruiters.com", "careers.smartrecruiters.com", "breezy.hr"]
# Code search returns at most 1,000 results a query, so each host is split by file type.
EXTENSIONS = ["md", "csv", "json", "yaml", "yml"]
REPOSITORY_QUERIES = [
    "ats job boards companies list",
    "greenhouse lever ashby companies",
    "greenhouse board list",
    "lever board list",
    "ashby board list",
]
SEARCH_PAGES = 5
PER_PAGE = 100
SECONDS_BETWEEN_SEARCHES = 2.5
MAX_FILE_BYTES = 30_000_000

META = SourceMeta(
    name="github_lists",
    description="Public GitHub job lists and files mentioning ATS board URLs, found from seed lists and GitHub search",
    download_url="https://github.com/search",
    filename="tokens.csv",
    license="Public repository data; licenses vary by repository",
    license_url="https://docs.github.com/en/site-policy/github-terms/github-terms-of-service",
    license_note="Only URLs (factual board identifiers) are read from each file; report.json names every file used. Every board is verified against its provider before use.",
    attribution="Public GitHub repositories, listed in report.json",
    downloadable=False,
)


def _gh(path: str, **params) -> dict:
    args = ["gh", "api", "-X", "GET", path] + [a for k, v in params.items() for a in ("-f", f"{k}={v}")]
    return json.loads(subprocess.run(args, capture_output=True, text=True, check=True, timeout=120).stdout)


def raw_url(html_url: str) -> str | None:
    """https://github.com/o/r/blob/ref/p.md -> https://raw.githubusercontent.com/o/r/ref/p.md"""
    parts = urlsplit(html_url)
    pieces = parts.path.strip("/").split("/", 3)
    if parts.hostname != "github.com" or len(pieces) < 4 or pieces[2] != "blob":
        return None
    return f"https://raw.githubusercontent.com/{pieces[0]}/{pieces[1]}/{unquote(pieces[3])}"


def discover(search=_gh, sleep=time.sleep, hosts=BOARD_HOSTS, extensions=EXTENSIONS, pages: int = SEARCH_PAGES) -> dict[str, str]:
    """raw file URL -> how it was found. A search that fails is skipped: seed lists still count."""
    found: dict[str, str] = {}
    for query in REPOSITORY_QUERIES:
        try:
            result = search("search/repositories", q=query, per_page=PER_PAGE, sort="stars")
        except (subprocess.SubprocessError, ValueError) as exc:
            print(f"[github] repository search failed: {type(exc).__name__}", flush=True)
            continue
        for item in result.get("items") or []:
            found.setdefault(f"https://raw.githubusercontent.com/{item['full_name']}/HEAD/README.md", f"repositories:{query}")
        sleep(SECONDS_BETWEEN_SEARCHES)
    for host in hosts:
        for ext in extensions:
            query = f'"{host}" extension:{ext}'
            for page in range(1, pages + 1):
                try:
                    result = search("search/code", q=query, per_page=PER_PAGE, page=page)
                except (subprocess.SubprocessError, ValueError) as exc:
                    print(f"[github] code search failed for {query} page {page}: {type(exc).__name__}", flush=True)
                    break
                sleep(SECONDS_BETWEEN_SEARCHES)
                items = result.get("items") or []
                for item in items:
                    url = raw_url(item.get("html_url") or "")
                    if url and (item.get("file_size") or 0) <= MAX_FILE_BYTES:
                        found.setdefault(url, f"code:{query}")
                if len(items) < PER_PAGE:
                    break
            print(f"[github] {query}: {len(found)} files so far", flush=True)
    return found


def harvest(raw_dir: Path, client: HttpClient, search=_gh, sleep=time.sleep, seeds=SEEDS, use_search: bool = True) -> dict:
    out = Path(raw_dir) / META.name
    out.mkdir(parents=True, exist_ok=True)
    log = out / "files.jsonl"
    done: dict[str, dict] = {}
    if log.exists():
        with open(log, encoding="utf-8") as fh:
            for line in fh:
                entry = json.loads(line)
                done[entry["url"]] = entry
    files = {url: "seed" for url in seeds}
    if use_search:
        for url, how in discover(search, sleep).items():
            files.setdefault(url, how)
    with open(log, "a", encoding="utf-8") as fh:
        for url, how in files.items():
            if url in done:
                continue
            entry = {"url": url, "found_by": how, "status": None, "tokens": []}
            try:
                resp = client.get(url)
                entry["status"] = resp.status
                if resp.status == 200 and len(resp.text) <= MAX_FILE_BYTES:
                    entry["tokens"] = [[ats, code, region] for (ats, _), (code, region, _) in boards_in_text(resp.text, url).items()]
            except RequestError:
                entry["status"] = "error"
            fh.write(json.dumps(entry) + "\n")
            fh.flush()
            done[url] = entry
    tokens: dict[tuple[str, str], tuple[str, str, str]] = {}
    for url in sorted(done):
        for ats, code, region in done[url]["tokens"]:
            tokens.setdefault((ats, code.casefold()), (code, region, url))
    write_tokens(out / META.filename, tokens)
    useful = sorted(({"url": e["url"], "found_by": e["found_by"], "boards": len(e["tokens"])} for e in done.values() if e["tokens"]), key=lambda e: (-e["boards"], e["url"]))
    failed = sorted(e["url"] for e in done.values() if e["status"] != 200)
    report = {"files_read": len(done), "files_with_boards": len(useful), "boards": len(tokens), "seeds_unavailable": sorted(set(seeds) & set(failed)), "files": useful}
    (out / "report.json").write_text(json.dumps(report, indent=1))
    return {k: v for k, v in report.items() if k != "files"}


def parse(path: Path):
    return parse_tokens(path, META.name)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="company_dataset.sources.github_lists")
    p.add_argument("--raw", default="data/raw")
    p.add_argument("--seeds-only", action="store_true")
    a = p.parse_args(argv)
    client = HttpClient(timeout=60, per_host_interval=0.2, max_attempts=2)
    print(json.dumps(harvest(Path(a.raw), client, use_search=not a.seeds_only), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
