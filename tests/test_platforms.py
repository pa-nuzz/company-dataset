import json

import pytest

from conftest import FakeResponse, FakeSession

from company_dataset.http import HttpClient
from company_dataset.models import Candidate
from company_dataset.normalize import canonical_api_url, canonical_board_url, parse_board_url
from company_dataset.validate import BoardValidator


def client(script):
    session = FakeSession(script)
    return HttpClient(session=session, sleep=lambda s: None, per_host_interval=0.0, max_attempts=2), session


def run(ats, code, script, region=None, name=None):
    cl, session = client(script)
    return BoardValidator(cl).validate_one(Candidate(ats=ats, codes=[code], region=region, name=name, sources=["t"])), session


@pytest.mark.parametrize("url,expected", [
    ("https://apply.workable.com/Botpress/", ("workable", "botpress", None)),
    ("https://apply.workable.com/botpress/j/ABC123/", ("workable", "botpress", None)),
    ("https://botpress.workable.com/", ("workable", "botpress", None)),
    ("https://3m.wd1.myworkdayjobs.com/en-US/Search/job/x", ("workday", "3m.wd1/Search", None)),
    ("https://ATT.WD1.myworkdayjobs.com/ATTGeneral", ("workday", "att.wd1/ATTGeneral", None)),
    ("https://wd3.myworkdaysite.com/recruiting/acme/External", ("workday", "acme.wd3/External", None)),
    ("https://pactum.bamboohr.com/careers/21", ("bamboohr", "pactum", None)),
    ("https://Climeworks.recruitee.com/o/engineer", ("recruitee", "climeworks", None)),
    ("https://buhlmann.teamtailor.com/jobs", ("teamtailor", "buhlmann", None)),
    ("https://pec.jobs.personio.de/job/1", ("personio", "pec", None)),
    ("https://acme.jobs.personio.com/", ("personio", "acme", "com")),
    ("https://ats.rippling.com/en-GB/prowler/jobs", ("rippling", "prowler", None)),
    ("https://careers-360care.icims.com/jobs/search", ("icims", "careers-360care", None)),
    ("https://accenture.pinpointhq.com/en/postings/1", ("pinpoint", "accenture", None)),
    ("https://join.com/companies/01informatica/123-dev", ("join", "01informatica", None)),
])
def test_extra_platform_urls_parse(url, expected):
    pb = parse_board_url(url)
    assert (pb.ats, pb.code, pb.region) == expected


@pytest.mark.parametrize("url", [
    "https://www.bamboohr.com/careers",
    "https://apply.workable.com/api/v1/widget/accounts/x",
    "https://wd1.myworkdayjobs.com/",
    "https://3m.wd1.myworkdayjobs.com/",
    "https://3m.wd1.myworkdayjobs.com/wday/cxs/3m/Search/jobs",
    "https://join.com/jobs",
    "https://www.icims.com/careers",
    "https://jobs.jobvite.com/acme",
])
def test_non_board_urls_on_extra_platforms_rejected(url):
    assert parse_board_url(url) is None


def test_extra_canonical_urls_built_from_code_only():
    assert canonical_api_url("workday", "3m.wd1/Search") == "https://3m.wd1.myworkdayjobs.com/wday/cxs/3m/Search/jobs"
    assert canonical_board_url("workday", "3m.wd1/Search") == "https://3m.wd1.myworkdayjobs.com/Search"
    assert canonical_api_url("workable", "botpress") == "https://apply.workable.com/api/v1/widget/accounts/botpress"
    assert canonical_api_url("personio", "acme", "com") == "https://acme.jobs.personio.com/xml"


def test_workable_valid_has_name_description_jobs():
    url = "https://apply.workable.com/api/v1/widget/accounts/botpress"
    body = {"name": "Botpress Technologies Inc.", "description": "<p>AI agents.</p>", "jobs": [{"title": "a"}, {"title": "b"}]}
    r, _ = run("workable", "botpress", {url: FakeResponse(200, json.dumps(body), url)})
    assert (r.status, r.jobs_count, r.company_name, r.description) == ("valid", 2, "Botpress Technologies Inc.", "AI agents.")


def test_workable_404_is_invalid_and_429_unresolved():
    url = "https://apply.workable.com/api/v1/widget/accounts/nope"
    assert run("workable", "nope", {url: FakeResponse(404, "Not Found", url)})[0].status == "invalid"
    r, _ = run("workable", "nope", {url: [FakeResponse(429, "", url), FakeResponse(429, "", url)]})
    assert (r.status, r.error) == ("unresolved", "http_429")


def test_workday_post_total_and_422_invalid():
    api = "POST https://3m.wd1.myworkdayjobs.com/wday/cxs/3m/Search/jobs"
    r, _ = run("workday", "3m.wd1/Search", {api: FakeResponse(200, json.dumps({"total": 86, "jobPostings": []}))})
    assert (r.status, r.jobs_count) == ("valid", 86)
    r, _ = run("workday", "3m.wd1/Search", {api: FakeResponse(422, '{"errorCode":"HTTP_422"}')})
    assert (r.status, r.error) == ("invalid", "board_not_found")


def test_bamboohr_redirect_off_host_is_invalid_and_name_from_company_info():
    lst = "https://pactum.bamboohr.com/careers/list"
    info = "https://pactum.bamboohr.com/careers/company-info"
    r, _ = run("bamboohr", "pactum", {
        lst: FakeResponse(200, json.dumps({"meta": {"totalCount": 1}, "result": [{"id": "21"}]}), lst),
        info: FakeResponse(200, json.dumps({"result": {"name": "Pactum"}}), info),
    })
    assert (r.status, r.jobs_count, r.company_name) == ("valid", 1, "Pactum")
    gone = "https://ghost.bamboohr.com/careers/list"
    r, _ = run("bamboohr", "ghost", {gone: FakeResponse(200, "<html>BambooHR</html>", "https://www.bamboohr.com/")})
    assert r.status == "invalid"


def test_recruitee_name_only_when_offers_agree():
    url = "https://climeworks.recruitee.com/api/offers/"
    body = {"offers": [{"company_name": "Climeworks"}, {"company_name": "Climeworks"}]}
    r, _ = run("recruitee", "climeworks", {url: FakeResponse(200, json.dumps(body), url)})
    assert (r.status, r.jobs_count, r.company_name) == ("valid", 2, "Climeworks")
    r, _ = run("recruitee", "climeworks", {url: FakeResponse(200, json.dumps({"offers": []}), url)})
    assert (r.status, r.jobs_count, r.company_name) == ("valid", 0, None)


def test_teamtailor_title_is_company_name():
    url = "https://buhlmann.teamtailor.com/jobs.json"
    body = {"title": "Bühlmann Hotels", "items": [{"id": "1"}]}
    r, _ = run("teamtailor", "buhlmann", {url: FakeResponse(200, json.dumps(body), url)})
    assert (r.status, r.jobs_count, r.company_name) == ("valid", 1, "Bühlmann Hotels")


def test_personio_tries_other_tld_and_reads_page_title():
    de, com = "https://pec.jobs.personio.de/xml", "https://pec.jobs.personio.com/xml"
    page = "https://pec.jobs.personio.com/"
    r, s = run("personio", "pec", {
        de: FakeResponse(404, "", de),
        com: FakeResponse(200, "<workzag-jobs><position></position><position></position></workzag-jobs>", com),
        page: FakeResponse(200, "<title>Jobs bei PEC</title>", page),
    })
    assert (r.status, r.jobs_count, r.company_name, r.region_answered) == ("valid", 2, "PEC", "com")
    assert s.calls[:2] == [de, com]


def test_rippling_404_message_is_invalid():
    url = "https://api.rippling.com/platform/api/ats/v1/board/ghost/jobs"
    r, _ = run("rippling", "ghost", {url: FakeResponse(404, '{"error_code":"RESOURCE_NOT_FOUND","message":"Job Board not found"}', url)})
    assert r.status == "invalid"


def test_icims_counts_jobs_and_reads_title():
    url = "https://careers-360care.icims.com/jobs/search?ss=1&in_iframe=1"
    body = '<title>360care Careers | Available job openings</title><a href="https://careers-360care.icims.com/jobs/101/x/job"></a><a href="https://careers-360care.icims.com/jobs/102/y/job"></a>'
    r, _ = run("icims", "careers-360care", {url: FakeResponse(200, body, url)})
    assert (r.status, r.jobs_count, r.company_name) == ("valid", 2, "360care")


def test_join_requires_company_title():
    url = "https://join.com/companies/acme"
    page = '<meta property="og:title" content="Jobs at Acme GmbH | JOIN"/><a href="/companies/acme/123-dev"></a>'
    r, _ = run("join", "acme", {url: FakeResponse(200, page, url)})
    assert (r.status, r.jobs_count, r.company_name) == ("valid", 1, "Acme GmbH")
    r, _ = run("join", "acme", {url: FakeResponse(200, "<title>JOIN</title>", url)})
    assert (r.status, r.error) == ("unresolved", "exists_no_identity")


def test_pinpoint_valid_and_invalid():
    url = "https://accenture.pinpointhq.com/postings.json"
    assert run("pinpoint", "accenture", {url: FakeResponse(200, json.dumps({"data": [{}, {}]}), url)})[0].jobs_count == 2
    assert run("pinpoint", "accenture", {url: FakeResponse(404, "", url)})[0].status == "invalid"


def test_vendor_identity_reads_workday_and_icims_hiring_organization():
    import json as _json

    from company_dataset.http import HttpClient
    from company_dataset.validate import BoardValidator
    from company_dataset.validate_extra import vendor_identity
    from conftest import FakeResponse, FakeSession

    wd_jobs = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/careers/jobs"
    wd_job = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/careers/job/X_R1"
    icims_search = "https://careers-acme.icims.com/jobs/search?ss=1&in_iframe=1"
    icims_job = "https://careers-acme.icims.com/jobs/12/engineer/job?in_iframe=1"
    session = FakeSession({
        "POST " + wd_jobs: FakeResponse(200, _json.dumps({"total": 1, "jobPostings": [{"externalPath": "/job/X_R1"}]})),
        wd_job: FakeResponse(200, _json.dumps({"hiringOrganization": {"name": "Acme College", "url": ""}})),
        icims_search: FakeResponse(200, '<a href="https://careers-acme.icims.com/jobs/12/engineer/job?x=1">x</a>'),
        icims_job: FakeResponse(200, '<script type="application/ld+json">{"@type":"JobPosting","hiringOrganization":{"@type":"Organization","name":"Acme Rx"}}</script>'),
    })
    v = BoardValidator(HttpClient(session=session, sleep=lambda s: None, max_attempts=1))
    assert vendor_identity(v, "workday", "acme.wd1/careers")["name"] == "Acme College"
    assert vendor_identity(v, "icims", "careers-acme")["name"] == "Acme Rx"
    # No open jobs: nothing to read, no name invented.
    empty = BoardValidator(HttpClient(session=FakeSession({"POST " + wd_jobs: FakeResponse(200, '{"total":0,"jobPostings":[]}')}), sleep=lambda s: None, max_attempts=1))
    assert vendor_identity(empty, "workday", "acme.wd1/careers") is None
