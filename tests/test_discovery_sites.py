import json

import pytest

from conftest import FakeResponse, FakeSession

from company_dataset.http import HttpClient
from company_dataset.sources import SOURCES, _harvest, arquivo_cdx as aq, company_sites as cs


def client(script=None):
    return HttpClient(session=FakeSession(script or {}), sleep=lambda s: None, per_host_interval=0, max_attempts=1)


@pytest.mark.parametrize("url,code", [
    ("https://jobs.lever.co/043a89e8f0d2453a8f36f66042f722f3", None),
    ("https://jobs.lever.co/3.0.7", None),
    ("https://jobs.ashbyhq.com/cac67a25-d7c2-44f4-91ab-280a5dc5b96d", None),
    ("https://boards.greenhouse.io/2", None),
    ("https://jobs.lever.co/llms.txt", None),
    ("https://jobs.lever.co/robots.txt", None),
    ("https://jobs.lever.co/acme", "acme"),
    ("https://careers.smartrecruiters.com/1871", "1871"),
    ("https://jobs.ashbyhq.com/ether.fi", "ether.fi"),
])
def test_identifiers_that_cannot_be_boards_are_dropped(url, code):
    board = _harvest.board_from_url(url)
    assert (board[1] if board else None) == code


@pytest.mark.parametrize("html,expected", [
    ("<iframe src='https://boards.greenhouse.io/embed/job_board?for=acme'></iframe>", ("greenhouse", "acme")),
    ("<script src='https://boards.greenhouse.io/embed/job_board/js?for=acme'></script>", ("greenhouse", "acme")),
    ("<a href='https://jobs.lever.co/acme'>Jobs</a>", ("lever", "acme")),
    ("<script src='https://jobs.ashbyhq.com/acme/embed'></script>", ("ashby", "acme")),
    ("<a href='https://acme.breezy.hr'>Jobs</a>", ("breezy", "acme")),
])
def test_site_page_finds_links_and_embeds(html, expected):
    c = client({"https://acme.com/careers": FakeResponse(200, html, "https://acme.com/careers")})
    assert expected in cs.boards_on_site(c, "https://acme.com")


def test_loose_query_parameters_are_not_read_as_boards():
    html = "<a href='/apply?for=research-analysts'>x</a>"
    assert cs.boards_on_site(client({"https://acme.com/careers": FakeResponse(200, html)}), "https://acme.com") == {}


def test_site_stops_at_the_first_page_with_a_board_and_obeys_robots():
    c = client({"https://a.com/robots.txt": FakeResponse(200, "User-agent: *\nDisallow: /careers\n"),
                "https://a.com/careers": FakeResponse(200, "https://jobs.lever.co/hidden"),
                "https://a.com/jobs": FakeResponse(200, "https://jobs.lever.co/visible")})
    assert list(cs.boards_on_site(c, "https://a.com")) == [("lever", "visible")]
    assert "https://a.com/careers" not in c.session.calls and "https://a.com" not in c.session.calls


def test_harvest_resumes_and_keeps_no_names(tmp_path):
    c = client({"https://a.com/careers": FakeResponse(200, "https://jobs.lever.co/acme")})
    assert cs.harvest(tmp_path, ["https://a.com"], c, workers=1) == {"sites_checked": 1, "boards": 1}
    again = client()
    assert cs.harvest(tmp_path, ["https://a.com"], again, workers=1)["boards"] == 1
    assert again.session.calls == []
    records, _ = cs.parse(tmp_path / cs.META.name / "tokens.csv")
    assert records[0].name is None


def test_plan_sites_dedupes_and_drops_invalid():
    assert cs.plan_sites([("A", "https://www.a.com/x"), ("A2", "www.a.com"), ("B", "not a url"), ("C", "c.io")]) == ["https://www.a.com", "https://c.io"]


def test_arquivo_pages_until_a_short_page(tmp_path, monkeypatch):
    monkeypatch.setattr(aq, "PAGE_SIZE", 2)
    target = "jobs.lever.co/*"
    row = lambda u: json.dumps({"url": u})
    c = client({aq.query(target, 0): FakeResponse(200, "\n".join([row("https://jobs.lever.co/acme/1"), row("https://jobs.lever.co/beta")])),
                aq.query(target, 1): FakeResponse(200, row("https://jobs.lever.co/gamma"))})
    assert aq.harvest(tmp_path, c, targets=[target]) == {"boards": 3, "failed": []}


def test_sources_registered():
    names = {m.META.name for m in SOURCES}
    assert {"company_sites", "arquivo_cdx"} <= names


def test_simplify_listings_are_seed_files():
    from company_dataset.sources.github_lists import SEEDS

    assert any("New-Grad-Positions" in s and s.endswith("listings.json") for s in SEEDS)
    assert any("Summer2026-Internships" in s and s.endswith("listings.json") for s in SEEDS)


def test_stopped_wayback_harvest_can_still_produce_tokens(tmp_path):
    from company_dataset.sources import wayback_cdx as wb

    out = tmp_path / wb.META.name
    out.mkdir()
    (out / "pages.jsonl").write_text(json.dumps({"target": "t", "page": 0, "tokens": [["lever", "acme", "", "u1"], ["lever", "ACME", "", "u2"]]}) + "\n" + json.dumps({"target": "t", "page": 1, "tokens": [["ashby", "beta", "", "u3"]]}) + "\n")
    assert wb.rebuild_tokens(tmp_path) == 2
    records, _ = wb.parse(out / "tokens.csv")
    assert sorted(r.code for r in records) == ["acme", "beta"]


def test_stopped_kaggle_harvest_can_still_produce_tokens(tmp_path):
    from company_dataset.sources import kaggle_datasets as kg

    out = tmp_path / kg.META.name
    out.mkdir()
    (out / "datasets.jsonl").write_text(json.dumps({"dataset": "a/b", "license": "MIT", "status": "read", "tokens": [["lever", "acme", ""]]}) + "\n")
    assert kg.rebuild_tokens(tmp_path) == 1


def test_fast_limits_only_loosen_the_two_rate_limited_vendors():
    from company_dataset.config import FAST_RATE_LIMITS, resolve_rate_limits

    normal, fast = resolve_rate_limits(env={}), resolve_rate_limits(overrides=FAST_RATE_LIMITS, env={})
    assert fast["smartrecruiters"]["max_concurrent"] > normal["smartrecruiters"]["max_concurrent"]
    assert fast["breezy"]["delay_seconds"] < normal["breezy"]["delay_seconds"]
    assert fast["workday"] == normal["workday"]
