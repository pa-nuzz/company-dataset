"""Live validation for the platforms in company_dataset.platforms.

Same rules as the original five: a board is invalid only when every candidate code gets the
platform's own "no such board" answer; 403/429/5xx/timeouts/odd payloads are unresolved.
"""
from __future__ import annotations

import re

from company_dataset.http import RequestError
from company_dataset.models import BoardValidation, Candidate, utc_now
from company_dataset.platforms import (
    BAMBOOHR,
    ICIMS,
    JOIN,
    PERSONIO,
    PINPOINT,
    RECRUITEE,
    RIPPLING,
    TEAMTAILOR,
    WORKABLE,
    WORKDAY,
    WORKDAY_QUERY,
    count_icims_jobs,
    count_join_jobs,
    count_personio_jobs,
    extra_api_url,
    loads,
    name_from_title,
    page_title,
    same_host,
    strip_tags,
)

VALID = "valid"
INVALID = "invalid"
UNRESOLVED = "unresolved"


class _NotFound(Exception):
    pass


class _Unresolved(Exception):
    def __init__(self, error: str, http_status: int | None = None):
        super().__init__(error)
        self.error = error
        self.http_status = http_status


def _get(v, ats, url):
    try:
        with v.rate_limiter.slot(ats):
            return v.client.get(url)
    except RequestError as exc:
        raise _Unresolved(f"request_error: {exc}")


def _post(v, ats, url, payload):
    try:
        with v.rate_limiter.slot(ats):
            return v.client.post_json(url, payload)
    except RequestError as exc:
        raise _Unresolved(f"request_error: {exc}")


def _json_or_unresolved(resp):
    data = loads(resp.text)
    if data is None:
        raise _Unresolved("invalid_json", 200)
    return data


def _check(resp, not_found=(404,)):
    if resp.status in not_found:
        raise _NotFound()
    if resp.status != 200:
        raise _Unresolved(f"http_{resp.status}", resp.status)


def _workable(v, cand, code, region):
    resp = _get(v, WORKABLE, extra_api_url(WORKABLE, code))
    _check(resp)
    data = _json_or_unresolved(resp)
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list) or "name" not in data:
        raise _Unresolved("unexpected_schema", 200)
    return {"jobs_count": len(data["jobs"]), "company_name": (data.get("name") or "").strip() or None, "description": strip_tags(data.get("description"))}


def _workday(v, cand, code, region):
    resp = _post(v, WORKDAY, extra_api_url(WORKDAY, code), WORKDAY_QUERY)
    # Workday answers an unknown tenant or site with 422 (or 404 for an unknown host path).
    _check(resp, not_found=(404, 422))
    data = _json_or_unresolved(resp)
    if not isinstance(data, dict) or not isinstance(data.get("total"), int):
        raise _Unresolved("unexpected_schema", 200)
    return {"jobs_count": data["total"]}


def _bamboohr(v, cand, code, region):
    resp = _get(v, BAMBOOHR, extra_api_url(BAMBOOHR, code))
    # An unknown subdomain redirects to the BambooHR marketing site.
    if not same_host(resp.url, f"{code}.bamboohr.com"):
        raise _NotFound()
    _check(resp)
    data = _json_or_unresolved(resp)
    if not isinstance(data, dict) or not isinstance(data.get("result"), list):
        raise _Unresolved("unexpected_schema", 200)
    name = None
    info = _get(v, BAMBOOHR, f"https://{code}.bamboohr.com/careers/company-info")
    if info.status == 200 and same_host(info.url, f"{code}.bamboohr.com"):
        payload = loads(info.text)
        if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
            name = (payload["result"].get("name") or "").strip() or None
    return {"jobs_count": len(data["result"]), "company_name": name}


def _recruitee(v, cand, code, region):
    resp = _get(v, RECRUITEE, extra_api_url(RECRUITEE, code))
    _check(resp)
    data = _json_or_unresolved(resp)
    if not isinstance(data, dict) or not isinstance(data.get("offers"), list):
        raise _Unresolved("unexpected_schema", 200)
    names = {(o.get("company_name") or "").strip() for o in data["offers"] if isinstance(o, dict)} - {""}
    return {"jobs_count": len(data["offers"]), "company_name": sorted(names)[0] if len(names) == 1 else None}


def _teamtailor(v, cand, code, region):
    resp = _get(v, TEAMTAILOR, extra_api_url(TEAMTAILOR, code))
    _check(resp)
    data = _json_or_unresolved(resp)
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise _Unresolved("unexpected_schema", 200)
    title = (data.get("title") or "").strip()
    return {"jobs_count": len(data["items"]), "company_name": title or None}


def _personio(v, cand, code, region):
    hosts = ["com", None] if region == "com" else [None, "com"]
    for r in hosts:
        resp = _get(v, PERSONIO, extra_api_url(PERSONIO, code, r))
        if resp.status == 404:
            continue
        _check(resp)
        if "<workzag-jobs" not in resp.text:
            raise _Unresolved("unexpected_schema", 200)
        page = _get(v, PERSONIO, resp.url.rsplit("/xml", 1)[0] + "/")
        name = name_from_title(PERSONIO, page_title(page.text)) if page.status == 200 else None
        return {"jobs_count": count_personio_jobs(resp.text), "company_name": name, "region_answered": r}
    raise _NotFound()


def _rippling(v, cand, code, region):
    resp = _get(v, RIPPLING, extra_api_url(RIPPLING, code))
    _check(resp)
    data = _json_or_unresolved(resp)
    if not isinstance(data, list):
        raise _Unresolved("unexpected_schema", 200)
    return {"jobs_count": len(data)}


def _icims(v, cand, code, region):
    resp = _get(v, ICIMS, extra_api_url(ICIMS, code))
    _check(resp)
    if not same_host(resp.url, f"{code}.icims.com"):
        raise _Unresolved("redirected_off_board", 200)
    name = name_from_title(ICIMS, page_title(resp.text))
    return {"jobs_count": count_icims_jobs(resp.text), "company_name": name}


def _pinpoint(v, cand, code, region):
    resp = _get(v, PINPOINT, extra_api_url(PINPOINT, code))
    _check(resp)
    data = _json_or_unresolved(resp)
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise _Unresolved("unexpected_schema", 200)
    return {"jobs_count": len(data["data"])}


def _join(v, cand, code, region):
    resp = _get(v, JOIN, extra_api_url(JOIN, code))
    _check(resp)
    name = name_from_title(JOIN, page_title(resp.text))
    if name is None:
        raise _Unresolved("exists_no_identity", 200)
    return {"jobs_count": count_join_jobs(resp.text, code), "company_name": name}


_HANDLERS = {
    WORKABLE: _workable,
    WORKDAY: _workday,
    BAMBOOHR: _bamboohr,
    RECRUITEE: _recruitee,
    TEAMTAILOR: _teamtailor,
    PERSONIO: _personio,
    RIPPLING: _rippling,
    ICIMS: _icims,
    PINPOINT: _pinpoint,
    JOIN: _join,
}


def validate_extra(v, cand: Candidate) -> BoardValidation:
    now = utc_now()
    handler = _HANDLERS[cand.ats]
    for code in dict.fromkeys(cand.codes):
        try:
            facts = handler(v, cand, code, cand.region)
        except _NotFound:
            continue
        except _Unresolved as exc:
            return BoardValidation(ats=cand.ats, code=cand.code, status=UNRESOLVED, http_status=exc.http_status, error=exc.error, checked_at=now)
        return BoardValidation(ats=cand.ats, code=cand.code, status=VALID, http_status=200, code_validated=code, checked_at=now, **facts)
    return BoardValidation(ats=cand.ats, code=cand.code, status=INVALID, http_status=404, error="board_not_found", checked_at=now)


_ICIMS_JOB_RE = re.compile(r'href="(https://[^"/]+\.icims\.com/jobs/\d+/[^"?]+/job)')


def vendor_identity(v, ats: str, code: str) -> dict | None:
    """The hiring organization a vendor states in its own job data, for vendors whose board
    API names no company. Needs at least one open job; None when there is nothing to read.

    Workday: CXS job detail `hiringOrganization`. iCIMS: JSON-LD `hiringOrganization` on a job page.
    """
    from company_dataset.careersite import organizations

    try:
        if ats == WORKDAY:
            resp = _post(v, ats, extra_api_url(WORKDAY, code), WORKDAY_QUERY)
            data = loads(resp.text) if resp.status == 200 else None
            posts = (data or {}).get("jobPostings") or []
            if not posts or not posts[0].get("externalPath"):
                return None
            url = extra_api_url(WORKDAY, code).rsplit("/jobs", 1)[0] + posts[0]["externalPath"]
            detail = loads(_get(v, ats, url).text) or {}
            org = detail.get("hiringOrganization") or {}
            return {"name": (org.get("name") or "").strip() or None, "url": org.get("url") or None, "evidence_url": url} if org.get("name") else None
        if ats == ICIMS:
            search = _get(v, ats, f"https://{code}.icims.com/jobs/search?ss=1&in_iframe=1")
            m = _ICIMS_JOB_RE.search(search.text or "") if search.status == 200 else None
            if not m:
                return None
            url = m.group(1) + "?in_iframe=1"
            page = _get(v, ats, url)
            for org in organizations(page.text if page.status == 200 else ""):
                if org["name"]:
                    return {"name": org["name"].strip(), "url": org["url"], "evidence_url": url}
            return None
    except _Unresolved:
        return None
    return None
