import json
import threading
from contextlib import contextmanager

import pytest

from conftest import FakeResponse, FakeSession

from company_dataset.config import resolve_dirs
from company_dataset.http import HttpClient
from company_dataset.models import Candidate
from company_dataset.normalize import BREEZY, SMARTRECRUITERS
from company_dataset.validate import SR_NO_SUCH_COMPANY, AtsRateLimiter, BoardValidator, run_validate, smartrecruiters_generic_page
from company_dataset.verification import INVALID_IDENTIFIER, classify

GH_JOBS_URL = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
GH_INFO_URL = "https://boards-api.greenhouse.io/v1/boards/acme"
LEVER_URL = "https://api.lever.co/v0/postings/acme?mode=json"
LEVER_EU_URL = "https://api.eu.lever.co/v0/postings/acme?mode=json"
ASHBY_URL = "https://api.ashbyhq.com/posting-api/job-board/acme"
SR_URL = "https://api.smartrecruiters.com/v1/companies/AbbVie/postings?limit=100"
SR_PAGE = "https://careers.smartrecruiters.com/AbbVie"
SR_GENERIC = "https://jobs.smartrecruiters.com"
BREEZY_URL = "https://testbox.breezy.hr/json"
BREEZY_PAGE = "https://testbox.breezy.hr"

BREEZY_TESTBOX_POSITION = {
    "company": {"name": "TestBox", "friendly_id": "testbox"},
    "friendly_id": "b3b8db09587f-core-experience-engineer-senior-software-engineer",
    "id": "b3b8db09587f",
    "name": "Core Experience Engineer - Senior Software Engineer",
    "url": "https://testbox.breezy.hr/p/b3b8db09587f-core-experience-engineer-senior-software-engineer",
}

BREEZY_404_HTML = (
    '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="robots" content="noindex">'
    "<title>Career portal not found | Breezy HR</title></head>"
    "<body><div class=\"badge\">404</div>"
    "<h1>There's no career portal here</h1></body></html>"
)


def make_client(script, sleeps=None):
    session = FakeSession(script)
    sleep = sleeps.append if sleeps is not None else (lambda s: None)
    client = HttpClient(session=session, sleep=sleep, per_host_interval=0.0, max_attempts=3)
    return client, session


def cand(ats, code, region=None, name=None):
    return Candidate(ats=ats, codes=[code], region=region, name=name, sources=["test"])


def test_greenhouse_valid_with_jobs_and_website():
    payload = {
        "jobs": [
            {"company_name": "Acme", "absolute_url": "https://acme.com/careers?gh_jid=1"},
            {"company_name": "Acme", "absolute_url": "https://acme.com/jobs?gh_jid=2"},
            {"company_name": "Acme", "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/3"},
        ],
        "meta": {"total": 3},
    }
    client, _ = make_client({
        GH_JOBS_URL: FakeResponse(200, json.dumps(payload), GH_JOBS_URL),
        GH_INFO_URL: FakeResponse(200, json.dumps({"name": "Acme", "content": ""}), GH_INFO_URL),
    })
    result = BoardValidator(client).validate_one(cand("greenhouse", "acme"))
    assert result.status == "valid"
    assert result.jobs_count == 3
    assert result.company_name == "Acme"
    assert result.website_url == "https://acme.com"
    assert result.description is None


def test_greenhouse_empty_jobs_still_valid_with_board_info_name():
    client, _ = make_client({
        GH_JOBS_URL: FakeResponse(200, json.dumps({"jobs": [], "meta": {"total": 0}})),
        GH_INFO_URL: FakeResponse(200, json.dumps({"name": "Acme", "content": "<p>We build things.</p>"})),
    })
    result = BoardValidator(client).validate_one(cand("greenhouse", "acme"))
    assert result.status == "valid"
    assert result.jobs_count == 0
    assert result.company_name == "Acme"
    assert result.description == "We build things."


def test_greenhouse_404_is_invalid():
    client, _ = make_client({GH_JOBS_URL: FakeResponse(404, "nope")})
    result = BoardValidator(client).validate_one(cand("greenhouse", "acme"))
    assert result.status == "invalid"
    assert result.http_status == 404


def test_greenhouse_board_info_failure_does_not_invalidate():
    client, _ = make_client({
        GH_JOBS_URL: FakeResponse(200, json.dumps({"jobs": [], "meta": {"total": 0}})),
        GH_INFO_URL: FakeResponse(404, "gone"),
    })
    result = BoardValidator(client).validate_one(cand("greenhouse", "acme"))
    assert result.status == "valid"
    assert result.company_name is None


def test_lever_country_unanimous():
    payload = [{"country": "US"}, {"country": "US"}, {"country": None}]
    client, _ = make_client({LEVER_URL: FakeResponse(200, json.dumps(payload))})
    result = BoardValidator(client).validate_one(cand("lever", "acme"))
    assert result.status == "valid"
    assert result.country == "US"
    assert result.country_basis == "lever_postings_unanimous"


def test_lever_mixed_countries_yields_no_country():
    payload = [{"country": "US"}, {"country": "GB"}]
    client, _ = make_client({LEVER_URL: FakeResponse(200, json.dumps(payload))})
    result = BoardValidator(client).validate_one(cand("lever", "acme"))
    assert result.status == "valid"
    assert result.country is None


def test_lever_empty_postings_is_valid():
    client, _ = make_client({LEVER_URL: FakeResponse(200, "[]")})
    result = BoardValidator(client).validate_one(cand("lever", "acme"))
    assert result.status == "valid"
    assert result.jobs_count == 0


def test_lever_falls_back_to_eu():
    client, session = make_client({
        LEVER_URL: FakeResponse(404, '{"ok":false,"error":"Document not found"}'),
        LEVER_EU_URL: FakeResponse(200, "[]"),
    })
    result = BoardValidator(client).validate_one(cand("lever", "acme"))
    assert result.status == "valid"
    assert result.region_answered == "eu"
    assert session.calls == [LEVER_URL, LEVER_EU_URL]


def test_lever_eu_candidate_tries_eu_first():
    client, session = make_client({
        LEVER_EU_URL: FakeResponse(200, "[]"),
        LEVER_URL: FakeResponse(404, ""),
    })
    result = BoardValidator(client).validate_one(cand("lever", "acme", region="eu"))
    assert result.status == "valid"
    assert result.region_answered == "eu"
    assert session.calls == [LEVER_EU_URL]


def test_lever_both_hosts_404_is_invalid():
    client, session = make_client({})
    result = BoardValidator(client).validate_one(cand("lever", "acme"))
    assert result.status == "invalid"
    assert session.calls == [LEVER_URL, LEVER_EU_URL]


def test_lever_case_variant_validates():
    client, _ = make_client({
        "https://api.lever.co/v0/postings/Palantir?mode=json": FakeResponse(404, ""),
        "https://api.eu.lever.co/v0/postings/Palantir?mode=json": FakeResponse(404, ""),
        "https://api.lever.co/v0/postings/palantir?mode=json": FakeResponse(200, "[]"),
    })
    c = Candidate(ats="lever", codes=["Palantir", "palantir"], sources=["test"])
    result = BoardValidator(client).validate_one(c)
    assert result.status == "valid"
    assert result.code_validated == "palantir"


def test_ashby_valid_with_mapped_country():
    payload = {"jobs": [
        {"address": {"postalAddress": {"addressCountry": "USA"}}},
        {"address": {"postalAddress": {"addressCountry": "United States"}}},
    ]}
    client, _ = make_client({ASHBY_URL: FakeResponse(200, json.dumps(payload))})
    result = BoardValidator(client).validate_one(cand("ashby", "acme"))
    assert result.status == "valid"
    assert result.country == "US"
    assert result.country_basis == "ashby_postings_unanimous"


def test_ashby_unmapped_country_vetoes():
    payload = {"jobs": [
        {"address": {"postalAddress": {"addressCountry": "USA"}}},
        {"address": {"postalAddress": {"addressCountry": "Ruritania"}}},
    ]}
    client, _ = make_client({ASHBY_URL: FakeResponse(200, json.dumps(payload))})
    result = BoardValidator(client).validate_one(cand("ashby", "acme"))
    assert result.status == "valid"
    assert result.country is None


def test_retries_on_429_then_valid():
    sleeps = []
    client, _ = make_client({LEVER_URL: [FakeResponse(429, "slow down"), FakeResponse(200, "[]")]}, sleeps)
    result = BoardValidator(client).validate_one(cand("lever", "acme"))
    assert result.status == "valid"
    assert len(sleeps) == 1


def test_connection_errors_exhausted_is_unresolved():
    client, _ = make_client({LEVER_URL: [RuntimeError("boom")] * 3})
    result = BoardValidator(client).validate_one(cand("lever", "acme"))
    assert result.status == "unresolved"
    assert result.error


def test_403_after_retries_is_unresolved_not_invalid():
    client, _ = make_client({ASHBY_URL: [FakeResponse(403, "blocked")] * 3})
    result = BoardValidator(client).validate_one(cand("ashby", "acme"))
    assert result.status == "unresolved"
    assert result.http_status == 403


def test_200_invalid_json_is_unresolved():
    client, _ = make_client({GH_JOBS_URL: FakeResponse(200, "<html>login</html>")})
    result = BoardValidator(client).validate_one(cand("greenhouse", "acme"))
    assert result.status == "unresolved"


def test_cache_skips_valid_and_invalid_but_retries_unresolved(tmp_path):
    cache_path = tmp_path / "validation.json"

    client1, _ = make_client({LEVER_URL: FakeResponse(404, "")})
    v1 = BoardValidator(client1, cache_path=cache_path)
    r1 = v1.validate_many([cand("lever", "acme")])
    assert r1["lever:acme"].status == "invalid"
    v1.flush()

    client2, session2 = make_client({})
    v2 = BoardValidator(client2, cache_path=cache_path)
    r2 = v2.validate_many([cand("lever", "acme")])
    assert len(session2.calls) == 0
    assert r2["lever:acme"].status == "invalid"

    client3, _ = make_client({ASHBY_URL: [RuntimeError("x")] * 3})
    v3 = BoardValidator(client3, cache_path=cache_path)
    v3.validate_many([cand("ashby", "acme")])
    v3.flush()

    client4, session4 = make_client({ASHBY_URL: FakeResponse(200, '{"jobs": []}')})
    v4 = BoardValidator(client4, cache_path=cache_path)
    r4 = v4.validate_many([cand("ashby", "acme")])
    assert len(session4.calls) > 0
    assert r4["ashby:acme"].status == "valid"


def test_validate_many_concurrent_correctness():
    script = {
        GH_JOBS_URL: FakeResponse(404, ""),
        GH_INFO_URL: FakeResponse(404, ""),
        LEVER_URL: FakeResponse(200, "[]"),
        ASHBY_URL: FakeResponse(200, '{"jobs": []}'),
    }
    client, _ = make_client(script)
    validator = BoardValidator(client, max_workers=3)
    results = validator.validate_many([
        cand("greenhouse", "acme"),
        cand("lever", "acme"),
        cand("ashby", "acme"),
    ])
    assert set(results) == {"greenhouse:acme", "lever:acme", "ashby:acme"}
    assert results["greenhouse:acme"].status == "invalid"
    assert results["lever:acme"].status == "valid"
    assert results["ashby:acme"].status == "valid"


def test_smartrecruiters_valid_uses_total_found_and_company_identifier():
    payload = {
        "offset": 0,
        "limit": 100,
        "totalFound": 1978,
        "content": [
            {"id": "1", "company": {"identifier": "AbbVie", "name": "AbbVie"}},
            {"id": "2", "company": {"identifier": "abbvie", "name": "AbbVie"}},
        ],
    }
    client, session = make_client({SR_URL: FakeResponse(200, json.dumps(payload))})
    result = BoardValidator(client).validate_one(cand("smartrecruiters", "AbbVie"))
    assert result.status == "valid"
    assert result.jobs_count == 1978
    assert result.company_identifier == "AbbVie"
    assert result.company_name == "AbbVie"
    assert session.calls == [SR_URL]


SR_ZERO = {"offset": 0, "limit": 100, "totalFound": 0, "content": []}


def test_smartrecruiters_zero_jobs_with_own_careers_page_is_live():
    client, _ = make_client({SR_URL: FakeResponse(200, json.dumps(SR_ZERO)), SR_PAGE: FakeResponse(200, "<title>Careers at AbbVie</title>", SR_PAGE)})
    result = BoardValidator(client).validate_one(cand("smartrecruiters", "AbbVie"))
    assert result.status == "valid"
    assert result.error == "total_found_zero"
    assert result.jobs_count == 0
    assert result.http_status == 200


def test_smartrecruiters_zero_jobs_redirecting_to_a_company_site_is_live():
    # A company can send its careers page to its own site, which only an existing account can configure.
    client, _ = make_client({SR_URL: FakeResponse(200, json.dumps(SR_ZERO)), SR_PAGE: FakeResponse(200, "", "https://careers.abbvie.com/en")})
    assert BoardValidator(client).validate_one(cand("smartrecruiters", "AbbVie")).status == "valid"


def test_smartrecruiters_zero_jobs_on_generic_search_page_does_not_exist():
    # The postings API answers 200 totalFound=0 for any identifier, invented ones included.
    client, _ = make_client({SR_URL: FakeResponse(200, json.dumps(SR_ZERO)), SR_PAGE: FakeResponse(200, "<title>SmartRecruiters Job Search</title>", SR_GENERIC + "/")})
    result = BoardValidator(client).validate_one(cand("smartrecruiters", "AbbVie"))
    assert result.status == "invalid"
    assert result.error == SR_NO_SUCH_COMPANY
    assert classify(result.status, result.http_status, result.error, result.jobs_count) == INVALID_IDENTIFIER


def test_smartrecruiters_zero_jobs_with_unreachable_page_is_not_live():
    client, _ = make_client({SR_URL: FakeResponse(200, json.dumps(SR_ZERO)), SR_PAGE: [FakeResponse(503, "")] * 3})
    result = BoardValidator(client).validate_one(cand("smartrecruiters", "AbbVie"))
    assert result.status == "unresolved"
    assert result.http_status == 503


@pytest.mark.parametrize("url,generic", [
    ("https://jobs.smartrecruiters.com", True),
    ("https://jobs.smartrecruiters.com/", True),
    ("https://careers.smartrecruiters.com/AbbVie", False),
    ("https://careers.smartrecruiters.com/DeloitteAT/deloitte-jobs", False),
    ("https://jobs.smartrecruiters.com/AbbVie/123-engineer", False),
    ("https://careers.accor.com/", False),
    ("", False),
])
def test_smartrecruiters_generic_page(url, generic):
    assert smartrecruiters_generic_page(url) is generic


def test_smartrecruiters_missing_company_identifier_is_unresolved():
    payload = {
        "totalFound": 2,
        "content": [
            {"company": {"name": "AbbVie"}},
            {"company": {"identifier": "AbbVie", "name": "AbbVie"}},
        ],
    }
    client, _ = make_client({SR_URL: FakeResponse(200, json.dumps(payload))})
    result = BoardValidator(client).validate_one(cand("smartrecruiters", "AbbVie"))
    assert result.status == "unresolved"
    assert result.error == "missing_company_identifier"


def test_smartrecruiters_company_identifier_mismatch_is_unresolved():
    payload = {
        "totalFound": 2,
        "content": [
            {"company": {"identifier": "Other", "name": "Other"}},
            {"company": {"identifier": "AbbVie", "name": "AbbVie"}},
        ],
    }
    client, _ = make_client({SR_URL: FakeResponse(200, json.dumps(payload))})
    result = BoardValidator(client).validate_one(cand("smartrecruiters", "AbbVie"))
    assert result.status == "unresolved"
    assert result.error == "company_identifier_mismatch"


def test_smartrecruiters_404_is_invalid():
    client, _ = make_client({SR_URL: FakeResponse(404, "")})
    result = BoardValidator(client).validate_one(cand("smartrecruiters", "AbbVie"))
    assert result.status == "invalid"
    assert result.http_status == 404


def test_smartrecruiters_bad_schema_is_unresolved():
    client, _ = make_client({SR_URL: FakeResponse(200, json.dumps({"totalFound": 1}))})
    result = BoardValidator(client).validate_one(cand("smartrecruiters", "AbbVie"))
    assert result.status == "unresolved"
    assert result.error == "unexpected_schema"


def test_smartrecruiters_429_is_retried_then_unresolved():
    client, session = make_client({SR_URL: [FakeResponse(429, "slow down")] * 3})
    result = BoardValidator(client).validate_one(cand("smartrecruiters", "AbbVie"))
    assert result.status == "unresolved"
    assert result.http_status == 429
    assert result.error == "http_429"
    assert session.calls == [SR_URL, SR_URL, SR_URL]


def test_smartrecruiters_500_is_retried_then_unresolved():
    client, session = make_client({SR_URL: [FakeResponse(500, "server error")] * 3})
    result = BoardValidator(client).validate_one(cand("smartrecruiters", "AbbVie"))
    assert result.status == "unresolved"
    assert result.http_status == 500
    assert result.error == "http_500"
    assert session.calls == [SR_URL, SR_URL, SR_URL]


def test_smartrecruiters_invalid_json_is_unresolved():
    client, session = make_client({SR_URL: FakeResponse(200, "<html>login</html>")})
    result = BoardValidator(client).validate_one(cand("smartrecruiters", "AbbVie"))
    assert result.status == "unresolved"
    assert result.http_status == 200
    assert result.error == "invalid_json"
    assert session.calls == [SR_URL]


def test_smartrecruiters_requests_only_first_page():
    payload = {
        "totalFound": 250,
        "content": [
            {"company": {"identifier": "AbbVie", "name": "AbbVie"}}
            for _ in range(100)
        ],
    }
    client, session = make_client({SR_URL: FakeResponse(200, json.dumps(payload))})
    result = BoardValidator(client).validate_one(cand("smartrecruiters", "AbbVie"))
    assert result.status == "valid"
    assert result.jobs_count == 250
    assert session.calls == [SR_URL]


def test_breezy_testbox_non_empty_is_valid_from_schema():
    client, session = make_client({BREEZY_URL: FakeResponse(200, json.dumps([BREEZY_TESTBOX_POSITION]))})
    result = BoardValidator(client).validate_one(cand("breezy", "testbox", name="TestBox"))
    assert result.status == "valid"
    assert result.jobs_count == 1
    assert result.company_identifier == "testbox"
    assert result.company_name == "TestBox"
    assert session.calls == [BREEZY_URL]


def test_breezy_non_empty_without_company_friendly_id_is_valid_with_blank_identifier():
    position = {"company": {"name": "TestBox"}, "friendly_id": "job-slug"}
    client, session = make_client({BREEZY_URL: FakeResponse(200, json.dumps([position]))})
    result = BoardValidator(client).validate_one(cand("breezy", "testbox", name="TestBox"))
    assert result.status == "valid"
    assert result.company_identifier is None
    assert result.company_name == "TestBox"
    assert session.calls == [BREEZY_URL]


def test_breezy_non_empty_company_friendly_id_match_is_valid():
    position = {"company": {"name": "TestBox", "friendly_id": "TestBox"}, "friendly_id": "job-slug"}
    client, session = make_client({BREEZY_URL: FakeResponse(200, json.dumps([position]))})
    result = BoardValidator(client).validate_one(cand("breezy", "testbox", name="TestBox"))
    assert result.status == "valid"
    assert result.company_identifier == "TestBox"
    assert session.calls == [BREEZY_URL]


def test_breezy_non_empty_company_friendly_id_mismatch_is_unresolved():
    position = {"company": {"name": "Other", "friendly_id": "other"}, "friendly_id": "job-slug"}
    client, session = make_client({BREEZY_URL: FakeResponse(200, json.dumps([position]))})
    result = BoardValidator(client).validate_one(cand("breezy", "testbox", name="TestBox"))
    assert result.status == "unresolved"
    assert result.error == "company_identifier_mismatch"
    assert result.http_status == 200
    assert result.jobs_count == 1
    assert result.company_identifier == "other"
    assert session.calls == [BREEZY_URL]


def test_breezy_non_empty_plain_company_string_is_valid():
    position = {"company": "TestBox", "friendly_id": "job-slug"}
    client, _ = make_client({BREEZY_URL: FakeResponse(200, json.dumps([position]))})
    result = BoardValidator(client).validate_one(cand("breezy", "testbox", name="TestBox"))
    assert result.status == "valid"
    assert result.company_identifier is None
    assert result.company_name == "TestBox"


def test_breezy_404_with_career_portal_body_is_invalid():
    client, session = make_client({BREEZY_URL: FakeResponse(404, BREEZY_404_HTML)})
    result = BoardValidator(client).validate_one(cand("breezy", "testbox"))
    assert result.status == "invalid"
    assert result.http_status == 404
    assert result.error == "board_not_found"
    assert session.calls == [BREEZY_URL]


def test_breezy_404_with_unrecognized_body_is_unresolved():
    client, session = make_client({BREEZY_URL: FakeResponse(404, "<html><body>Not found</body></html>")})
    result = BoardValidator(client).validate_one(cand("breezy", "testbox"))
    assert result.status == "unresolved"
    assert result.http_status == 404
    assert result.error == "http_404_unrecognized"
    assert session.calls == [BREEZY_URL]


def test_breezy_empty_with_hosted_page_identity_is_valid():
    page = "<html><head><title>TestBox</title></head></html>"
    client, session = make_client({
        BREEZY_URL: FakeResponse(200, "[]"),
        BREEZY_PAGE: FakeResponse(200, page),
    })
    result = BoardValidator(client).validate_one(cand("breezy", "testbox", name="TestBox"))
    assert result.status == "valid"
    assert result.jobs_count == 0
    assert result.company_identifier is None
    assert result.company_name == "TestBox"
    assert session.calls == [BREEZY_URL, BREEZY_PAGE]


def test_breezy_empty_with_generic_hosted_page_is_unresolved():
    page = "<html><head><title>Breezy HR</title></head></html>"
    client, _ = make_client({
        BREEZY_URL: FakeResponse(200, "[]"),
        BREEZY_PAGE: FakeResponse(200, page),
    })
    result = BoardValidator(client).validate_one(cand("breezy", "testbox", name="TestBox"))
    assert result.status == "unresolved"
    assert result.error == "exists_no_identity"
    assert result.jobs_count == 0
    assert result.http_status == 200


def test_breezy_empty_without_hosted_page_identity_is_unresolved():
    client, _ = make_client({
        BREEZY_URL: FakeResponse(200, "[]"),
        BREEZY_PAGE: FakeResponse(200, "<html><head></head></html>"),
    })
    result = BoardValidator(client).validate_one(cand("breezy", "testbox", name="TestBox"))
    assert result.status == "unresolved"
    assert result.error == "exists_no_identity"


def test_breezy_empty_hosted_page_404_is_unresolved():
    client, _ = make_client({
        BREEZY_URL: FakeResponse(200, "[]"),
        BREEZY_PAGE: FakeResponse(404, ""),
    })
    result = BoardValidator(client).validate_one(cand("breezy", "testbox", name="TestBox"))
    assert result.status == "unresolved"
    assert result.error == "hosted_page_http_404"


def test_breezy_non_list_schema_is_unresolved():
    client, _ = make_client({BREEZY_URL: FakeResponse(200, json.dumps({"jobs": []}))})
    result = BoardValidator(client).validate_one(cand("breezy", "testbox"))
    assert result.status == "unresolved"
    assert result.error == "unexpected_schema"


class RecordingLimiter:
    def __init__(self):
        self.ats = []

    @contextmanager
    def slot(self, ats):
        self.ats.append(ats)
        yield


def test_smartrecruiters_and_breezy_use_group_rate_limiter():
    limiter = RecordingLimiter()
    client, _ = make_client({
        SR_URL: FakeResponse(200, json.dumps({"totalFound": 1, "content": [{"company": {"identifier": "AbbVie", "name": "AbbVie"}}]})),
        BREEZY_URL: FakeResponse(200, json.dumps([BREEZY_TESTBOX_POSITION])),
    })
    validator = BoardValidator(client, rate_limiter=limiter)
    validator.validate_one(cand("smartrecruiters", "AbbVie"))
    validator.validate_one(cand("breezy", "testbox", name="TestBox"))
    assert limiter.ats == [SMARTRECRUITERS, BREEZY]


def test_empty_breezy_uses_group_rate_limiter_for_json_and_page():
    limiter = RecordingLimiter()
    client, _ = make_client({
        BREEZY_URL: FakeResponse(200, "[]"),
        BREEZY_PAGE: FakeResponse(200, "<html><head><title>TestBox</title></head></html>"),
    })
    BoardValidator(client, rate_limiter=limiter).validate_one(cand("breezy", "testbox", name="TestBox"))
    assert limiter.ats == [BREEZY, BREEZY]


def test_ats_rate_limiter_waits_between_group_requests():
    clock = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    limiter = AtsRateLimiter(
        {BREEZY: {"max_concurrent": 2, "delay_seconds": 0.5}},
        sleep=sleep,
        clock=lambda: clock[0],
    )
    with limiter.slot(BREEZY):
        pass
    with limiter.slot(BREEZY):
        pass
    assert sleeps == [pytest.approx(0.5)]


def test_ats_rate_limiter_caps_group_concurrency():
    limiter = AtsRateLimiter({BREEZY: {"max_concurrent": 1, "delay_seconds": 0}})
    entered = threading.Event()
    release = threading.Event()

    def hold():
        with limiter.slot(BREEZY):
            entered.set()
            release.wait(2)

    first = threading.Thread(target=hold)
    first.start()
    assert entered.wait(2)

    second_entered = threading.Event()

    def second():
        with limiter.slot(BREEZY):
            second_entered.set()

    other = threading.Thread(target=second)
    other.start()
    assert not second_entered.wait(0.1)
    release.set()
    assert second_entered.wait(2)
    first.join(2)
    other.join(2)


def test_ats_rate_limiter_ignores_unconfigured_ats():
    sleeps = []
    limiter = AtsRateLimiter({BREEZY: {"max_concurrent": 1, "delay_seconds": 1}}, sleep=sleeps.append)
    with limiter.slot(SMARTRECRUITERS):
        pass
    assert sleeps == []


def test_run_validate_filters_ats_and_records_unresolved_reasons(tmp_path):
    dirs = resolve_dirs(tmp_path)
    (dirs["intermediate"] / "candidates.csv").write_text(
        "ats,code,alt_codes,region,name,name_source,careersite_urls,sources\n"
        "breezy,testbox,,,,,,\n"
        "smartrecruiters,AbbVie,,,,,,\n"
        "lever,acme,,,,,,\n",
        encoding="utf-8",
    )
    client, session = make_client({
        BREEZY_URL: FakeResponse(200, "[]"),
        BREEZY_PAGE: FakeResponse(200, "<html><head><title>Breezy HR</title></head></html>"),
        SR_URL: FakeResponse(200, json.dumps({"totalFound": 0, "content": []})),
        SR_PAGE: FakeResponse(200, "<title>Careers at AbbVie</title>", SR_PAGE),
    })
    stats = run_validate(
        dirs,
        client=client,
        ats={"breezy", "smartrecruiters"},
        rate_limiter=AtsRateLimiter({}),
    )
    assert stats["candidates_total"] == 2
    assert stats["checked"] == 2
    assert stats["by_ats"] == {
        "breezy": {"unresolved": 1},
        "smartrecruiters": {"valid": 1},
    }
    assert stats["unresolved_reasons"] == {
        "breezy": {"exists_no_identity": 1},
    }
    assert not any("lever" in url for url in session.calls)
    saved = json.loads((dirs["intermediate"] / "validation_stats.json").read_text(encoding="utf-8"))
    assert saved["unresolved_reasons"] == stats["unresolved_reasons"]


def test_validation_stats_reasons_are_per_ats_and_include_all_four(tmp_path):
    dirs = resolve_dirs(tmp_path)
    (dirs["intermediate"] / "candidates.csv").write_text(
        "ats,code,alt_codes,region,name,name_source,careersite_urls,sources\n"
        "smartrecruiters,ZeroCo,,,,,,\n"
        "smartrecruiters,MismatchCo,,,,,,\n"
        "breezy,emptyco,,,,,,\n"
        "breezy,missingco,,,,,,\n",
        encoding="utf-8",
    )
    sr_zero_url = "https://api.smartrecruiters.com/v1/companies/ZeroCo/postings?limit=100"
    sr_mismatch_url = "https://api.smartrecruiters.com/v1/companies/MismatchCo/postings?limit=100"
    breezy_empty_url = "https://emptyco.breezy.hr/json"
    breezy_empty_page = "https://emptyco.breezy.hr"
    breezy_missing_url = "https://missingco.breezy.hr/json"
    client, _ = make_client({
        sr_zero_url: FakeResponse(200, json.dumps({"totalFound": 0, "content": []})),
        sr_mismatch_url: FakeResponse(200, json.dumps({
            "totalFound": 1,
            "content": [{"company": {"identifier": "Other", "name": "Other"}}],
        })),
        breezy_empty_url: FakeResponse(200, "[]"),
        breezy_empty_page: FakeResponse(200, "<html><head><title>Breezy HR</title></head></html>"),
        breezy_missing_url: FakeResponse(404, "<html><body>Not found</body></html>"),
    })
    stats = run_validate(
        dirs,
        client=client,
        ats={"breezy", "smartrecruiters"},
        rate_limiter=AtsRateLimiter({}),
    )
    assert stats["unresolved_reasons"] == {
        "breezy": {
            "exists_no_identity": 1,
            "http_404_unrecognized": 1,
        },
        "smartrecruiters": {
            "company_identifier_mismatch": 1,
        },
    }
    saved = json.loads((dirs["intermediate"] / "validation_stats.json").read_text(encoding="utf-8"))
    assert saved["unresolved_reasons"] == stats["unresolved_reasons"]


def test_cli_ats_filter_is_passed(monkeypatch):
    import company_dataset.legacy.cli as cli

    captured = {}
    monkeypatch.setattr(cli, "resolve_dirs", lambda data_dir: {})
    monkeypatch.setattr(cli, "run_validate", lambda dirs, **kwargs: captured.update(kwargs))
    assert cli.main(["validate", "--ats", "breezy,smartrecruiters"]) == 0
    assert captured["ats"] == {"breezy", "smartrecruiters"}


def test_cli_rate_limit_overrides_are_passed(monkeypatch):
    import company_dataset.legacy.cli as cli

    for var in (
        "COMPANY_DATASET_SMARTRECRUITERS_MAX_CONCURRENT",
        "COMPANY_DATASET_SMARTRECRUITERS_DELAY_SECONDS",
        "COMPANY_DATASET_BREEZY_MAX_CONCURRENT",
        "COMPANY_DATASET_BREEZY_DELAY_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)
    captured = {}
    monkeypatch.setattr(cli, "resolve_dirs", lambda data_dir: {})
    monkeypatch.setattr(cli, "run_validate", lambda dirs, **kwargs: captured.update(kwargs))
    assert cli.main([
        "validate",
        "--smartrecruiters-max-concurrent", "1",
        "--smartrecruiters-delay-seconds", "0.25",
        "--breezy-max-concurrent", "3",
        "--breezy-delay-seconds", "1.5",
    ]) == 0
    limits = captured["rate_limits"]
    assert limits["smartrecruiters"] == {"max_concurrent": 1, "delay_seconds": 0.25}
    assert limits["breezy"] == {"max_concurrent": 3, "delay_seconds": 1.5}
    assert limits["workable"] == {"max_concurrent": 1, "delay_seconds": 0.7}


def test_cli_rejects_unknown_ats(monkeypatch):
    import company_dataset.legacy.cli as cli

    monkeypatch.setattr(cli, "resolve_dirs", lambda data_dir: {})
    with pytest.raises(SystemExit):
        cli.main(["validate", "--ats", "jazzhr"])
