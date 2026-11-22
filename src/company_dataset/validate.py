from __future__ import annotations

import json
import random
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from company_dataset.config import resolve_rate_limits
from company_dataset.enrichment import (
    ashby_facts,
    greenhouse_facts,
    lever_facts,
    names_match,
    parse_page,
    site_name,
    strip_html,
)
from company_dataset.http import HttpClient, RequestError
from company_dataset.models import BoardValidation, Candidate, load_candidates, utc_now
from company_dataset.platforms import EXTRA_ATS
from company_dataset.validate_extra import validate_extra
from company_dataset.normalize import (
    ASHBY,
    BREEZY,
    GREENHOUSE,
    LEVER,
    SMARTRECRUITERS,
    canonical_api_url,
    canonical_board_url,
    greenhouse_board_info_url,
)

VALID = "valid"
INVALID = "invalid"
UNRESOLVED = "unresolved"

_BREEZY_GENERIC_IDENTITIES = {"breezy", "breezy hr", "breezyhr"}


class AtsRateLimiter:
    def __init__(self, limits: dict[str, dict] | None = None, sleep=time.sleep, clock=time.monotonic):
        self.limits = limits or {}
        self.sleep = sleep
        self.clock = clock
        self._semaphores = {}
        self._locks = {}
        self._last = {}
        for ats, cfg in self.limits.items():
            self._semaphores[ats] = threading.BoundedSemaphore(max(1, int(cfg["max_concurrent"])))
            self._locks[ats] = threading.Lock()
            self._last[ats] = None

    @contextmanager
    def slot(self, ats: str):
        cfg = self.limits.get(ats)
        if cfg is None:
            yield
            return
        with self._semaphores[ats]:
            with self._locks[ats]:
                now = self.clock()
                last = self._last[ats]
                if last is not None:
                    wait = last + float(cfg["delay_seconds"]) - now
                    if wait > 0:
                        self.sleep(wait)
                self._last[ats] = self.clock()
            yield


def _breezy_company_facts(position: dict) -> tuple[str | None, str | None]:
    company = position.get("company")
    identifier = None
    name = None
    if isinstance(company, dict):
        raw_identifier = company.get("friendly_id")
        if isinstance(raw_identifier, str) and raw_identifier.strip():
            identifier = raw_identifier.strip()
        raw_name = company.get("name")
        if isinstance(raw_name, str) and raw_name.strip():
            name = raw_name.strip()
    elif isinstance(company, str) and company.strip():
        name = company.strip()
    return identifier, name


def _breezy_page_identity(page, cand: Candidate) -> str | None:
    for value in (site_name(page), page.title):
        text = strip_html(value)
        if not text or text.casefold() in _BREEZY_GENERIC_IDENTITIES:
            continue
        for candidate in (cand.name, cand.code):
            if candidate and names_match(text, candidate):
                return text
    return None


SR_NO_SUCH_COMPANY = "smartrecruiters_no_such_company"
_SR_GENERIC_HOST = "jobs.smartrecruiters.com"


def smartrecruiters_generic_page(final_url: str) -> bool:
    """The root of SmartRecruiters' job search, where careers pages of unknown companies land."""
    parts = urlsplit(final_url or "")
    return parts.hostname == _SR_GENERIC_HOST and not parts.path.strip("/")


class BoardValidator:
    def __init__(self, client: HttpClient, cache_path: str | Path | None = None, max_workers: int = 10, progress_every: int = 250, logger=None, rate_limiter: AtsRateLimiter | None = None):
        self.client = client
        self.cache_path = Path(cache_path) if cache_path else None
        self.max_workers = max_workers
        self.progress_every = progress_every
        self.logger = logger
        self.rate_limiter = rate_limiter or AtsRateLimiter({})
        self.cache: dict[str, dict] = {}
        if self.cache_path and self.cache_path.exists():
            try:
                self.cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
            except ValueError:
                self.cache = {}

    def flush(self):
        if not self.cache_path:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cache), encoding="utf-8")
        tmp.replace(self.cache_path)

    def _get_json(self, url: str):
        try:
            resp = self.client.get(url)
        except RequestError:
            return None
        if resp.status != 200:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    def validate_one(self, cand: Candidate) -> BoardValidation:
        if cand.ats == GREENHOUSE:
            return self._validate_greenhouse(cand)
        if cand.ats == LEVER:
            return self._validate_lever(cand)
        if cand.ats == ASHBY:
            return self._validate_ashby(cand)
        if cand.ats == SMARTRECRUITERS:
            return self._validate_smartrecruiters(cand)
        if cand.ats == BREEZY:
            return self._validate_breezy(cand)
        if cand.ats in EXTRA_ATS:
            return validate_extra(self, cand)
        return BoardValidation(ats=cand.ats, code=cand.code, status=UNRESOLVED, error="unknown ats", checked_at=utc_now())

    def _validate_greenhouse(self, cand: Candidate) -> BoardValidation:
        now = utc_now()
        codes = list(dict.fromkeys(cand.codes))
        for code in codes:
            url = canonical_api_url(GREENHOUSE, code)
            try:
                resp = self.client.get(url)
            except RequestError as exc:
                return BoardValidation(ats=cand.ats, code=cand.code, status=UNRESOLVED, error=f"request_error: {exc}", checked_at=now)
            if resp.status == 404:
                continue
            if resp.status != 200:
                return BoardValidation(ats=cand.ats, code=cand.code, status=UNRESOLVED, http_status=resp.status, error=f"http_{resp.status}", checked_at=now)
            try:
                payload = resp.json()
            except ValueError:
                return BoardValidation(ats=cand.ats, code=cand.code, status=UNRESOLVED, http_status=200, error="invalid_json", checked_at=now)
            if not isinstance(payload, dict) or "jobs" not in payload:
                return BoardValidation(ats=cand.ats, code=cand.code, status=UNRESOLVED, http_status=200, error="unexpected_schema", checked_at=now)
            facts = greenhouse_facts(payload)
            company_name = facts["company_name"]
            description = None
            info = self._get_json(greenhouse_board_info_url(code))
            if info is not None:
                info_name = info.get("name") or None
                company_name = company_name or (str(info_name).strip() if info_name else None)
                content = strip_html(info.get("content") or "")
                if content:
                    description = content
            return BoardValidation(
                ats=cand.ats,
                code=cand.code,
                status=VALID,
                http_status=200,
                jobs_count=facts["jobs_count"],
                company_name=company_name,
                description=description,
                website_url=facts["website_url"],
                # The one API serves both instances; EU boards link their jobs on the EU host.
                region_answered="eu" if ".eu.greenhouse.io/" in json.dumps(payload.get("jobs") or [])[:20000] else None,
                code_validated=code,
                checked_at=now,
            )
        return BoardValidation(ats=cand.ats, code=cand.code, status=INVALID, http_status=404, error="board_not_found", checked_at=now)

    def _validate_lever(self, cand: Candidate) -> BoardValidation:
        now = utc_now()
        codes = list(dict.fromkeys(cand.codes))
        hosts = ["eu", None] if cand.region == "eu" else [None, "eu"]
        for region in hosts:
            for code in codes:
                url = canonical_api_url(LEVER, code, region)
                try:
                    resp = self.client.get(url)
                except RequestError as exc:
                    return BoardValidation(ats=cand.ats, code=cand.code, status=UNRESOLVED, error=f"request_error: {exc}", checked_at=now)
                if resp.status == 404:
                    continue
                if resp.status != 200:
                    return BoardValidation(ats=cand.ats, code=cand.code, status=UNRESOLVED, http_status=resp.status, error=f"http_{resp.status}", checked_at=now)
                try:
                    payload = resp.json()
                except ValueError:
                    return BoardValidation(ats=cand.ats, code=cand.code, status=UNRESOLVED, http_status=200, error="invalid_json", checked_at=now)
                if not isinstance(payload, list):
                    return BoardValidation(ats=cand.ats, code=cand.code, status=UNRESOLVED, http_status=200, error="unexpected_schema", checked_at=now)
                facts = lever_facts(payload)
                return BoardValidation(
                    ats=cand.ats,
                    code=cand.code,
                    status=VALID,
                    http_status=200,
                    jobs_count=facts["jobs_count"],
                    country=facts["country"],
                    country_basis="lever_postings_unanimous" if facts["country"] else None,
                    region_answered="eu" if region == "eu" else None,
                    code_validated=code,
                    checked_at=now,
                )
        return BoardValidation(ats=cand.ats, code=cand.code, status=INVALID, http_status=404, error="board_not_found", checked_at=now)

    def _validate_ashby(self, cand: Candidate) -> BoardValidation:
        now = utc_now()
        codes = list(dict.fromkeys(cand.codes))
        for code in codes:
            url = canonical_api_url(ASHBY, code)
            try:
                resp = self.client.get(url)
            except RequestError as exc:
                return BoardValidation(ats=cand.ats, code=cand.code, status=UNRESOLVED, error=f"request_error: {exc}", checked_at=now)
            if resp.status == 404:
                continue
            if resp.status != 200:
                return BoardValidation(ats=cand.ats, code=cand.code, status=UNRESOLVED, http_status=resp.status, error=f"http_{resp.status}", checked_at=now)
            try:
                payload = resp.json()
            except ValueError:
                return BoardValidation(ats=cand.ats, code=cand.code, status=UNRESOLVED, http_status=200, error="invalid_json", checked_at=now)
            if not isinstance(payload, dict) or "jobs" not in payload:
                return BoardValidation(ats=cand.ats, code=cand.code, status=UNRESOLVED, http_status=200, error="unexpected_schema", checked_at=now)
            facts = ashby_facts(payload)
            return BoardValidation(
                ats=cand.ats,
                code=cand.code,
                status=VALID,
                http_status=200,
                jobs_count=facts["jobs_count"],
                country=facts["country"],
                country_basis="ashby_postings_unanimous" if facts["country"] else None,
                code_validated=code,
                checked_at=now,
            )
        return BoardValidation(ats=cand.ats, code=cand.code, status=INVALID, http_status=404, error="board_not_found", checked_at=now)

    def _smartrecruiters_zero_jobs(self, cand: Candidate, code: str, now: str) -> BoardValidation:
        """The postings API answers 200 totalFound=0 for any identifier, so only the careers page
        can show the company exists: an unknown one is sent to the generic job search page."""
        try:
            with self.rate_limiter.slot(SMARTRECRUITERS):
                page = self.client.get(canonical_board_url(SMARTRECRUITERS, code))
        except RequestError as exc:
            return BoardValidation(ats=cand.ats, code=cand.code, status=UNRESOLVED, jobs_count=0, error=f"request_error: {exc}", checked_at=now)
        if page.status == 404:
            return BoardValidation(ats=cand.ats, code=cand.code, status=INVALID, http_status=404, error="board_not_found", checked_at=now)
        if page.status != 200:
            return BoardValidation(ats=cand.ats, code=cand.code, status=UNRESOLVED, http_status=page.status, jobs_count=0, error=f"http_{page.status}", checked_at=now)
        if smartrecruiters_generic_page(page.url):
            return BoardValidation(ats=cand.ats, code=cand.code, status=INVALID, http_status=200, error=SR_NO_SUCH_COMPANY, checked_at=now)
        return BoardValidation(ats=cand.ats, code=cand.code, status="valid", http_status=200, jobs_count=0, error="total_found_zero", code_validated=code, checked_at=now)

    def _validate_smartrecruiters(self, cand: Candidate) -> BoardValidation:
        now = utc_now()
        codes = list(dict.fromkeys(cand.codes))
        for code in codes:
            url = canonical_api_url(SMARTRECRUITERS, code)
            try:
                with self.rate_limiter.slot(SMARTRECRUITERS):
                    resp = self.client.get(url)
            except RequestError as exc:
                return BoardValidation(ats=cand.ats, code=cand.code, status=UNRESOLVED, error=f"request_error: {exc}", checked_at=now)
            if resp.status == 404:
                continue
            if resp.status != 200:
                return BoardValidation(ats=cand.ats, code=cand.code, status=UNRESOLVED, http_status=resp.status, error=f"http_{resp.status}", checked_at=now)
            try:
                payload = resp.json()
            except ValueError:
                return BoardValidation(ats=cand.ats, code=cand.code, status=UNRESOLVED, http_status=200, error="invalid_json", checked_at=now)
            if not isinstance(payload, dict):
                return BoardValidation(ats=cand.ats, code=cand.code, status=UNRESOLVED, http_status=200, error="unexpected_schema", checked_at=now)
            total_found = payload.get("totalFound")
            content = payload.get("content")
            if (
                not isinstance(total_found, int)
                or isinstance(total_found, bool)
                or total_found < 0
                or not isinstance(content, list)
            ):
                return BoardValidation(ats=cand.ats, code=cand.code, status=UNRESOLVED, http_status=200, error="unexpected_schema", checked_at=now)
            if total_found == 0:
                return self._smartrecruiters_zero_jobs(cand, code, now)
            if not content:
                return BoardValidation(ats=cand.ats, code=cand.code, status=UNRESOLVED, http_status=200, error="unexpected_schema", checked_at=now)
            identifiers = []
            names = []
            for posting in content:
                company = posting.get("company") if isinstance(posting, dict) else None
                identifier = company.get("identifier") if isinstance(company, dict) else None
                if not isinstance(identifier, str) or not identifier.strip():
                    return BoardValidation(
                        ats=cand.ats,
                        code=cand.code,
                        status=UNRESOLVED,
                        http_status=200,
                        jobs_count=total_found,
                        company_identifier=identifiers[0] if identifiers else None,
                        company_name=names[0] if names else None,
                        error="missing_company_identifier",
                        checked_at=now,
                    )
                identifiers.append(identifier.strip())
                name = company.get("name")
                if isinstance(name, str) and name.strip():
                    names.append(name.strip())
            if any(identifier.casefold() != code.casefold() for identifier in identifiers):
                return BoardValidation(
                    ats=cand.ats,
                    code=cand.code,
                    status=UNRESOLVED,
                    http_status=200,
                    jobs_count=total_found,
                    company_identifier=identifiers[0],
                    company_name=names[0] if names else None,
                    error="company_identifier_mismatch",
                    checked_at=now,
                )
            return BoardValidation(
                ats=cand.ats,
                code=cand.code,
                status=VALID,
                http_status=200,
                jobs_count=total_found,
                company_identifier=identifiers[0],
                company_name=names[0] if names else None,
                code_validated=code,
                checked_at=now,
            )
        return BoardValidation(ats=cand.ats, code=cand.code, status=INVALID, http_status=404, error="board_not_found", checked_at=now)

    def _validate_breezy(self, cand: Candidate) -> BoardValidation:
        now = utc_now()
        codes = list(dict.fromkeys(cand.codes))
        for code in codes:
            url = canonical_api_url(BREEZY, code)
            try:
                with self.rate_limiter.slot(BREEZY):
                    resp = self.client.get(url)
            except RequestError as exc:
                return BoardValidation(ats=cand.ats, code=cand.code, status=UNRESOLVED, error=f"request_error: {exc}", checked_at=now)
            if resp.status == 404:
                if "career portal not found" in resp.text.casefold():
                    continue
                return BoardValidation(ats=cand.ats, code=cand.code, status=UNRESOLVED, http_status=404, error="http_404_unrecognized", checked_at=now)
            if resp.status != 200:
                return BoardValidation(ats=cand.ats, code=cand.code, status=UNRESOLVED, http_status=resp.status, error=f"http_{resp.status}", checked_at=now)
            try:
                payload = resp.json()
            except ValueError:
                return BoardValidation(ats=cand.ats, code=cand.code, status=UNRESOLVED, http_status=200, error="invalid_json", checked_at=now)
            if not isinstance(payload, list):
                return BoardValidation(ats=cand.ats, code=cand.code, status=UNRESOLVED, http_status=200, error="unexpected_schema", checked_at=now)
            if payload:
                if not isinstance(payload[0], dict):
                    return BoardValidation(ats=cand.ats, code=cand.code, status=UNRESOLVED, http_status=200, error="unexpected_schema", checked_at=now)
                identifier, name = _breezy_company_facts(payload[0])
                if identifier is not None and identifier.casefold() != code.casefold():
                    return BoardValidation(
                        ats=cand.ats,
                        code=cand.code,
                        status=UNRESOLVED,
                        http_status=200,
                        jobs_count=len(payload),
                        company_identifier=identifier,
                        company_name=name,
                        error="company_identifier_mismatch",
                        checked_at=now,
                    )
                return BoardValidation(
                    ats=cand.ats,
                    code=cand.code,
                    status=VALID,
                    http_status=200,
                    jobs_count=len(payload),
                    company_identifier=identifier,
                    company_name=name,
                    code_validated=code,
                    checked_at=now,
                )
            page_url = canonical_board_url(BREEZY, code)
            try:
                with self.rate_limiter.slot(BREEZY):
                    page_resp = self.client.get(page_url)
            except RequestError as exc:
                return BoardValidation(ats=cand.ats, code=cand.code, status=UNRESOLVED, http_status=200, jobs_count=0, error=f"hosted_page_request_error: {exc}", checked_at=now)
            if page_resp.status != 200:
                return BoardValidation(ats=cand.ats, code=cand.code, status=UNRESOLVED, http_status=200, jobs_count=0, error=f"hosted_page_http_{page_resp.status}", checked_at=now)
            identity = _breezy_page_identity(parse_page(page_resp.text), cand)
            if identity is None:
                return BoardValidation(ats=cand.ats, code=cand.code, status=UNRESOLVED, http_status=200, jobs_count=0, error="exists_no_identity", checked_at=now)
            return BoardValidation(
                ats=cand.ats,
                code=cand.code,
                status=VALID,
                http_status=200,
                jobs_count=0,
                company_name=identity,
                code_validated=code,
                checked_at=now,
            )
        return BoardValidation(ats=cand.ats, code=cand.code, status=INVALID, http_status=404, error="board_not_found", checked_at=now)

    def validate_many(self, candidates, limit: int | None = None, revalidate: bool = False, max_age_days: float | None = None) -> dict[str, BoardValidation]:
        results: dict[str, BoardValidation] = {}
        todo: list[Candidate] = []
        cutoff = None
        if max_age_days is not None:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for cand in candidates:
            cached = self.cache.get(cand.key)
            fresh = cached is not None and (cutoff is None or (cached.get("checked_at") or "") >= cutoff)
            if cached and fresh and not revalidate and cached.get("status") in (VALID, INVALID):
                results[cand.key] = BoardValidation.from_dict(cached)
            else:
                todo.append(cand)
        if limit is not None:
            per_ats: Counter = Counter()
            selected = []
            for cand in todo:
                if per_ats[cand.ats] < limit:
                    selected.append(cand)
                    per_ats[cand.ats] += 1
            todo = selected
        if not todo:
            return results
        done = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = [executor.submit(self.validate_one, cand) for cand in todo]
            for future in as_completed(futures):
                outcome = future.result()
                results[outcome.key] = outcome
                self.cache[outcome.key] = outcome.to_dict()
                done += 1
                if self.progress_every and done % self.progress_every == 0:
                    self.flush()
                    if self.logger:
                        statuses = Counter(v.status for v in results.values())
                        self.logger(f"[validate] {done}/{len(todo)} checked (valid={statuses.get(VALID, 0)} invalid={statuses.get(INVALID, 0)} unresolved={statuses.get(UNRESOLVED, 0)})")
        self.flush()
        return results


def run_validate(
    dirs: dict,
    client: HttpClient | None = None,
    max_workers: int = 10,
    per_host_interval: float = 0.15,
    timeout: float = 20.0,
    limit: int | None = None,
    revalidate: bool = False,
    max_age_days: float | None = None,
    ats: set[str] | None = None,
    rate_limiter: AtsRateLimiter | None = None,
    rate_limits: dict[str, dict] | None = None,
    sample: int | None = None,
    seed: int = 1,
) -> dict:
    candidates = load_candidates(dirs)
    if ats:
        candidates = [c for c in candidates if c.ats in ats]
    if sample is not None and sample > 0:
        rnd = random.Random(seed)
        sampled = []
        for name in sorted({c.ats for c in candidates}):
            group = sorted((c for c in candidates if c.ats == name), key=lambda c: c.key)
            sampled.extend(rnd.sample(group, min(sample, len(group))))
        candidates = sampled
    if not candidates:
        if ats:
            print(f"[validate] no candidates for ats={','.join(sorted(ats))}")
        else:
            print("[validate] no candidates found; run build stage first")
        return {}
    client = client or HttpClient(timeout=timeout, per_host_interval=per_host_interval)
    validator = BoardValidator(
        client,
        cache_path=dirs["cache"] / "validation.json",
        max_workers=max_workers,
        progress_every=250,
        logger=print,
        rate_limiter=rate_limiter or AtsRateLimiter(rate_limits or resolve_rate_limits()),
    )
    results = validator.validate_many(candidates, limit=limit, revalidate=revalidate, max_age_days=max_age_days)
    by_status = Counter(v.status for v in results.values())
    by_ats: dict[str, Counter] = {}
    unresolved_reasons: dict[str, Counter] = {}
    for v in results.values():
        by_ats.setdefault(v.ats, Counter())[v.status] += 1
        if v.status == UNRESOLVED:
            reason = (v.error or "unknown").split(":", 1)[0].strip()
            unresolved_reasons.setdefault(v.ats, Counter())[reason] += 1
    stats = {
        "generated_at": utc_now(),
        "candidates_total": len(candidates),
        "checked": len(results),
        "by_status": dict(by_status),
        "by_ats": {ats: dict(counter) for ats, counter in by_ats.items()},
        "unresolved_reasons": {ats: dict(counter) for ats, counter in unresolved_reasons.items()},
    }
    stats_path = dirs["intermediate"] / "validation_stats.json"
    stats_path.write_text(json.dumps(stats, indent=2))
    print(
        f"[validate] checked={len(results)} valid={by_status.get(VALID, 0)} "
        f"invalid={by_status.get(INVALID, 0)} unresolved={by_status.get(UNRESOLVED, 0)}"
    )
    return stats
