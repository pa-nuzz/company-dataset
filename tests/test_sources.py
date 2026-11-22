import json
from pathlib import Path

from company_dataset.sources import feashliaa, headcountlabs, kaleb, lastround, latmay, moonie, openatsfeed

LATMAY_CSV = "\ufeffcanonical_url,ats_platform\n" + (
    "https://job-boards.greenhouse.io/acme,Greenhouse\n"
    "https://jobs.lever.co/leverco,Lever\n"
    "https://jobs.ashbyhq.com/ashbyco,Ashby\n"
    "https://10percent.breezy.hr,Breezy HR\n"
    "https://careers.smartrecruiters.com/AbbVie,SmartRecruiters\n"
    "https://3m.wd1.myworkdayjobs.com/Search,Workday\n"
    "https://jobs.lever.co/,Lever\n"
    "https://job-boards.greenhouse.io/.well-known,Greenhouse\n"
    "https://jobs.lever.co/leverco,Lever\n"
)

LASTROUND_CSV = (
    "ats_vendor,company_name,board_slug,last_crawled\n"
    "greenhouse,10a Labs,10alabs,2026-07-17\n"
    'ashby,"0g Labs",0g,2026-07-12\n'
    "lever,100MS,100ms,2026-08-01\n"
)

KALEB_CSV = (
    "platform,token,company,board_url,api_url,open_roles,count_basis,region\n"
    "greenhouse,10alabs,10alabs,https://job-boards.greenhouse.io/10alabs,https://boards-api.greenhouse.io/v1/boards/10alabs/jobs,16,vendor,\n"
    "lever,aavelabs,aavelabs,https://jobs.eu.lever.co/aavelabs,https://api.eu.lever.co/v0/postings/aavelabs?mode=json,14,observed,EU\n"
    "ashby,0g,0g,https://jobs.ashbyhq.com/0g,https://api.ashbyhq.com/posting-api/job-board/0g,8,observed,\n"
    "workable,hf,hf,https://apply.workable.com/hf,https://apply.workable.com/api/v1/widget?id=1,2,vendor,\n"
)

MOONIE_JSONL = (
    '{"provider": "greenhouse", "slug": "anthropic", "site": null, "region": null, "name": "Anthropic", "status": "ok"}\n'
    '{"provider": "lever", "slug": "bunq", "site": null, "region": "eu", "name": null, "status": "ok"}\n'
    '{"provider": "lever", "slug": "oldco", "site": null, "region": null, "name": null, "status": "dead"}\n'
    '{"provider": "recruitee", "slug": "x", "site": null, "region": null, "name": null, "status": "ok"}\n'
    '{"provider": "lever", "slug": "maybe", "site": null, "region": null, "name": "Maybe", "status": "unconfirmed"}\n'
    "\n"
)


def test_latmay_parses_and_rejects(tmp_path):
    path = tmp_path / "latmay.csv"
    path.write_text(LATMAY_CSV, encoding="utf-8")
    records, rejects = latmay.parse(path)
    assert [(r.ats, r.code) for r in records] == [
        ("greenhouse", "acme"),
        ("lever", "leverco"),
        ("ashby", "ashbyco"),
        ("breezy", "10percent"),
        ("smartrecruiters", "AbbVie"),
        ("workday", "3m.wd1/Search"),
        ("lever", "leverco"),
    ]
    assert records[0].source == latmay.META.name
    assert records[0].careersite_url == "https://job-boards.greenhouse.io/acme"
    reasons = sorted(r.reason for r in rejects)
    assert reasons == ["unparseable_url", "unparseable_url"]


def test_lastround_parses(tmp_path):
    path = tmp_path / "lastround.csv"
    path.write_text(LASTROUND_CSV, encoding="utf-8")
    records, rejects = lastround.parse(path)
    assert len(records) == 3 and rejects == []
    by_ats = {r.ats: r for r in records}
    assert by_ats["greenhouse"].name == "10a Labs"
    assert by_ats["greenhouse"].code == "10alabs"
    assert by_ats["ashby"].name == "0g Labs"
    assert by_ats["ashby"].code == "0g"
    assert by_ats["lever"].name == "100MS"


def test_kaleb_parses_with_eu_region(tmp_path):
    path = tmp_path / "kaleb.csv"
    path.write_text(KALEB_CSV, encoding="utf-8")
    records, rejects = kaleb.parse(path)
    assert len(records) == 4
    assert rejects == []
    workable = next(r for r in records if r.ats == "workable")
    assert (workable.code, workable.careersite_url) == ("hf", "https://apply.workable.com/hf")
    lever = next(r for r in records if r.ats == "lever")
    assert lever.code == "aavelabs"
    assert lever.region == "eu"
    assert lever.careersite_url == "https://jobs.eu.lever.co/aavelabs"
    ashby = next(r for r in records if r.ats == "ashby")
    assert ashby.name == "0g"
    assert ashby.region is None


def test_moonie_parses_statuses(tmp_path):
    path = tmp_path / "moonie.jsonl"
    path.write_text(MOONIE_JSONL, encoding="utf-8")
    records, rejects = moonie.parse(path)
    assert [(r.ats, r.code) for r in records] == [
        ("greenhouse", "anthropic"),
        ("lever", "bunq"),
        ("recruitee", "x"),
        ("lever", "maybe"),
    ]
    bunq = next(r for r in records if r.code == "bunq")
    assert bunq.region == "eu"
    anthropic = next(r for r in records if r.code == "anthropic")
    assert anthropic.name == "Anthropic"
    reasons = sorted(r.reason for r in rejects)
    assert reasons == ["blank_line", "source_marked_dead"]
    assert all(r.source == moonie.META.name for r in rejects)


def test_moonie_accepts_mixed_case_provider(tmp_path):
    path = tmp_path / "moonie.jsonl"
    path.write_text(
        '{"provider": "Greenhouse", "slug": "acme", "site": null, "region": null, "name": "Acme", "status": "ok"}\n',
        encoding="utf-8",
    )
    records, rejects = moonie.parse(path)
    assert rejects == []
    assert len(records) == 1
    assert records[0].ats == "greenhouse"
    assert records[0].code == "acme"


def test_moonie_counts_blank_lines_as_rejects(tmp_path):
    path = tmp_path / "moonie.jsonl"
    path.write_text(
        '{"provider": "greenhouse", "slug": "acme", "site": null, "region": null, "name": "Acme", "status": "ok"}\n\n\n',
        encoding="utf-8",
    )
    records, rejects = moonie.parse(path)
    assert len(records) == 1
    assert [r.reason for r in rejects] == ["blank_line", "blank_line"]


KALEB_MISMATCH_CSV = (
    "platform,token,company,board_url,api_url,open_roles,count_basis,region\n"
    "greenhouse,acme,Acme,https://jobs.lever.co/acme,,5,vendor,\n"
)


def test_kaleb_rejects_platform_url_mismatch(tmp_path):
    path = tmp_path / "kaleb.csv"
    path.write_text(KALEB_MISMATCH_CSV, encoding="utf-8")
    records, rejects = kaleb.parse(path)
    assert records == []
    assert [r.reason for r in rejects] == ["platform_url_mismatch"]
    assert rejects[0].detail == "label=greenhouse url_host_ats=lever"


def test_adapters_skip_missing_file(tmp_path):
    for mod in (latmay, lastround, kaleb, moonie):
        records, rejects = mod.parse(tmp_path / "does-not-exist.csv")
        assert records == []
        assert rejects == []


def test_source_meta_has_licenses():
    for mod in (latmay, lastround, kaleb, moonie):
        assert mod.META.name
        assert mod.META.download_url
        assert mod.META.license
    assert latmay.META.license.startswith("CC-BY")
    assert lastround.META.license.startswith("CC-BY")
    assert moonie.META.license.startswith("CC0")
    assert kaleb.META.license == "CC0-1.0"
    assert "schema.org" in kaleb.META.license_note


class TestFeashliaa:
    def test_parse_valid_file(self, tmp_path: Path):
        data = {
            "greenhouse": ["stripe", "vercel"],
            "lever": ["airtable"],
            "ashby": ["0g", "0x"],
        }
        path = tmp_path / "companies.json"
        path.write_text(json.dumps(data))
        records, rejects = feashliaa.parse(path)
        assert len(records) == 5
        assert len(rejects) == 0
        by_ats = {}
        for r in records:
            by_ats.setdefault(r.ats, []).append(r.code)
        assert set(by_ats) == {"greenhouse", "lever", "ashby"}
        assert by_ats["greenhouse"] == ["stripe", "vercel"]

    def test_parse_empty_file(self, tmp_path: Path):
        path = tmp_path / "companies.json"
        path.write_text("{}")
        records, rejects = feashliaa.parse(path)
        assert records == []
        assert rejects == []

    def test_parse_missing_file(self, tmp_path: Path):
        records, rejects = feashliaa.parse(tmp_path / "nonexistent.json")
        assert records == []
        assert rejects == []

    def test_invalid_slugs_rejected(self, tmp_path: Path):
        data = {"greenhouse": ["ok", "with/slash", "trailing;", "", ".hidden"]}
        path = tmp_path / "companies.json"
        path.write_text(json.dumps(data))
        records, rejects = feashliaa.parse(path)
        # A trailing ";" is a scraping artifact: cleaned off, then left to validation.
        assert [r.code for r in records] == ["ok", "trailing"]
        assert len(rejects) == 3
        reasons = {r.reason for r in rejects}
        assert reasons == {"invalid_slug"}

    def test_meta_has_license(self):
        assert feashliaa.META.license == "MIT"
        assert "feashliaa" in feashliaa.META.name


def test_openatsfeed_keeps_only_supported_ats(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text('{"greenhouse": ["acme", " ", "abridge\\\\"], "ashby": ["Applied Compute"], "jazzhr": ["x"]}')
    records, rejects = openatsfeed.parse(path)
    assert [(r.ats, r.code) for r in records] == [("greenhouse", "acme"), ("greenhouse", "abridge"), ("ashby", "Applied Compute")]
    assert len(rejects) == 1


def test_openatsfeed_keeps_breezy_and_smartrecruiters(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text('{"breezy": ["10percent", "www", "api"], "smartrecruiters": ["AbbVie", ""], "jazzhr": ["x"]}')
    records, rejects = openatsfeed.parse(path)
    assert [(r.ats, r.code) for r in records] == [("breezy", "10percent"), ("smartrecruiters", "AbbVie")]
    assert len(rejects) == 3


def test_headcountlabs_parse(tmp_path):
    path = tmp_path / "boards.csv"
    path.write_text("ats,code,employer_name\nashby,0g,0g Labs\njazzhr,x,X\ngreenhouse,,Nope\n")
    records, rejects = headcountlabs.parse(path)
    assert [(r.ats, r.code, r.name) for r in records] == [("ashby", "0g", "0g Labs")]
    assert sorted(r.reason for r in rejects) == ["invalid_slug", "unsupported_ats"]


def test_kalil_parses_urls_and_checks_file_platform(tmp_path):
    from company_dataset.sources import kalil

    path = tmp_path / "companies.csv"
    path.write_text(
        "file_ats,name,slug,url\n"
        "workable,0x,0x,https://apply.workable.com/0x\n"
        "workday,23Andme,23andme/23,https://23andme.wd5.myworkdayjobs.com/23\n"
        "icims,360care LLC,360care,https://careers-360care.icims.com\n"
        "join,Klarkode GmbH,02100,https://join.com/companies/02100\n"
        "bamboohr,Wrong,wrong,https://wrong.recruitee.com\n"
        "paycom,X,x,https://www.paycomonline.net/v4/ats/web.php/portal/x/career-page\n",
        encoding="utf-8",
    )
    records, rejects = kalil.parse(path)
    assert [(r.ats, r.code, r.name) for r in records] == [
        ("workable", "0x", "0x"),
        ("workday", "23andme.wd5/23", "23Andme"),
        ("icims", "careers-360care", "360care LLC"),
        ("join", "02100", "Klarkode GmbH"),
    ]
    assert sorted(r.reason for r in rejects) == ["platform_url_mismatch", "unsupported_ats"]


def test_feashliaa_workday_and_icims_slug_formats(tmp_path):
    path = tmp_path / "companies.json"
    path.write_text(json.dumps({
        "workday": ["2020companies|wd1|external_careers", "broken|wd1", "wd503|wd1|smartbank"],
        "icims": ["142designgroup", "-careers-harrowcouncil"],
        "bamboohr": ["10web"],
    }))
    records, rejects = feashliaa.parse(path)
    assert [(r.ats, r.code) for r in records] == [
        ("workday", "2020companies.wd1/external_careers"),
        ("icims", "careers-142designgroup"),
        ("icims", "careers-harrowcouncil"),
        ("bamboohr", "10web"),
    ]
    # Upstream also lists shard names as tenants ("wd503|wd1|smartbank"): malformed, not a board.
    assert [r.reason for r in rejects] == ["invalid_slug", "invalid_slug"]


def test_joa_takes_board_tokens_from_apply_urls_once(tmp_path):
    from company_dataset.sources import joa

    path = tmp_path / "joa.jsonl"
    path.write_text(
        '{"employer": "Dovetail", "apply_url": "https://jobs.ashbyhq.com/dovetail/7902/application"}\n'
        '{"employer": "Dovetail", "apply_url": "https://jobs.ashbyhq.com/dovetail/8000/application"}\n'
        '{"employer": "JD Sports", "apply_url": "https://job-boards.eu.greenhouse.io/jdsports/jobs/1"}\n'
        '{"employer": "Own Site", "apply_url": "https://careers.example.com/1"}\n'
        "not json\n",
        encoding="utf-8",
    )
    records, rejects = joa.parse(path)
    assert [(r.ats, r.code, r.region, r.name) for r in records] == [
        ("ashby", "dovetail", None, "Dovetail"),
        ("greenhouse", "jdsports", "eu", "JD Sports"),
    ]
    assert [r.reason for r in rejects] == ["invalid_json"]


HARVEST_ONLY = ("africa_ats", "builder_jobs", "commoncrawl_gapfill", "jobanalytics", "legacy_subdomains")


def test_harvested_adapters_are_registered_but_never_downloaded(tmp_path):
    from company_dataset.config import resolve_dirs
    from company_dataset.legacy.download import run_download
    from company_dataset.http import HttpClient
    from company_dataset.sources import SOURCES

    by_name = {mod.META.name: mod for mod in SOURCES}
    for name in HARVEST_ONLY:
        assert by_name[name].META.downloadable is False

    class NoNetwork:
        def get(self, url, **kwargs):
            assert not any(host in url for host in ("github.com/Thelastpoet", "github.com/zachproffitt", "github.com/RichJacobs69", "cc-index/collections")), url
            raise OSError("offline")

    dirs = resolve_dirs(tmp_path)
    entries = {e["source"]: e for e in run_download(dirs, client=HttpClient(session=NoNetwork(), sleep=lambda s: None, max_attempts=1))}
    for name in HARVEST_ONLY:
        assert entries[name]["error"] == "not downloadable: harvested once, raw file kept outside the pipeline"
        assert not (dirs["raw"] / name / "tokens.csv").exists()


def test_harvested_adapter_with_no_raw_file_reports_file_missing(tmp_path):
    from company_dataset import store
    from company_dataset.sources import africa_ats
    from company_dataset.ingest import import_sources

    conn = store.connect(tmp_path / "s.db")
    report = import_sources(conn, tmp_path, "t", modules=[africa_ats])
    assert report["africa_ats"]["status"] == "file_missing"
    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
