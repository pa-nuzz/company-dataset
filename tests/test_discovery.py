import csv
import gzip
import json

import pytest

from conftest import FakeResponse, FakeSession

from company_dataset.http import HttpClient
from company_dataset.sources import _harvest, commoncrawl_indexer as cci, slug_prober as prober, wayback_cdx as wb


def client(script):
    return HttpClient(session=FakeSession(script), sleep=lambda s: None, per_host_interval=0, max_attempts=1)


@pytest.mark.parametrize("url,expected", [
    ("https://jobs.lever.co/acme/123", ("lever", "acme", "")),
    ("http://boards.eu.greenhouse.io/acme/jobs/2", ("greenhouse", "acme", "eu")),
    ("https://acme.breezy.hr/p/1", ("breezy", "acme", "")),
    ("https://jobs.lever.co/$company", None),
    ("https://jobs.lever.co/{{slug}}/x", None),
    ("https://www.breezy.hr/pricing", None),
    ("https://apply.workable.com/acme/", None),
    ("not a url", None),
])
def test_board_from_url(url, expected):
    assert _harvest.board_from_url(url) == expected


def test_discovered_identifiers_carry_no_name_and_bad_rows_are_rejected(tmp_path):
    path = tmp_path / "tokens.csv"
    path.write_text("ats,code,region,evidence\nlever,acme,,u\ngreenhouse,euco,eu,u\nworkable,nope,,u\nlever,$bad,,u\n")
    records, rejects = _harvest.parse_tokens(path, "src")
    assert [(r.ats, r.code, r.region, r.name) for r in records] == [("lever", "acme", None, None), ("greenhouse", "euco", "eu", None)]
    assert sorted(r.reason for r in rejects) == ["invalid_slug", "unsupported_ats"]


TARGET = ("jobs.lever.co/*", None)
NUM = wb._query(TARGET, showNumPages="true")
P0, P1 = wb._query(TARGET, page=0, fl="original"), wb._query(TARGET, page=1, fl="original")


def read_tokens(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_wayback_collects_unique_boards_across_pages(tmp_path):
    c = client({NUM: FakeResponse(200, "2\n"), P0: FakeResponse(200, "https://jobs.lever.co/acme/1\nhttps://jobs.lever.co/ACME/2\nhttps://jobs.lever.co/$x\n"),
                P1: FakeResponse(200, "https://jobs.lever.co/beta\n")})
    out = wb.harvest(tmp_path, c, sleep=lambda s: None, targets=[TARGET])
    assert out == {"pages_fetched": 2, "boards": 2, "complete": True}
    rows = read_tokens(tmp_path / wb.META.name / "tokens.csv")
    assert [(r["ats"], r["code"]) for r in rows] == [("lever", "acme"), ("lever", "beta")]
    assert rows[0]["evidence"] == "https://jobs.lever.co/acme/1"


def test_wayback_resumes_without_refetching_finished_pages(tmp_path):
    first = client({NUM: FakeResponse(200, "2\n"), P0: FakeResponse(200, "https://jobs.lever.co/acme\n")})
    assert wb.harvest(tmp_path, first, sleep=lambda s: None, targets=[TARGET], max_pages=1)["complete"] is False
    assert not (tmp_path / wb.META.name / "tokens.csv").exists()
    second = client({NUM: FakeResponse(200, "2\n"), P1: FakeResponse(200, "https://jobs.lever.co/beta\n")})
    assert wb.harvest(tmp_path, second, sleep=lambda s: None, targets=[TARGET])["boards"] == 2
    assert P0 not in second.session.calls


def test_wayback_backs_off_then_recovers(tmp_path):
    waits = []
    c = client({NUM: FakeResponse(200, "1\n"), P0: [FakeResponse(503, ""), FakeResponse(429, ""), FakeResponse(200, "https://jobs.lever.co/acme\n")]})
    assert wb.harvest(tmp_path, c, sleep=waits.append, targets=[TARGET])["boards"] == 1
    assert waits == list(wb.BACKOFF_SECONDS[:2])


def test_wayback_stops_after_persistent_refusal_and_keeps_finished_pages(tmp_path):
    c = client({NUM: FakeResponse(200, "2\n"), P0: FakeResponse(200, "https://jobs.lever.co/acme\n"), P1: [FakeResponse(503, "")] * 10})
    with pytest.raises(wb.HarvestStopped):
        wb.harvest(tmp_path, c, sleep=lambda s: None, targets=[TARGET])
    log = (tmp_path / wb.META.name / "pages.jsonl").read_text().splitlines()
    assert json.loads(log[0])["page"] == 0
    assert not (tmp_path / wb.META.name / "tokens.csv").exists()


def test_common_crawl_work_list_covers_gaps_newest_first():
    pairs = cci.work_list(["CC-MAIN-2026-30", "CC-MAIN-2026-39", "CC-MAIN-2018-05"], harvested=["CC-MAIN-2026-39"], oldest_year=2019)
    assert pairs[0][0] == "CC-MAIN-2026-39"
    assert {p for c, p in pairs if c == "CC-MAIN-2026-39"} == set(cci.EU_PREFIXES)
    assert {p for c, p in pairs if c == "CC-MAIN-2026-30"} == set(cci.PREFIXES + cci.EU_PREFIXES)
    assert not any(c == "CC-MAIN-2018-05" for c, _ in pairs)


def test_common_crawl_reads_only_files_whose_range_overlaps():
    prefix = "co,lever,jobs)/"
    footers = [("a", "co,kitchen)/", "co,lever,jobs)/m"), ("b", "co,lever,jobs)/m", "co,lever,jobs)/z"), ("c", "co,lever,jobs*", "de,x)/"),
               ("d", "ab)/", "co,kitchen)/"), ("e", None, None), ("a", "zz)/", "zzz)/")]
    assert cci.relevant_files(footers, prefix) == ["a", "b"]
    assert cci.prefix_end(prefix) == "co,lever,jobs)0"


def test_candidate_slugs():
    assert prober.candidate_slugs("Acme Robotics Inc", "https://www.acme-robots.com") == ["acme-robots", "acmerobotics", "acme-robotics-inc"]
    assert prober.candidate_slugs("Stripe", "stripe.com") == ["stripe"]
    assert prober.candidate_slugs("X", None) == []
    assert prober.candidate_slugs(None, "bbc.co.uk") == ["bbc"]


def test_plan_skips_known_boards_and_respects_the_limit():
    companies = [("Acme", "acme.com"), ("Beta", "beta.com"), ("Gamma", "gamma.com"), ("Delta", "delta.com")]
    plan = prober.plan_slugs(companies, {("lever", "acme")}, limit=3, seed=1)
    assert all(len(v) == 3 for v in plan.values())
    assert "acme" not in plan["lever"]
    assert plan == prober.plan_slugs(companies, {("lever", "acme")}, limit=3, seed=1)


@pytest.mark.parametrize("ats,status,final,expected", [
    ("greenhouse", 200, None, "live"),
    ("greenhouse", 404, None, "missing"),
    ("lever", 200, None, "live"),
    ("lever", 429, None, "rate_limited"),
    ("ashby", 500, None, "error"),
    ("smartrecruiters", 200, "https://careers.smartrecruiters.com/acme", "live"),
    ("smartrecruiters", 200, "https://jobs.smartrecruiters.com", "missing"),
])
def test_probe_outcomes(ats, status, final, expected):
    url = prober.probe_url(ats, "acme")
    assert prober.probe(client({url: FakeResponse(status, "", final or url)}), ats, "acme") == expected


def test_prober_stops_a_vendor_that_keeps_rate_limiting():
    slugs = [f"slug{i}" for i in range(20)]
    c = client({prober.probe_url("lever", s): FakeResponse(429, "") for s in slugs})
    waits = []
    stats = prober.probe_vendor(c, "lever", slugs, sleep=waits.append)
    assert stats["stopped_by_rate_limit"] is True
    assert stats["tried"] == prober.MAX_CONSECUTIVE_RATE_LIMITS
    assert len(waits) == prober.MAX_CONSECUTIVE_RATE_LIMITS - 1


def test_prober_writes_hits_with_no_name_and_a_report(tmp_path):
    url = prober.probe_url("greenhouse", "acme")
    report = prober.run(tmp_path, {"greenhouse": ["acme", "ghost"]}, client_factory=lambda: client({url: FakeResponse(200, "{}", url)}))
    assert report["greenhouse"]["live"] == 1 and report["greenhouse"]["missing"] == 1
    assert [r["code"] for r in read_tokens(tmp_path / prober.META.name / "tokens.csv")] == ["acme"]
    records, _ = prober.parse(tmp_path / prober.META.name / "tokens.csv")
    assert records[0].name is None


def test_new_sources_are_registered_and_ingest_can_select_them(tmp_path):
    from company_dataset import ingest, store
    from company_dataset.sources import SOURCES

    names = {m.META.name for m in SOURCES}
    assert {wb.META.name, cci.META.name, prober.META.name} <= names
    (tmp_path / wb.META.name).mkdir()
    (tmp_path / wb.META.name / "tokens.csv").write_text("ats,code,region,evidence\nlever,acme,,u\n")
    conn = store.connect(tmp_path / "s.db")
    report = ingest.import_sources(conn, tmp_path, "t", only={wb.META.name})
    assert list(report) == [wb.META.name]
    assert report[wb.META.name]["records"] == 1
    assert conn.execute("SELECT name FROM source_rows").fetchone()[0] is None


def test_wayback_page_count_query_does_not_ask_for_fields():
    assert "fl=" not in NUM and "showNumPages=true" in NUM
    assert "fl=original" in P0


def test_wayback_stops_when_the_page_count_is_not_a_number(tmp_path):
    with pytest.raises(wb.HarvestStopped):
        wb.harvest(tmp_path, client({NUM: FakeResponse(200, "-\n")}), sleep=lambda s: None, targets=[TARGET])
