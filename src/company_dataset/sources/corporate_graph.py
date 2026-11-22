"""Boards reached from companies already known: their links, their redirects, their subsidiaries and regional variants.

    python -m company_dataset.sources.corporate_graph links --raw data/raw --db data/store.db
    python -m company_dataset.sources.corporate_graph subsidiaries --raw data/raw --db data/store.db --limit-domains 2000
    python -m company_dataset.sources.corporate_graph variants --raw data/raw --db data/store.db --limit-per-vendor 5000

links reads only stored pages. subsidiaries asks Wikidata for parents and subsidiaries of verified websites,
then probes the identifiers their names suggest. variants tries regional and departmental suffixes on
confirmed boards. A probe that redirects to another board records that board too. Each mode adds to
hits.jsonl, and tokens.csv is rebuilt from it.
"""
from __future__ import annotations

import argparse
import json
import random
import threading
from pathlib import Path
from urllib.parse import quote

from company_dataset.http import HttpClient, RequestError
from company_dataset.models import SourceMeta
from company_dataset.normalize import ASHBY, BREEZY, CORE_ATS, GREENHOUSE, LEVER, SMARTRECRUITERS, host_of
from company_dataset.sources._harvest import board_from_url, parse_tokens, write_tokens
from company_dataset.sources.slug_prober import SECONDS_BETWEEN_REQUESTS, candidate_slugs, probe_vendor
from company_dataset.wikidata import ENDPOINT, _variants

SUFFIXES = ("us", "uk", "eu", "apac", "latam", "india", "canada", "tech", "engineering", "careers", "jobs", "global", "labs")
DELIMITERS = ("-", "_")
# Stored Greenhouse and SmartRecruiters identifiers are usually run together: hubspotjobs, DeloitteAT.
COMPACT_ATS = (GREENHOUSE, SMARTRECRUITERS)
VENDORS = (GREENHOUSE, LEVER, ASHBY, SMARTRECRUITERS, BREEZY)
WIKIDATA_BATCH = 10

META = SourceMeta(
    name="corporate_graph",
    description="Boards reached from known companies: links on their pages, redirects, Wikidata subsidiaries and regional variants",
    download_url="https://query.wikidata.org/",
    filename="tokens.csv",
    license="Factual identifiers; Wikidata is CC0",
    license_url="https://creativecommons.org/publicdomain/zero/1.0/",
    license_note="Only identifiers a page links to or a vendor answered for are kept; every board is verified against its provider before use.",
    attribution="Pages already fetched by this pipeline; Wikidata",
    downloadable=False,
)


def _hits_path(raw_dir: Path) -> Path:
    path = Path(raw_dir) / META.name / "hits.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def rebuild_tokens(raw_dir: Path) -> int:
    tokens = {}
    with open(_hits_path(raw_dir), encoding="utf-8") as fh:
        for line in fh:
            hit = json.loads(line)
            tokens.setdefault((hit["ats"], hit["code"].casefold()), (hit["code"], hit.get("region") or "", hit["evidence"]))
    return write_tokens(Path(raw_dir) / META.name / META.filename, tokens)


def stored_page_boards(conn, known: set[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    """Boards stored pages link to or redirected to, and that the store does not know yet."""
    found: dict[tuple[str, str], dict] = {}
    for r in conn.execute("SELECT url, final_url, summary FROM pages"):
        final = board_from_url(r["final_url"] or "")
        if final and (final[0], final[1].casefold()) not in known:
            found.setdefault((final[0], final[1].casefold()), {"ats": final[0], "code": final[1], "region": final[2], "evidence": f"redirect:{r['url']}"})
        for ref in json.loads(r["summary"]).get("board_refs") or []:
            ats, _, code = ref.partition(":")
            board = board_from_url(_board_probe_url(ats, code)) if ats in CORE_ATS and code else None
            if board and (board[0], board[1].casefold()) not in known:
                found.setdefault((board[0], board[1].casefold()), {"ats": board[0], "code": board[1], "region": board[2], "evidence": f"link:{r['url']}"})
    return found


def _board_probe_url(ats: str, code: str) -> str:
    q = quote(code, safe="")
    return {GREENHOUSE: f"https://job-boards.greenhouse.io/{q}", LEVER: f"https://jobs.lever.co/{q}", ASHBY: f"https://jobs.ashbyhq.com/{q}",
            SMARTRECRUITERS: f"https://careers.smartrecruiters.com/{q}", BREEZY: f"https://{q}.breezy.hr"}[ats]


def variant_slugs(ats: str, code: str) -> list[str]:
    delimiters = tuple(DELIMITERS) + (("",) if ats in COMPACT_ATS else ())
    out = []
    for suffix in SUFFIXES:
        for delimiter in delimiters:
            if ats == BREEZY and delimiter == "_":
                continue  # a Breezy identifier is a hostname label
            out.append(f"{code}{delimiter}{suffix}")
    return out


def plan_variants(conn, known: set[tuple[str, str]], limit: int, seed: int) -> dict[str, list[str]]:
    rows = conn.execute("SELECT DISTINCT b.ats, b.code FROM identities i JOIN boards b ON b.id=i.board_id WHERE i.status='identity_confirmed' ORDER BY b.ats, b.code_key").fetchall()
    rng = random.Random(seed)
    plan = {}
    for ats in VENDORS:
        codes = [r["code"] for r in rows if r["ats"] == ats]
        rng.shuffle(codes)
        chosen: dict[str, None] = {}
        for code in codes:
            for slug in variant_slugs(ats, code):
                if (ats, slug.casefold()) not in known:
                    chosen.setdefault(slug)
            if len(chosen) >= limit:
                break
        plan[ats] = list(chosen)[:limit]
    return plan


def subsidiary_query(hosts: list[str]) -> str:
    values = " ".join(f"<{u}>" for h in hosts for u in _variants(h))
    return ("SELECT DISTINCT ?relatedLabel ?site WHERE { VALUES ?u { " + values + " } ?item wdt:P856 ?u . "
            "{ ?item wdt:P355 ?related } UNION { ?related wdt:P749 ?item } UNION { ?item wdt:P749 ?related } UNION { ?related wdt:P355 ?item } "
            "?related wdt:P856 ?site . SERVICE wikibase:label { bd:serviceParam wikibase:language \"en\". } }")


def related_companies(client: HttpClient, hosts: list[str]) -> list[tuple[str, str]]:
    """(name, website) of parents and subsidiaries. A failed batch is skipped, not fatal."""
    out: dict[tuple[str, str], None] = {}
    for i in range(0, len(hosts), WIKIDATA_BATCH):
        chunk = [h.removeprefix("www.") for h in hosts[i:i + WIKIDATA_BATCH]]
        try:
            resp = client.get(ENDPOINT + quote(subsidiary_query(chunk)), headers={"Accept": "application/sparql-results+json"})
            if resp.status != 200:
                continue
            for b in json.loads(resp.text)["results"]["bindings"]:
                out.setdefault((b["relatedLabel"]["value"], b["site"]["value"]))
        except (RequestError, ValueError, KeyError):
            continue
    return list(out)


def run_probes(raw_dir: Path, plan: dict[str, list[str]], evidence: str, client_factory=None) -> dict:
    lock = threading.Lock()
    report: dict[str, dict] = {}
    log = open(_hits_path(raw_dir), "a", encoding="utf-8")

    def on_hit(ats, slug, redirected=None):
        with lock:
            log.write(json.dumps({"ats": ats, "code": slug, "region": "", "evidence": evidence}) + "\n")
            if redirected:
                log.write(json.dumps({"ats": redirected[0], "code": redirected[1], "region": redirected[2], "evidence": f"redirect:{ats}:{slug}"}) + "\n")
            log.flush()

    def work(ats):
        client = (client_factory or (lambda: HttpClient(timeout=20, max_attempts=1, per_host_interval=SECONDS_BETWEEN_REQUESTS)))()
        report[ats] = probe_vendor(client, ats, plan[ats], on_hit=on_hit)
        print(f"[corporate] {ats}: {report[ats]}", flush=True)

    threads = [threading.Thread(target=work, args=(ats,)) for ats in plan if plan[ats]]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log.close()
    return report


def parse(path: Path):
    return parse_tokens(path, META.name)


def main(argv=None) -> int:
    from company_dataset import store

    p = argparse.ArgumentParser(prog="company_dataset.sources.corporate_graph")
    p.add_argument("mode", choices=["links", "subsidiaries", "variants"])
    p.add_argument("--raw", default="data/raw")
    p.add_argument("--db", required=True)
    p.add_argument("--limit-per-vendor", type=int, default=5000)
    p.add_argument("--limit-domains", type=int, default=2000)
    p.add_argument("--seed", type=int, default=20261103)
    a = p.parse_args(argv)
    raw = Path(a.raw)
    conn = store.connect(a.db)
    known = {(r["ats"], r["code_key"]) for r in conn.execute("SELECT ats, code_key FROM boards")}
    if a.mode == "links":
        found = stored_page_boards(conn, known)
        with open(_hits_path(raw), "a", encoding="utf-8") as fh:
            for hit in found.values():
                fh.write(json.dumps(hit) + "\n")
        out = {"new_boards": len(found)}
    elif a.mode == "variants":
        plan = plan_variants(conn, known, a.limit_per_vendor, a.seed)
        conn.close()
        out = run_probes(raw, plan, "regional_or_department_variant")
    else:
        hosts = sorted({host_of(r[0]) or r[0] for r in conn.execute("SELECT DISTINCT value FROM field_evidence WHERE field='website_url' AND status='verified'")})
        random.Random(a.seed).shuffle(hosts)
        conn_known = known
        conn.close()
        related = related_companies(HttpClient(timeout=60, per_host_interval=1.0, max_attempts=3), hosts[:a.limit_domains])
        slugs = sorted({s for name, site in related for s in candidate_slugs(name, site)})
        plan = {ats: [s for s in slugs if (ats, s.casefold()) not in conn_known][:a.limit_per_vendor] for ats in VENDORS}
        out = {"related_companies": len(related), "probes": run_probes(raw, plan, "wikidata_parent_or_subsidiary")}
    print(json.dumps({**out, "tokens": rebuild_tokens(raw)} if isinstance(out, dict) else out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
