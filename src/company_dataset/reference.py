"""BigPicture free company dataset as a reference table keyed by website domain.

Used for two things only:
  * LinkedIn handle and country for a company whose website we already know
    (joined on domain, unambiguous matches only);
  * a candidate website for a board that has none, when the board's name matches
    exactly one company whose domain also agrees with the board slug. Such candidates
    are only accepted after the company's own homepage confirms the identity.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

import duckdb

from company_dataset.models import SourceMeta, utc_now
from company_dataset.normalize import clean_website, host_of

META = SourceMeta(
    name="bigpicture",
    description="BigPicture 2023 Q4 Free Company Dataset (17.1M companies: name, website domain, LinkedIn handle, country)",
    download_url="https://huggingface.co/datasets/bigpictureio/companies-2023-q4-sm/resolve/main/companies-2023-q4-sm.csv.gz",
    filename="companies-2023-q4-sm.csv.gz",
    license="ODC-By-1.0",
    license_url="https://opendatacommons.org/licenses/by/1-0/",
    license_note="Reference data only (LinkedIn handle, country, candidate website by domain/name join); never a source of boards.",
    attribution="BigPicture (bigpicture.io) Free Company Dataset 2023 Q4, via Hugging Face bigpictureio/companies-2023-q4-sm",
)

csv.field_size_limit(10**8)
INDEX_VERSION = 1  # bump when _scan changes what a row means


def name_key(name) -> str:
    from company_dataset.enrichment import name_key as _nk

    return _nk(name)


def _label_key(domain: str) -> str:
    return re.sub(r"[^a-z0-9]", "", domain.split(".")[0])


def domain_chain(host: str) -> list[str]:
    """acme.com -> [acme.com]; about.acme.com -> [about.acme.com, acme.com]."""
    labels = host.split(".")
    chain = [host]
    if len(labels) > 2:
        chain.append(".".join(labels[1:]))
    return chain


class Reference:
    def __init__(self, by_domain: dict | None = None, by_name: dict | None = None):
        self.by_domain: dict[str, list[dict]] = by_domain or {}
        self.by_name: dict[str, list[dict]] = by_name or {}

    def _rows_for(self, website: str, name: str | None) -> list[dict]:
        host = host_of(website)
        for dom in domain_chain(host) if host else []:
            rows = self.by_domain.get(dom)
            if not rows:
                continue
            if len(rows) == 1:
                return rows
            from company_dataset.enrichment import names_match

            named = [r for r in rows if name and names_match(r["name"], name)]
            return named if len(named) == 1 else []
        return []

    def linkedin_slug(self, website: str, name: str | None) -> str | None:
        rows = self._rows_for(website, name)
        if not rows or not rows[0].get("handle"):
            return None
        handle = rows[0]["handle"]
        return handle.split("company/", 1)[1] if handle.startswith("company/") else None

    def country(self, website: str, name: str | None) -> str | None:
        rows = self._rows_for(website, name)
        return (rows[0].get("country") or None) if rows else None

    def name(self, website: str) -> str | None:
        rows = self._rows_for(website, None)
        return rows[0]["name"] if rows else None

    def name_candidates(self, name: str | None, code: str) -> list[str]:
        """A website for a board known only by name: exactly one slug-agreeing domain."""
        if not name:
            return []
        key = name_key(name)
        rows = self.by_name.get(key) or []
        slug = re.sub(r"[^a-z0-9]", "", code.lower())
        agreeing = set()
        for r in rows:
            dom = host_of(r.get("website") or "")
            if not dom:
                continue
            label = _label_key(dom)
            # The domain must agree with the board slug, which is independent evidence;
            # agreeing with the name alone would let a generic name pick any namesake.
            if label and (label == slug or (len(label) >= 4 and slug.startswith(label))):
                site = clean_website("https://" + dom)
                if site:
                    agreeing.add(site)
        return sorted(agreeing) if len(agreeing) == 1 else []

    def to_json(self) -> dict:
        return {"by_domain": self.by_domain, "by_name": self.by_name}


def _scan(path: Path):
    """Every row with a website, parsed the way every lookup reads it."""
    with gzip.open(path, "rt", encoding="utf-8", errors="ignore", newline="") as fh:
        for row in csv.DictReader(fh):
            yield {
                "website": host_of(row.get("website") or ""),
                "key": name_key(row.get("name")),
                "name": (row.get("name") or "").strip(),
                "handle": (row.get("handle") or "").strip(),
                "country": (row.get("country_code") or "").strip().upper(),
            }


def _collect(rows, domains: set[str], names: set[str]) -> Reference:
    by_domain: dict[str, list[dict]] = defaultdict(list)
    by_name: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        dom, key = row["website"], row["key"]
        if not dom or not (dom in domains or key in names):
            continue
        rec = {"name": row["name"], "website": dom, "handle": row["handle"], "country": row["country"]}
        by_domain[dom].append(rec)
        if key in names:
            by_name[key].append(rec)
    return Reference(dict(by_domain), dict(by_name))


def _index_path(path: Path, cache_dir: Path) -> Path:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return Path(cache_dir) / f"bigpicture_v{INDEX_VERSION}_{Path(path).stat().st_size}_{digest.hexdigest()[:16]}.duckdb"


def _build_index(path: Path, index: Path) -> None:
    """Written beside its final name and moved into place, so an interrupted build is never read."""
    index.parent.mkdir(parents=True, exist_ok=True)
    work = index.with_suffix(".building")
    rows_csv = index.with_suffix(".rows.csv")
    work.unlink(missing_ok=True)
    try:
        with open(rows_csv, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            for seq, row in enumerate(_scan(path)):
                if row["website"]:
                    w.writerow([seq, row["website"], row["key"], row["name"], row["handle"], row["country"]])
        con = duckdb.connect(str(work))
        try:
            con.execute(
                "CREATE TABLE companies AS SELECT * FROM read_csv(?, header=false, delim=',', quote='\"', escape='\"', parallel=false, auto_detect=false,"
                " columns={'seq': 'BIGINT', 'website': 'VARCHAR', 'key': 'VARCHAR', 'name': 'VARCHAR', 'handle': 'VARCHAR', 'country': 'VARCHAR'})",
                [str(rows_csv)],
            )
        finally:
            con.close()
        work.replace(index)
    finally:
        rows_csv.unlink(missing_ok=True)
        work.unlink(missing_ok=True)


def _indexed_rows(index: Path, domains: set[str], names: set[str]):
    if not domains and not names:
        return
    con = duckdb.connect(str(index), read_only=True)
    try:
        con.execute("CREATE TEMP TABLE wanted_domains(v VARCHAR)")
        con.execute("CREATE TEMP TABLE wanted_names(v VARCHAR)")
        if domains:
            con.executemany("INSERT INTO wanted_domains VALUES (?)", [[d] for d in domains])
        if names:
            con.executemany("INSERT INTO wanted_names VALUES (?)", [[n] for n in names])
        cur = con.execute(
            "SELECT website, key, COALESCE(name, ''), COALESCE(handle, ''), COALESCE(country, '') FROM companies"
            " WHERE website IN (SELECT v FROM wanted_domains) OR key IN (SELECT v FROM wanted_names) ORDER BY seq")
        for website, key, name, handle, country in cur.fetchall():
            yield {"website": website, "key": key or "", "name": name, "handle": handle, "country": country}
    finally:
        con.close()


def build(path: Path, domains: set[str], names: set[str], cache_dir: Path | None = None) -> Reference:
    """The rows matching these domains or names. With cache_dir, the file is parsed once into an
    on-disk index keyed to its size and checksum; without it, every call scans the whole file."""
    if cache_dir is None:
        return _collect(_scan(path), domains, names)
    index = _index_path(path, cache_dir)
    if not index.exists():
        _build_index(path, index)
    return _collect(_indexed_rows(index, domains, names), domains, names)


def load_or_build(dirs: dict, boards, pages: dict) -> Reference:
    """Build the filtered reference index for the current boards and save it."""
    from company_dataset.enrichment import board_name_for_lookup, content_website_candidates, greenhouse_content_url, vendor_websites

    path = Path(dirs["raw"]) / META.name / META.filename
    if not path.exists():
        print(f"[reference] {path} missing; run download stage. Continuing without reference data.")
        return Reference()
    domains: set[str] = set()
    names: set[str] = set()
    for board, outcome in boards:
        vendor_page = pages.get(board.careersite_url)
        vendor_sites = vendor_websites(board, outcome, vendor_page)
        sites = list(vendor_sites)
        if board.ats == "greenhouse":
            sites += content_website_candidates(board, pages.get(greenhouse_content_url(board.code)))
        for site, _ in sites:
            for dom in domain_chain(host_of(site)):
                domains.add(dom)
            page = pages.get(site)
            if page and page.get("final_url"):
                for dom in domain_chain(host_of(page["final_url"])):
                    domains.add(dom)
        if not vendor_sites:
            key = name_key(board_name_for_lookup(board, outcome, vendor_page))
            if len(key) >= 3:
                names.add(key)
    print(f"[reference] scanning {path.name} for {len(domains)} domains and {len(names)} names")
    ref = build(path, domains, names)
    out = Path(dirs["intermediate"]) / "reference_index.json"
    out.write_text(json.dumps({"generated_at": utc_now(), **ref.to_json()}), encoding="utf-8")
    print(f"[reference] matched {len(ref.by_domain)} domains, {len(ref.by_name)} names")
    return ref


def load_saved(dirs: dict) -> Reference:
    path = Path(dirs["intermediate"]) / "reference_index.json"
    if not path.exists():
        return Reference()
    data = json.loads(path.read_text(encoding="utf-8"))
    return Reference(data.get("by_domain"), data.get("by_name"))
