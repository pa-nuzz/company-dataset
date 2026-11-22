"""Board identifiers guessed from the Y Combinator company directory, confirmed against each vendor.

    python -m company_dataset.sources.yc_slugs --raw data/raw --db data/store.db --limit-per-vendor 5000

Startups in this directory use these ATS far more often than companies at large, so a guess here is
worth more than one from a general list. Same limits and rules as sources/slug_prober.py.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from company_dataset.http import HttpClient
from company_dataset.models import SourceMeta
from company_dataset.sources import slug_prober
from company_dataset.sources._harvest import parse_tokens

DIRECTORY = "https://yc-oss.github.io/api/companies/all.json"
ALL_IDENTIFIERS = 10**9

META = SourceMeta(
    name="yc_slugs_2026_11_03",
    description="Board identifiers guessed from Y Combinator company names and domains, confirmed against each vendor's public endpoint",
    download_url=DIRECTORY,
    filename="tokens.csv",
    license="Community mirror of a public directory; used for guesses only",
    license_url="https://github.com/yc-oss/api",
    license_note="Only identifiers a vendor answered for are kept; every board is verified against its provider before use.",
    attribution="yc-oss/api mirror of the Y Combinator company directory",
    downloadable=False,
)


def companies(payload: list[dict]) -> list[tuple[str, str]]:
    return [(c["name"], c["website"]) for c in payload if c.get("name") and c.get("website")]


def remaining_plan(companies_: list[tuple[str, str]], known: set[tuple[str, str]], vendors: list[str], tried_per_vendor: int, seed: int) -> dict[str, list[str]]:
    """Every identifier left for these vendors, after the first tried_per_vendor of the seeded order."""
    unknown = sorted(set(vendors) - set(slug_prober.VENDORS))
    if unknown:
        raise ValueError(f"unknown vendors: {unknown}")
    full = slug_prober.plan_slugs(companies_, known, ALL_IDENTIFIERS, seed)
    return {ats: full[ats][tried_per_vendor:] for ats in vendors}


def parse(path: Path):
    return parse_tokens(path, META.name)


def main(argv=None) -> int:
    from company_dataset import store

    p = argparse.ArgumentParser(prog="company_dataset.sources.yc_slugs")
    p.add_argument("--raw", default="data/raw")
    p.add_argument("--db", required=True)
    p.add_argument("--limit-per-vendor", type=int, default=None, help="a pilot: this many identifiers for every vendor")
    p.add_argument("--continue-vendors", default="", help="comma-separated vendors: probe all identifiers the pilot did not reach, for these vendors only")
    p.add_argument("--pilot-size", type=int, default=5000, help="--continue-vendors: identifiers per vendor the pilot already tried")
    p.add_argument("--seed", type=int, default=20261103)
    a = p.parse_args(argv)
    resp = HttpClient(timeout=120, max_attempts=3).get(DIRECTORY)
    conn = store.connect(a.db)
    known = {(r["ats"], r["code_key"]) for r in conn.execute("SELECT ats, code_key FROM boards")}
    conn.close()
    listed = companies(json.loads(resp.text))
    if a.continue_vendors:
        vendors = [v for v in a.continue_vendors.split(",") if v]
        # The pilot's hits are not in the store yet, so they count as unknown here, which keeps the seeded order the pilot used.
        plan = remaining_plan(listed, known, vendors, a.pilot_size, a.seed)
        print(f"[prober] continuing {({k: len(v) for k, v in plan.items()})}", flush=True)
        print(json.dumps(slug_prober.run(Path(a.raw), plan, meta=META, keep_existing=True, report_key="_".join(vendors) + "_continued"), indent=1))
        return 0
    if a.limit_per_vendor is None:
        p.error("give --limit-per-vendor or --continue-vendors")
    plan = slug_prober.plan_slugs(listed, known, a.limit_per_vendor, a.seed)
    print(json.dumps(slug_prober.run(Path(a.raw), plan, meta=META), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
