from __future__ import annotations

import csv
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path

# The 7 business-required fields (per the project spec). `country` is additional
# metadata, not a required field, and must never be counted toward this list.
REQUIRED_FIELDS = ("name", "description", "linkedin_url", "website_url", "careersite_url", "ats_system", "ats_code")


@dataclass
class SourceMeta:
    name: str
    description: str
    download_url: str
    filename: str
    license: str
    license_url: str = ""
    license_note: str = ""
    attribution: str = ""
    downloadable: bool = True  # False: download_url documents the origin; the raw file came from a one-off harvest


@dataclass
class SourceRecord:
    source: str
    ats: str
    code: str
    region: str | None = None
    name: str | None = None
    careersite_url: str | None = None
    row_ref: str = ""


@dataclass
class Reject:
    source: str
    reason: str
    detail: str = ""
    row_ref: str = ""
    ats: str = ""
    code: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class Candidate:
    ats: str
    codes: list[str] = field(default_factory=list)
    region: str | None = None
    name: str | None = None
    name_source: str = ""
    careersite_urls: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    @property
    def code(self) -> str:
        return self.codes[0]

    @property
    def key(self) -> str:
        return f"{self.ats}:{self.code.casefold()}"


@dataclass
class BoardValidation:
    ats: str
    code: str
    status: str
    http_status: int | None = None
    jobs_count: int | None = None
    company_name: str | None = None
    company_identifier: str | None = None
    description: str | None = None
    website_url: str | None = None
    country: str | None = None
    country_basis: str | None = None
    region_answered: str | None = None
    code_validated: str | None = None
    error: str | None = None
    checked_at: str = ""

    @property
    def key(self) -> str:
        return f"{self.ats}:{self.code.casefold()}"

    def to_dict(self) -> dict:
        return {
            "ats": self.ats,
            "code": self.code,
            "status": self.status,
            "http_status": self.http_status,
            "jobs_count": self.jobs_count,
            "company_name": self.company_name,
            "company_identifier": self.company_identifier,
            "description": self.description,
            "website_url": self.website_url,
            "country": self.country,
            "country_basis": self.country_basis,
            "region_answered": self.region_answered,
            "code_validated": self.code_validated,
            "error": self.error,
            "checked_at": self.checked_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BoardValidation":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_candidates(dirs: dict) -> list[Candidate]:
    path = Path(dirs["intermediate"]) / "candidates.csv"
    candidates: list[Candidate] = []
    if not path.exists():
        return candidates
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            alt = [c for c in (row.get("alt_codes") or "").split(";") if c]
            candidates.append(
                Candidate(
                    ats=row["ats"],
                    codes=[row["code"]] + alt,
                    region=row.get("region") or None,
                    name=row.get("name") or None,
                    name_source=row.get("name_source") or "",
                    careersite_urls=[u for u in (row.get("careersite_urls") or "").split("; ") if u],
                    sources=[s for s in (row.get("sources") or "").split("; ") if s],
                )
            )
    return candidates
