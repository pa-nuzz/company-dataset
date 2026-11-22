import json

import pytest

from conftest import FakeResponse, FakeSession

from company_dataset import ingest, store
from company_dataset.http import HttpClient
from company_dataset.sources import SOURCES, search_dorks as sd, slug_files as sf


def client(script=None):
    return HttpClient(session=FakeSession(script or {}), sleep=lambda s: None, per_host_interval=0, max_attempts=1)


@pytest.mark.parametrize("path,text,expected", [
    ("a/slugs.json", json.dumps({"greenhouse": ["stripe", "figma"], "lever": ["netflix"]}), {("greenhouse", "stripe"), ("greenhouse", "figma"), ("lever", "netflix")}),
    ("a/greenhouse-slugs.json", json.dumps(["stripe", "figma"]), {("greenhouse", "stripe"), ("greenhouse", "figma")}),
    ("a/companies.json", json.dumps([{"name": "Stripe", "ats": "greenhouse", "token": "stripe"}, {"name": "Ramp", "platform": "Ashby", "slug": "ramp"}]), {("greenhouse", "stripe"), ("ashby", "ramp")}),
    ("a/companies_ashby.txt", "ramp\n# a comment\nnotion # trailing\n\n", {("ashby", "ramp"), ("ashby", "notion")}),
    ("a/companies.yaml", "greenhouse:\n  - stripe\n  - figma\nlever:\n  - netflix\n", {("greenhouse", "stripe"), ("greenhouse", "figma"), ("lever", "netflix")}),
    ("a/companies.yaml", "- name: Ramp\n  ats: ashby\n  slug: ramp\n", {("ashby", "ramp")}),
    # Nothing ties these to an ATS.
    ("owid/slugs.json", json.dumps(["life-expectancy", "co2-emissions"]), set()),
    ("a/companies.json", json.dumps({"names": ["stripe", "figma"]}), set()),
    ("a/list.txt", "stripe\nfigma\n", set()),
    ("a/greenhouse-and-lever.json", json.dumps(["stripe"]), set()),
    # Values that cannot be identifiers.
    ("a/greenhouse.json", json.dumps(["Acme Corp", "https://x.com/y", "true", "greenhouse", "$bad", "3.0.7"]), set()),
    ("a/slugs.json", "not json at all", set()),
])
def test_slugs_in_file(path, text, expected):
    assert sf.slugs_in_file(path, text) == expected


def test_ats_in_name_needs_exactly_one_ats():
    assert sf.ats_in_name("greenhouse_vetted_slugs.json") == "greenhouse"
    assert sf.ats_in_name("green_house.json") is None
    assert sf.ats_in_name("leverage.json") is None
    assert sf.ats_in_name("lever-ashby.json") is None


def test_harvest_combines_bare_slugs_and_urls_and_keeps_no_names(tmp_path):
    def search(path, **params):
        if params["q"] == sf.QUERIES[0] and params["page"] == 1:
            return {"items": [{"html_url": "https://github.com/o/r/blob/main/greenhouse_slugs.json", "file_size": 10}]}
        return {"items": []}

    body = json.dumps(["stripe", "https://jobs.lever.co/netflix"])
    c = client({"https://raw.githubusercontent.com/o/r/main/greenhouse_slugs.json": FakeResponse(200, body)})
    assert sf.harvest(tmp_path, c, search=search, sleep=lambda s: None) == {"files_read": 1, "files_with_boards": 1, "boards": 2}
    records, _ = sf.parse(tmp_path / sf.META.name / "tokens.csv")
    assert sorted((r.ats, r.code) for r in records) == [("greenhouse", "stripe"), ("lever", "netflix")]
    assert all(r.name is None for r in records)


def write_tokens(raw, name, rows):
    (raw / name).mkdir(exist_ok=True)
    (raw / name / "tokens.csv").write_text("ats,code,region,evidence\n" + "".join(f"{a},{c},,u\n" for a, c in rows))


def test_incremental_import_appends_only_new_rows(tmp_path):
    conn = store.connect(tmp_path / "s.db")
    write_tokens(tmp_path, sf.META.name, [("lever", "acme")])
    assert ingest.import_sources(conn, tmp_path, "t1", modules=[sf], incremental=True)[sf.META.name]["records"] == 1
    write_tokens(tmp_path, sf.META.name, [("lever", "acme"), ("lever", "ACME"), ("ashby", "beta")])
    report = ingest.import_sources(conn, tmp_path, "t2", modules=[sf], incremental=True)
    assert report[sf.META.name]["status"] == "appended" and report[sf.META.name]["records"] == 1
    rows = conn.execute("SELECT seq, code FROM source_rows ORDER BY seq").fetchall()
    assert [(r["seq"], r["code"]) for r in rows] == [(0, "acme"), (1, "beta")]
    assert ingest.import_sources(conn, tmp_path, "t3", modules=[sf], incremental=True)[sf.META.name]["status"] == "already_imported"
    assert conn.execute("SELECT rows_read FROM sources").fetchone()[0] == 2


def test_changed_file_is_still_refused_without_incremental(tmp_path):
    conn = store.connect(tmp_path / "s.db")
    write_tokens(tmp_path, sf.META.name, [("lever", "acme")])
    ingest.import_sources(conn, tmp_path, "t", modules=[sf])
    write_tokens(tmp_path, sf.META.name, [("lever", "other")])
    with pytest.raises(RuntimeError):
        ingest.import_sources(conn, tmp_path, "t", modules=[sf])


def test_brave_backend_sends_its_key_in_a_header_and_reads_results(tmp_path):
    env = {"BRAVE_API_KEY": "SECRET"}
    name, build = sd.backend_from_env(env)
    assert name == "brave"
    url = build("site:jobs.lever.co", 0)
    assert "SECRET" not in url
    assert sd.request_headers(name, env) == {"X-Subscription-Token": "SECRET", "Accept": "application/json"}
    assert sd.result_links({"web": {"results": [{"url": "https://jobs.lever.co/acme"}]}}) == ["https://jobs.lever.co/acme"]


def test_slug_files_registered():
    assert "slug_files" in {m.META.name for m in SOURCES}
