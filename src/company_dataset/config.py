from __future__ import annotations

import os
from pathlib import Path

RAW_DIR_NAME = "raw"
CACHE_DIR_NAME = "cache"
INTERMEDIATE_DIR_NAME = "intermediate"
OUTPUT_DIR_NAME = "output"
REVIEW_DIR_NAME = "review"
REPORTS_DIR_NAME = "reports"

DEFAULT_DATA_DIR = "data"

ATS_RATE_LIMITS = {
    "smartrecruiters": {"max_concurrent": 2, "delay_seconds": 0.5},
    "breezy": {"max_concurrent": 2, "delay_seconds": 0.5},
    # One shared API host each: keep these gentle.
    "workable": {"max_concurrent": 1, "delay_seconds": 0.7},  # about 1.4 requests a second; it answers 429 well before 4
    "rippling": {"max_concurrent": 3, "delay_seconds": 0.3},
    "join": {"max_concurrent": 4, "delay_seconds": 0.25},
    # Per-company subdomains on shared vendor infrastructure.
    "bamboohr": {"max_concurrent": 4, "delay_seconds": 0.15},
    "recruitee": {"max_concurrent": 4, "delay_seconds": 0.15},
    "teamtailor": {"max_concurrent": 4, "delay_seconds": 0.15},
    "personio": {"max_concurrent": 4, "delay_seconds": 0.15},
    "pinpoint": {"max_concurrent": 3, "delay_seconds": 0.2},
    "icims": {"max_concurrent": 3, "delay_seconds": 0.3},
    "workday": {"max_concurrent": 4, "delay_seconds": 0.2},
}

RATE_LIMIT_ENV_VARS = {
    "smartrecruiters": {
        "max_concurrent": "COMPANY_DATASET_SMARTRECRUITERS_MAX_CONCURRENT",
        "delay_seconds": "COMPANY_DATASET_SMARTRECRUITERS_DELAY_SECONDS",
    },
    "breezy": {
        "max_concurrent": "COMPANY_DATASET_BREEZY_MAX_CONCURRENT",
        "delay_seconds": "COMPANY_DATASET_BREEZY_DELAY_SECONDS",
    },
}


def _parse_rate_limit_value(field: str, raw: str, var: str):
    try:
        value = int(raw) if field == "max_concurrent" else float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {var}: {raw!r}") from exc
    if field == "max_concurrent" and value < 1:
        raise ValueError(f"invalid {var}: must be >= 1")
    if field == "delay_seconds" and value < 0:
        raise ValueError(f"invalid {var}: must be >= 0")
    return value


# Run-now settings for the two vendors with a shared rate limit. Above this, 429s turn real boards into
# inconclusive results, which costs more accuracy and time than it saves.
# Workable is left out: at these rates it answered 429 to nearly every request.
FAST_RATE_LIMITS = {"smartrecruiters": {"max_concurrent": 8, "delay_seconds": 0.1}, "breezy": {"max_concurrent": 16, "delay_seconds": 0.05}}
FAST_PER_HOST_INTERVAL = 0.1
FAST_WORKERS = 64


def resolve_rate_limits(
    overrides: dict[str, dict] | None = None,
    env=None,
) -> dict[str, dict]:
    env = os.environ if env is None else env
    limits = {ats: dict(cfg) for ats, cfg in ATS_RATE_LIMITS.items()}
    for ats, fields in RATE_LIMIT_ENV_VARS.items():
        for field, var in fields.items():
            raw = env.get(var)
            if raw is None or str(raw).strip() == "":
                continue
            limits[ats][field] = _parse_rate_limit_value(field, str(raw).strip(), var)
    for ats, fields in (overrides or {}).items():
        for field, value in fields.items():
            if value is not None:
                limits[ats][field] = value
    return limits


def resolve_dirs(data_dir: str | Path = DEFAULT_DATA_DIR) -> dict[str, Path]:
    base = Path(data_dir)
    dirs = {
        "data": base,
        "raw": base / RAW_DIR_NAME,
        "cache": base / CACHE_DIR_NAME,
        "intermediate": base / INTERMEDIATE_DIR_NAME,
        "output": base / OUTPUT_DIR_NAME,
        "review": base / REVIEW_DIR_NAME,
        "reports": Path(REPORTS_DIR_NAME),
    }
    for key, path in dirs.items():
        path.mkdir(parents=True, exist_ok=True)
    return dirs
