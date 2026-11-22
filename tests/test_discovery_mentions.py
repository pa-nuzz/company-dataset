import json
from datetime import datetime, timezone

from conftest import FakeResponse, FakeSession

from company_dataset.http import HttpClient
from company_dataset.sources import SOURCES, hf_datasets as hf, hn_hiring as hn


def client(script=None):
    return HttpClient(session=FakeSession(script or {}), sleep=lambda s: None, per_host_interval=0, max_attempts=1)


def test_quarters_cover_the_history_newest_first_and_stop_at_now():
    windows = hn.quarters(datetime(2015, 5, 2, tzinfo=timezone.utc), oldest_year=2014)
    assert len(windows) == 6
    assert windows[0][0] == int(datetime(2015, 4, 1, tzinfo=timezone.utc).timestamp())
    assert windows[-1][0] == int(datetime(2014, 1, 1, tzinfo=timezone.utc).timestamp())
    assert windows[-1][1] == int(datetime(2014, 4, 1, tzinfo=timezone.utc).timestamp())


NOW = datetime(2014, 2, 1, tzinfo=timezone.utc)
START, END = hn.quarters(NOW, 2014)[0]


def test_hn_reads_every_page_and_unescapes_links(tmp_path):
    p0, p1 = hn.search_url("jobs.lever.co", START, END, 0), hn.search_url("jobs.lever.co", START, END, 1)
    c = client({p0: FakeResponse(200, json.dumps({"nbPages": 2, "hits": [{"objectID": "1", "comment_text": "Apply: https:&#x2F;&#x2F;jobs.lever.co&#x2F;acme&#x2F;123"}]})),
                p1: FakeResponse(200, json.dumps({"nbPages": 2, "hits": [{"objectID": "2", "comment_text": "<a href=\"https://jobs.ashbyhq.com/beta\">x</a>"}]}))})
    out = hn.harvest(tmp_path, c, now=NOW, hosts=["jobs.lever.co"])
    assert out == {"boards": 2, "failed_windows": 0}
    records, _ = hn.parse(tmp_path / hn.META.name / "tokens.csv")
    assert sorted(r.code for r in records) == ["acme", "beta"]


def test_hn_resumes_and_retries_only_failed_windows(tmp_path):
    url = hn.search_url("jobs.lever.co", START, END, 0)
    assert hn.harvest(tmp_path, client({url: FakeResponse(500, "")}), now=NOW, hosts=["jobs.lever.co"])["failed_windows"] == 1
    good = client({url: FakeResponse(200, json.dumps({"nbPages": 1, "hits": [{"objectID": "1", "comment_text": "https://jobs.lever.co/acme"}]}))})
    assert hn.harvest(tmp_path, good, now=NOW, hosts=["jobs.lever.co"])["boards"] == 1
    again = client()
    assert hn.harvest(tmp_path, again, now=NOW, hosts=["jobs.lever.co"])["boards"] == 1
    assert again.session.calls == []


def test_hf_license_is_read_from_tags():
    assert hf.license_of({"tags": ["task:x", "license:cc-by-4.0"]}) == "cc-by-4.0"
    assert hf.license_of({"tags": []}) == "none"


def test_hf_only_reads_permissively_licensed_datasets(tmp_path, monkeypatch):
    search = {"https://huggingface.co/api/datasets?search=job%20postings&limit=100&sort=downloads": FakeResponse(200, json.dumps([
        {"id": "a/open", "tags": ["license:mit"]}, {"id": "b/nc", "tags": ["license:cc-by-nc-sa-4.0"]}, {"id": "c/none", "tags": []}]))}
    c = client({**search, f"{hf.PARQUET}?dataset=a/open": FakeResponse(200, json.dumps({"parquet_files": [{"url": "u1", "size": 10}]}))})
    monkeypatch.setattr(hf, "_rows_as_text", lambda con, url: iter(['{"apply":"https://jobs.lever.co/acme"}']))
    out = hf.harvest(tmp_path, c)
    assert out == {"datasets_read": 1, "skipped_for_license": 2, "boards": 1}
    assert not any("b/nc" in call or "c/none" in call for call in c.session.calls)
    report = json.loads((tmp_path / hf.META.name / "report.json").read_text())
    assert report["read"][0]["boards"] == 1 and "b/nc (cc-by-nc-sa-4.0)" in report["skipped_for_license"]


def test_hf_skips_oversized_datasets():
    c = client({f"{hf.PARQUET}?dataset=x/big": FakeResponse(200, json.dumps({"parquet_files": [{"url": "u", "size": hf.MAX_DATASET_BYTES + 1}]}))})
    assert hf.parquet_urls(c, "x/big") == []


def test_mention_sources_are_registered():
    names = {m.META.name for m in SOURCES}
    assert {"hn_hiring", "hf_datasets"} <= names
