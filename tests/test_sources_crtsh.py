import json

from conftest import FakeResponse, FakeSession

from company_dataset.http import HttpClient
from company_dataset.sources import crtsh


def client(script=None):
    return HttpClient(session=FakeSession(script or {}), sleep=lambda s: None, per_host_interval=0, max_attempts=1)


def test_crtsh_harvests_custom_subdomains(tmp_path):
    script = {
        crtsh.query_url("breezy.hr"): FakeResponse(200, json.dumps([
            {"common_name": "acme.breezy.hr", "name_value": "ACME.breezy.hr\nwww.acme.breezy.hr"},
            {"common_name": "*.blog.breezy.hr", "name_value": ""},
            {"common_name": "admin.breezy.hr", "name_value": ""},
        ])),
        crtsh.query_url("greenhouse.io"): FakeResponse(200, json.dumps([
            {"common_name": "", "name_value": "direct.greenhouse.io\nmail1.assets-cdn.greenhouse.io"},
        ])),
        crtsh.query_url("smartrecruiters.com"): FakeResponse(502, ""),
    }
    out = crtsh.harvest(tmp_path, client(script))
    assert out["boards"] == 2
    assert out["domains"] == {
        "breezy.hr": "ok:entries=3 boards=1",
        "greenhouse.io": "ok:entries=1 boards=1",
        "smartrecruiters.com": "error:status_502",
    }
    records, _ = crtsh.parse(tmp_path / crtsh.META.name / "tokens.csv")
    assert sorted(r.ats + "/" + r.code for r in records) == ["breezy/acme", "greenhouse/direct"]


def test_crtsh_ignores_bad_payloads(tmp_path):
    script = {
        crtsh.query_url("breezy.hr"): FakeResponse(200, "<html>overloaded</html>"),
        crtsh.query_url("greenhouse.io"): FakeResponse(200, "[]"),
        crtsh.query_url("smartrecruiters.com"): FakeResponse(200, "null"),
    }
    out = crtsh.harvest(tmp_path, client(script))
    assert out["boards"] == 0
    assert out["domains"]["breezy.hr"] == "error:not_json"
    assert out["domains"]["greenhouse.io"] == "ok:entries=0 boards=0"
    assert out["domains"]["smartrecruiters.com"] == "error:not_json"
    records, _ = crtsh.parse(tmp_path / crtsh.META.name / "tokens.csv")
    assert records == []
