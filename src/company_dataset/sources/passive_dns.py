"""Board identifiers from passive DNS and certificate data, gathered with subfinder and Amass.

    subfinder -d breezy.hr -all -silent -o data/raw/passive_dns_2026_11_03/subfinder.txt
    amass enum -d breezy.hr -dir ...; amass subs -names -d breezy.hr -dir ... -o .../amass.txt
    python -m company_dataset.sources.passive_dns --raw data/raw

The tools read public third-party records and send nothing to the ATS. Only hosts that are one label
under an ATS domain can be boards: acme.breezy.hr, not mail1.assets-cdn.breezy.hr. www.acme.breezy.hr
counts as acme. Names that turn out to be vendor infrastructure fail provider validation later.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from company_dataset.models import SourceMeta
from company_dataset.normalize import BREEZY, GREENHOUSE, SMARTRECRUITERS
from company_dataset.sources._harvest import _SLUG_RE, parse_tokens, write_tokens

SUBDOMAIN_ATS = {"breezy.hr": BREEZY, "greenhouse.io": GREENHOUSE, "smartrecruiters.com": SMARTRECRUITERS}
# The vendors' own hosts: real names, but never a customer's board.
VENDOR_HOSTS = frozenset(("www", "app", "api", "boards", "job-boards", "jobs", "careers", "my", "mail", "assets", "assets-cdn", "cdn", "static", "status", "help", "support",
                          "docs", "blog", "admin", "dev", "staging", "test", "auth", "login", "sso", "go", "info", "email", "marketing", "developers"))

META = SourceMeta(
    name="passive_dns_2026_11_03",
    description="ATS-hosted board hostnames from passive DNS and certificate sources, via subfinder and Amass",
    download_url="https://github.com/projectdiscovery/subfinder",
    filename="tokens.csv",
    license="Public DNS and certificate records (factual data)",
    license_url="",
    license_note="Only hostnames are kept; every board is verified against its provider before use.",
    attribution="subfinder and Amass, over their public passive sources",
    downloadable=False,
)


def board_from_host(host: str) -> tuple[str, str] | None:
    host = host.strip().lower().rstrip(".").removeprefix("*.").removeprefix("www.")
    for domain, ats in SUBDOMAIN_ATS.items():
        if host.endswith("." + domain):
            label = host[: -len(domain) - 1]
            if "." not in label and label not in VENDOR_HOSTS and _SLUG_RE.match(label):
                return ats, label
    return None


def build(raw_dir: Path) -> dict:
    out = Path(raw_dir) / META.name
    tokens: dict[tuple[str, str], tuple[str, str, str]] = {}
    per_file = {}
    for path in sorted(out.glob("*.txt")):
        names = {line.strip() for line in path.read_text(errors="ignore").splitlines() if line.strip()}
        per_file[path.name] = len(names)
        for name in names:
            board = board_from_host(name)
            if board:
                tokens.setdefault((board[0], board[1].casefold()), (board[1], "", f"{path.stem}:{name}"))
    write_tokens(out / META.filename, tokens)
    return {"names_per_file": per_file, "boards": len(tokens)}


def parse(path: Path):
    return parse_tokens(path, META.name)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="company_dataset.sources.passive_dns")
    p.add_argument("--raw", default="data/raw")
    a = p.parse_args(argv)
    print(json.dumps(build(Path(a.raw)), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
