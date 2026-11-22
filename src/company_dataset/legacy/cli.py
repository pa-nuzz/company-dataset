from __future__ import annotations

import argparse
import sys

from company_dataset.legacy.build import run_build
from company_dataset.config import resolve_dirs, resolve_rate_limits
from company_dataset.legacy.download import run_download
from company_dataset.enrichment import run_enrich
from company_dataset.legacy.export import run_export
from company_dataset.normalize import SUPPORTED_ATS
from company_dataset.legacy.report import run_report
from company_dataset.validate import run_validate


def parse_ats(value: str) -> set[str]:
    ats = {item.strip() for item in value.split(",") if item.strip()}
    unknown = ats - set(SUPPORTED_ATS)
    if unknown:
        raise argparse.ArgumentTypeError(f"unsupported ats: {', '.join(sorted(unknown))}")
    if not ats:
        raise argparse.ArgumentTypeError("at least one ats is required")
    return ats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="company-dataset",
        description="Build a verified dataset of companies using Greenhouse, Lever, or Ashby public job boards",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="all",
        choices=["download", "build", "validate", "enrich", "export", "report", "all"],
        help="pipeline stage to run (default: all)",
    )
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--workers", type=int, default=10, help="max concurrent requests")
    parser.add_argument("--per-host-delay", type=float, default=0.15, help="min seconds between requests per host")
    parser.add_argument("--timeout", type=float, default=20.0, help="per-request timeout in seconds")
    parser.add_argument("--limit", type=int, default=None, help="validate at most N boards per ATS (smoke runs)")
    parser.add_argument("--ats", type=parse_ats, default=None, help="comma-separated ATS filter for validation")
    parser.add_argument("--smartrecruiters-max-concurrent", type=int, default=None, help="SmartRecruiters per-ATS concurrency override")
    parser.add_argument("--smartrecruiters-delay-seconds", type=float, default=None, help="SmartRecruiters per-ATS delay override")
    parser.add_argument("--breezy-max-concurrent", type=int, default=None, help="Breezy per-ATS concurrency override")
    parser.add_argument("--breezy-delay-seconds", type=float, default=None, help="Breezy per-ATS delay override")
    parser.add_argument("--run-dir", type=str, default=None, help="output directory for this run (must not exist)")
    parser.add_argument("--sample", type=int, default=None, help="validate a random sample of N boards per ATS after --ats filter (deterministic with --seed)")
    parser.add_argument("--seed", type=int, default=1, help="random seed for --sample")
    parser.add_argument("--revalidate", action="store_true", help="ignore cached validation results")
    parser.add_argument("--max-age-days", type=float, default=None, help="recheck cached validation results older than this")
    parser.add_argument("--refresh", action="store_true", help="re-download sources even when a cached copy exists")
    return parser


def main(argv=None) -> int:
    print("company-dataset-legacy is deprecated: use the store-backed pipeline (company-dataset).", file=sys.stderr)
    args = build_parser().parse_args(argv)
    dirs = resolve_dirs(args.data_dir)
    command = args.command
    if command in ("download", "all"):
        run_download(dirs, refresh=args.refresh)
    if command in ("build", "all"):
        run_build(dirs)
    if command in ("validate", "all"):
        rate_limits = resolve_rate_limits(
            overrides={
                "smartrecruiters": {
                    "max_concurrent": args.smartrecruiters_max_concurrent,
                    "delay_seconds": args.smartrecruiters_delay_seconds,
                },
                "breezy": {
                    "max_concurrent": args.breezy_max_concurrent,
                    "delay_seconds": args.breezy_delay_seconds,
                },
            }
        )
        run_validate(
            dirs,
            max_workers=args.workers,
            per_host_interval=args.per_host_delay,
            timeout=args.timeout,
            limit=args.limit,
            revalidate=args.revalidate,
            max_age_days=args.max_age_days,
            ats=args.ats,
            rate_limits=rate_limits,
            sample=args.sample,
            seed=args.seed,
        )
    if command in ("enrich", "all"):
        run_enrich(dirs, max_workers=args.workers, timeout=args.timeout)
    if command in ("export", "all"):
        run_export(dirs, run_dir=args.run_dir)
    if command in ("report", "all"):
        run_report(dirs, run_dir=args.run_dir)
    return 0
