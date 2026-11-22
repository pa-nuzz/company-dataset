"""The enrichment run end to end on a fake network: board checks, homepages, identity, stored evidence."""
import csv
import gzip
import json

import pytest
import requests

from conftest import FakeResponse

from company_dataset import http, store, wikidata
from company_dataset.pilot import main as pilot_main

HOME = ("<html><head><title>Acme Robots | Home</title><meta property='og:site_name' content='Acme Robots'>"
        "<meta name='description' content='Acme Robots builds warehouse automation for regional distribution centers across Europe.'></head>"
        "<body><svg><title>Facebook</title></svg><a href='https://www.linkedin.com/company/acme-robots/'>LinkedIn</a>"
        "<a href='https://job-boards.greenhouse.io/acmerobots'>Jobs</a></body></html>")
SCRIPT = {
    "https://boards-api.greenhouse.io/v1/boards/acmerobots/jobs": FakeResponse(200, json.dumps({"jobs": [
        {"company_name": "Acme Robots", "absolute_url": "https://acmerobots.example.com/careers?gh_jid=1"}], "meta": {"total": 1}})),
    "https://boards-api.greenhouse.io/v1/boards/acmerobots": FakeResponse(200, json.dumps({"name": "Acme Robots", "content": ""})),
    "https://job-boards.greenhouse.io/acmerobots": FakeResponse(200, "<title>Acme Robots</title><a href='https://acmerobots.example.com'>Home</a>", "https://job-boards.greenhouse.io/acmerobots"),
    "https://acmerobots.example.com": FakeResponse(200, HOME, "https://acmerobots.example.com"),
    "https://boards-api.greenhouse.io/v1/boards/soloco/jobs": FakeResponse(200, json.dumps({"jobs": [{"company_name": "Solo Co", "absolute_url": "https://job-boards.greenhouse.io/soloco/jobs/1"}], "meta": {"total": 1}})),
    "https://boards-api.greenhouse.io/v1/boards/soloco": FakeResponse(200, json.dumps({"name": "Solo Co", "content": ""})),
    "https://job-boards.greenhouse.io/soloco": FakeResponse(200, "<title>Solo Co</title>", "https://job-boards.greenhouse.io/soloco"),
    "https://api.smartrecruiters.com/v1/companies/GhostCo/postings?limit=100": FakeResponse(200, json.dumps({"totalFound": 0, "content": []})),
    "https://careers.smartrecruiters.com/GhostCo": FakeResponse(200, "<title>SmartRecruiters Job Search</title>", "https://jobs.smartrecruiters.com"),
}


@pytest.fixture
def world(tmp_path, monkeypatch):
    conn = store.connect(tmp_path / "s.db")
    conn.execute("INSERT INTO sources(id, name, imported_at) VALUES (1, 'lastroundai', 't')")
    for seq, (ats, code, name) in enumerate([("greenhouse", "acmerobots", "Acme Robots"), ("greenhouse", "goneco", "Gone Co"),
                                              ("greenhouse", "soloco", None), ("smartrecruiters", "GhostCo", "Ghost Co")]):
        conn.execute("INSERT INTO source_rows(source_id, seq, row_ref, board_id, ats, code, name) VALUES (1,?,?,?,?,?,?)",
                     (seq, f"r{seq}", store.board_id(conn, ats, code), ats, code, name))
    conn.commit()
    conn.close()
    raw = tmp_path / "raw" / "bigpicture"
    raw.mkdir(parents=True)
    with gzip.open(raw / "companies-2023-q4-sm.csv.gz", "wt", encoding="utf-8") as fh:
        fh.write("handle,name,website,industry,size,type,founded,city,state,country_code\n"
                 "company/acme-robots,Acme Robots,acmerobots.example.com,robotics,51-200,Private,2015,,,DE\n")

    def fake_get(self, url, timeout=None, headers=None, **kwargs):
        return SCRIPT.get(url) or FakeResponse(404, "", url)

    monkeypatch.setattr(requests.Session, "get", fake_get)
    monkeypatch.setattr(http._HostThrottle, "wait", lambda self: None)
    monkeypatch.setattr(wikidata, "lookup", lambda client, hosts: {})
    return tmp_path


def run(world):
    out = world / "run"
    assert pilot_main(["--db", str(world / "s.db"), "--raw", str(world / "raw"), "--run", "t1", "--out", str(out), "--per-ats", "10", "--workers", "2"]) == 0
    with open(out / "results.csv", newline="", encoding="utf-8") as fh:
        return {r["ats_code"]: r for r in csv.DictReader(fh)}, json.loads((out / "report.json").read_text()), store.connect(world / "s.db")


def test_confirmed_board_gets_verified_fields_and_evidence(world):
    rows, report, conn = run(world)
    acme = rows["acmerobots"]
    assert acme["identity_status"] == "identity_confirmed"
    assert acme["name"] == "Acme Robots"
    assert acme["website_url"] == "https://acmerobots.example.com"
    assert acme["linkedin_url"] == "https://www.linkedin.com/company/acme-robots"
    assert acme["description"].startswith("Acme Robots builds warehouse automation")
    assert acme["country"] == "DE"
    assert acme["careersite_status"] == "verified"
    assert acme["complete_strict"] == "True"
    fields = {r["field"]: r["status"] for r in conn.execute(
        "SELECT e.field, e.status FROM field_evidence e JOIN boards b ON b.id=e.board_id WHERE b.code='acmerobots' AND e.run='t1'")}
    assert fields == {f: "verified" for f in ("ats_system", "ats_code", "careersite_url", "name", "website_url", "linkedin_url", "description", "country")}
    assert report["boards"] == 4
    assert report["complete_strict"] == 1


def test_board_with_only_a_vendor_name_stays_unresolved(world):
    rows, _, conn = run(world)
    assert (rows["soloco"]["identity_status"], rows["soloco"]["identity_basis"]) == ("identity_unresolved", "vendor_only")
    assert rows["soloco"]["website_url"] == ""
    assert conn.execute("SELECT COUNT(*) FROM field_evidence e JOIN boards b ON b.id=e.board_id WHERE b.code='soloco' AND e.field='name'").fetchone()[0] == 0


def test_dead_and_invented_boards_are_not_live(world):
    rows, _, conn = run(world)
    assert rows["goneco"]["outcome"] == "dead_removed"
    assert rows["goneco"]["identity_status"] == "not_live"
    assert rows["GhostCo"]["outcome"] == "invalid_identifier"
    assert rows["GhostCo"]["identity_status"] == "not_live"
    assert {r["status"] for r in conn.execute("SELECT status FROM identities WHERE run='t1' AND board_id IN (SELECT id FROM boards WHERE code IN ('goneco', 'GhostCo'))")} == {"not_live"}


def test_refuses_to_overwrite_an_existing_run_directory(world):
    run(world)
    with pytest.raises(SystemExit):
        pilot_main(["--db", str(world / "s.db"), "--raw", str(world / "raw"), "--run", "t1", "--out", str(world / "run")])


def test_run_can_be_limited_to_listed_boards_or_undecided_ones(world):
    ids = world / "ids.txt"
    conn = store.connect(world / "s.db")
    acme = conn.execute("SELECT id FROM boards WHERE code='acmerobots'").fetchone()[0]
    conn.close()
    ids.write_text(f"{acme}\n")
    base = ["--db", str(world / "s.db"), "--raw", str(world / "raw"), "--per-ats", "10", "--workers", "2"]
    assert pilot_main(base + ["--run", "listed", "--out", str(world / "listed"), "--board-ids", str(ids)]) == 0
    with open(world / "listed" / "results.csv", newline="", encoding="utf-8") as fh:
        assert [r["ats_code"] for r in csv.DictReader(fh)] == ["acmerobots"]
    assert pilot_main(base + ["--run", "rest", "--out", str(world / "rest"), "--only-undecided"]) == 0
    with open(world / "rest" / "results.csv", newline="", encoding="utf-8") as fh:
        assert sorted(r["ats_code"] for r in csv.DictReader(fh)) == ["GhostCo", "goneco", "soloco"]


def test_name_matched_homepage_is_not_independent_for_a_dormant_smartrecruiters_board():
    from company_dataset.enrich import homepage_is_independent

    assert homepage_is_independent("reference_name_slug_domain", "site names itself", "smartrecruiters", 0) is False
    assert homepage_is_independent("reference_name_slug_domain", "site names itself", "smartrecruiters", 4) is True
    assert homepage_is_independent("reference_name_slug_domain", "site names itself", "greenhouse", 0) is True
    assert homepage_is_independent("board_link", "site names itself", "smartrecruiters", 0) is True
    assert homepage_is_independent("other", "links_to_board:smartrecruiters:x", "smartrecruiters", 0) is True
    assert homepage_is_independent("other", "site names itself", "smartrecruiters", 4) is False
