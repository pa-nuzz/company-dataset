"""Board verification outcomes. A board outcome says whether the board exists; it says
nothing about whose board it is (that is identity.py)."""
from __future__ import annotations

VERIFIED_LIVE = "verified_live"
VERIFIED_LIVE_ZERO_JOBS = "verified_live_zero_jobs"
DEAD_REMOVED = "dead_removed"
INVALID_IDENTIFIER = "invalid_identifier"
IDENTITY_MISMATCH = "identity_mismatch"
RATE_LIMITED = "rate_limited"
TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
BLOCKED_BY_SECURITY = "blocked_by_security"
INCONCLUSIVE = "inconclusive"

OUTCOMES = (
    VERIFIED_LIVE, VERIFIED_LIVE_ZERO_JOBS, DEAD_REMOVED, INVALID_IDENTIFIER, IDENTITY_MISMATCH,
    RATE_LIMITED, TEMPORARILY_UNAVAILABLE, BLOCKED_BY_SECURITY, INCONCLUSIVE,
)
LIVE = (VERIFIED_LIVE, VERIFIED_LIVE_ZERO_JOBS)
# Worth asking again later; the others are final until the board is re-discovered.
RETRYABLE = (RATE_LIMITED, TEMPORARILY_UNAVAILABLE, BLOCKED_BY_SECURITY, INCONCLUSIVE)


def classify(status: str | None, http_status: int | None, error: str | None, jobs_count: int | None) -> str:
    """Map a validator result (validate.py / validate_extra.py dict) to an outcome.

    A provider 404/410 for a well-formed code is reported as dead_removed: the APIs do not
    distinguish "never existed" from "removed". 400/422 mean the provider rejected the
    identifier itself.
    """
    error = error or ""
    if status == "valid":
        return VERIFIED_LIVE if (jobs_count or 0) > 0 else VERIFIED_LIVE_ZERO_JOBS
    if status == "invalid":
        if http_status in (400, 422) or error == "smartrecruiters_no_such_company":
            return INVALID_IDENTIFIER
        return DEAD_REMOVED
    if error == "company_identifier_mismatch":
        return IDENTITY_MISMATCH
    if http_status == 429:
        return RATE_LIMITED
    if http_status in (401, 403, 405):
        return BLOCKED_BY_SECURITY
    if http_status == 410:
        return DEAD_REMOVED
    if http_status in (400, 422):
        return INVALID_IDENTIFIER
    if (http_status or 0) >= 500 or (error.startswith("request_error") and "redirect" not in error.lower()):
        return TEMPORARILY_UNAVAILABLE
    # 200 with an empty or unprovable answer (SmartRecruiters totalFound=0, empty Breezy
    # board without page identity, unparseable JSON, redirect loops): existence not proven.
    return INCONCLUSIVE
