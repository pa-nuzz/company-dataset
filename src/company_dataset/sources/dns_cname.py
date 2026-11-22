"""Boards behind a company's own hostname: careers.acme.com pointing at an ATS through DNS.

    python -m company_dataset.sources.dns_cname --raw data/raw --db data/store.db --limit-domains 5000

CNAME chains are followed to their end. A target such as acme.breezy.hr names the board itself; a
shared one such as jobs.lever.co does not, so the alias is fetched once and the board read from where
it lands or what it links to. Checkpoints after each batch, so a stopped run resumes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import dns.asyncresolver
import dns.exception
import dns.resolver

from company_dataset.enrichment import fetch_page
from company_dataset.http import HttpClient
from company_dataset.identity import registrable_domain
from company_dataset.models import SourceMeta
from company_dataset.normalize import CORE_ATS, host_of
from company_dataset.sources._harvest import board_from_url, parse_tokens, write_tokens

SUBDOMAINS = ("careers", "jobs", "join", "work", "apply")
ATS_DOMAINS = ("greenhouse.io", "lever.co", "ashbyhq.com", "smartrecruiters.com", "breezy.hr")
MAX_CHAIN = 6
CONCURRENCY = 50
BATCH_DOMAINS = 500
LOOKUP_TIMEOUT_SECONDS = 4.0

META = SourceMeta(
    name="dns_cname",
    description="Company careers hostnames whose DNS CNAME chain ends at a target ATS",
    download_url="https://www.iana.org/domains/root",
    filename="tokens.csv",
    license="DNS records (factual data)",
    license_url="",
    license_note="Only DNS targets and the board a hostname leads to are kept; every board is verified against its provider before use.",
    attribution="Public DNS",
    downloadable=False,
)


def ats_target(target: str) -> str | None:
    """The chain's ATS hostname, or None. Whole labels only: notlever.co is not lever.co."""
    host = target.rstrip(".").lower()
    return host if any(host == d or host.endswith("." + d) for d in ATS_DOMAINS) else None


async def cname_chain(resolver, host: str) -> list[str]:
    chain: list[str] = []
    current = host
    for _ in range(MAX_CHAIN):
        try:
            answer = await resolver.resolve(current, "CNAME")
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers, dns.exception.Timeout, dns.exception.DNSException):
            break
        current = str(answer[0].target).rstrip(".").lower()
        if current in chain:
            break
        chain.append(current)
    return chain


async def scan(hosts: list[str], resolver=None, concurrency: int = CONCURRENCY) -> dict[str, list[str]]:
    """Alias -> its CNAME chain, for aliases whose chain reaches a target ATS."""
    if resolver is None:
        resolver = dns.asyncresolver.Resolver()
        resolver.lifetime = LOOKUP_TIMEOUT_SECONDS
    gate = asyncio.Semaphore(concurrency)

    async def one(host):
        async with gate:
            return host, await cname_chain(resolver, host)

    return {h: c for h, c in await asyncio.gather(*(one(h) for h in hosts)) if any(ats_target(t) for t in c)}


def boards_for_alias(host: str, chain: list[str], client: HttpClient) -> dict[tuple[str, str], tuple[str, str, str]]:
    found: dict[tuple[str, str], tuple[str, str, str]] = {}
    evidence = f"cname:{host}->{chain[-1]}"
    for target in chain:
        board = board_from_url(f"https://{target}") if ats_target(target) else None
        if board:
            found.setdefault((board[0], board[1].casefold()), (board[1], board[2], evidence))
    if found:
        return found
    page = fetch_page(client, f"https://{host}")
    final = board_from_url(page.get("final_url") or "")
    if final:
        found.setdefault((final[0], final[1].casefold()), (final[1], final[2], evidence))
    for ref in page.get("board_refs") or []:
        ats, _, code = ref.partition(":")
        if ats in CORE_ATS and code:
            found.setdefault((ats, code.casefold()), (code, "", evidence))
    return found


def candidate_domains(conn, extra: list[str] | tuple = ()) -> list[str]:
    """Verified company websites first, then the extra list, one registrable domain each."""
    rows = conn.execute("SELECT DISTINCT value FROM field_evidence WHERE field='website_url' AND status='verified' ORDER BY value")
    seen = dict.fromkeys(registrable_domain(host_of(r[0]) or r[0]) for r in rows)
    seen.update(dict.fromkeys(registrable_domain(host_of(e) or e) for e in extra))
    return [d for d in seen if d and "." in d]


def harvest(raw_dir: Path, domains: list[str], client: HttpClient, resolver=None) -> dict:
    out = Path(raw_dir) / META.name
    out.mkdir(parents=True, exist_ok=True)
    log = out / "batches.jsonl"
    done, tokens, aliases = set(), {}, 0
    if log.exists():
        with open(log, encoding="utf-8") as fh:
            for line in fh:
                entry = json.loads(line)
                done.update(entry["domains"])
                aliases += entry["aliases"]
                for ats, code, region, evidence in entry["tokens"]:
                    tokens.setdefault((ats, code.casefold()), (code, region, evidence))
    todo = [d for d in domains if d not in done]
    with open(log, "a", encoding="utf-8") as fh:
        for start in range(0, len(todo), BATCH_DOMAINS):
            batch = todo[start:start + BATCH_DOMAINS]
            hits = asyncio.run(scan([f"{sub}.{d}" for d in batch for sub in SUBDOMAINS], resolver))
            found: dict = {}
            for host, chain in hits.items():
                found.update(boards_for_alias(host, chain, client))
            fh.write(json.dumps({"domains": batch, "aliases": len(hits), "tokens": [[k[0], v[0], v[1], v[2]] for k, v in found.items()]}) + "\n")
            fh.flush()
            aliases += len(hits)
            for key, value in found.items():
                tokens.setdefault(key, value)
            print(f"[dns] {start + len(batch)}/{len(todo)} domains: {aliases} aliases, {len(tokens)} boards", flush=True)
    write_tokens(out / META.filename, tokens)
    return {"domains_checked": len(done) + len(todo), "aliases_on_ats": aliases, "boards": len(tokens)}


def parse(path: Path):
    return parse_tokens(path, META.name)


def main(argv=None) -> int:
    from company_dataset import store

    p = argparse.ArgumentParser(prog="company_dataset.sources.dns_cname")
    p.add_argument("--raw", default="data/raw")
    p.add_argument("--db", required=True)
    p.add_argument("--limit-domains", type=int, required=True)
    a = p.parse_args(argv)
    conn = store.connect(a.db)
    domains = candidate_domains(conn)[:a.limit_domains]
    conn.close()
    print(json.dumps(harvest(Path(a.raw), domains, HttpClient(timeout=20, per_host_interval=0.5, max_attempts=2)), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
