"""Board identifiers from Certificate Transparency logs, via crt.sh.

    python -m company_dataset.sources.crtsh --raw data/raw

Every HTTPS hostname a vendor issues a certificate for is logged publicly. Custom career
subdomains under the vendors' own domains appear there: acme.breezy.hr has a cert. Only
hostname-shaped facts are kept; vendor infrastructure names are dropped by the same rules
passive DNS uses, and every board is verified against its provider before use.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from company_dataset.http import HttpClient, RequestError
from company_dataset.models import SourceMeta
from company_dataset.sources._harvest import parse_tokens, write_tokens
from company_dataset.sources.passive_dns import SUBDOMAIN_ATS, board_from_host

DOMAINS = tuple(SUBDOMAIN_ATS)
META = SourceMeta(
    name="crtsh_2026_11_03",
    description="Board subdomains under ATS domains observed in public Certificate Transparency logs (crt.sh)",
    download_url="https://crt.sh",
    filename="tokens.csv",
    license="Public certificate transparency records (factual data)",
    license_url="",
    license_note="Only hostnames are kept; every board is verified against its provider before use.",
    attribution="crt.sh, over the public CT logs",
    downloadable=False,
)


def query_url(domain: str) -> str:
    return f"https://crt.sh/?q=%.{domain}&output=json"


def names_in(entry: dict) -> set[str]:
    names = set(entry.get("common_name", "").split())
    for value in entry.get("name_value", "").split("\n"):
        names.update(value.split())
    return {name for name in (n.lower().strip() for n in names) if name}


def harvest(raw_dir: Path, client: HttpClient, domains=DOMAINS, sleep=None) -> dict:
    out = Path(raw_dir) / META.name
    tokens: dict[tuple[str, str], tuple[str, str, str]] = {}
    per_domain: dict[str, str] = {}
    for domain in domains:
        try:
            resp = client.get(query_url(domain))
        except RequestError:
            per_domain[domain] = "error:unreachable"
            continue
        if resp.status != 200 or not resp.text.strip():
            per_domain[domain] = f"error:status_{resp.status}"
            continue
        try:
            entries = json.loads(resp.text)
        except ValueError:
            per_domain[domain] = "error:not_json"
            continue
        if not isinstance(entries, list):
            per_domain[domain] = "error:not_json"
            continue
        found = 0
        for entry in entries:
            for name in names_in(entry):
                board = board_from_host(name)
                if not board:
                    continue
                key = (board[0], board[1].casefold())
                if key not in tokens:
                    found += 1
                    tokens[key] = (board[1], "", f"crtsh:{name}")
        per_domain[domain] = f"ok:entries={len(entries)} boards={found}"
    write_tokens(out / META.filename, tokens)
    return {"domains": per_domain, "boards": len(tokens)}


def parse(path: Path):
    return parse_tokens(path, META.name)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="company_dataset.sources.crtsh")
    p.add_argument("--raw", default="data/raw")
    p.add_argument("--timeout", type=float, default=90)
    a = p.parse_args(argv)
    client = HttpClient(timeout=a.timeout, max_attempts=2, per_host_interval=2.0)
    print(json.dumps(harvest(Path(a.raw), client), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
