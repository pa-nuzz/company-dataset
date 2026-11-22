import io
import json
import zipfile

import pytest

from conftest import FakeResponse, FakeSession

from company_dataset.http import HttpClient
from company_dataset.sources import SOURCES, kaggle_datasets as kg, passive_dns as pd, vc_boards as vc


def client(script=None):
    return HttpClient(session=FakeSession(script or {}), sleep=lambda s: None, per_host_interval=0, max_attempts=1)


@pytest.mark.parametrize("host,expected", [
    ("acme.breezy.hr", ("breezy", "acme")),
    ("WWW.Acme.breezy.hr.", ("breezy", "acme")),
    ("*.acme.breezy.hr", ("breezy", "acme")),
    ("acme.greenhouse.io", ("greenhouse", "acme")),
    ("acme.smartrecruiters.com", ("smartrecruiters", "acme")),
    ("mail1.assets-cdn.breezy.hr", None),
    ("staging-argocd.perform.breezy.hr", None),
    ("admin.breezy.hr", None),
    ("app.breezy.hr", None),
    ("breezy.hr", None),
    ("acme.notbreezy.hr", None),
    ("acme.lever.co", None),
])
def test_board_from_host(host, expected):
    assert pd.board_from_host(host) == expected


def test_passive_dns_merges_tool_output_without_names(tmp_path):
    out = tmp_path / pd.META.name
    out.mkdir()
    (out / "subfinder.txt").write_text("acme.breezy.hr\nbeta.breezy.hr\nadmin.breezy.hr\n")
    (out / "amass.txt").write_text("ACME.breezy.hr\ngamma.greenhouse.io\n")
    result = pd.build(tmp_path)
    assert result["boards"] == 3 and result["names_per_file"] == {"amass.txt": 2, "subfinder.txt": 3}
    records, _ = pd.parse(out / "tokens.csv")
    assert sorted(r.code.casefold() for r in records) == ["acme", "beta", "gamma"]
    assert all(r.name is None for r in records)


SITEMAP_INDEX = "<sitemapindex><sitemap><loc>https://v.example/sitemap_companies.xml</loc></sitemap><sitemap><loc>https://v.example/sitemap_jobs1.xml</loc></sitemap></sitemapindex>"
COMPANIES = "<urlset><url><loc>https://v.example/company/acme</loc></url><url><loc>https://v.example/company/secret</loc></url><url><loc>https://v.example/about</loc></url></urlset>"


def test_vc_site_follows_sitemap_to_company_pages_and_obeys_robots(tmp_path):
    c = client({
        "https://v.example/robots.txt": FakeResponse(200, "User-agent: *\nDisallow: /company/secret\n"),
        "https://v.example/jobs": FakeResponse(200, '{"apply":"https:\\/\\/jobs.lever.co\\/fromjobs"}'),
        "https://v.example/sitemap.xml": FakeResponse(200, SITEMAP_INDEX),
        "https://v.example/sitemap_companies.xml": FakeResponse(200, COMPANIES),
        "https://v.example/company/acme": FakeResponse(200, "<a href='https://boards.greenhouse.io/acme/jobs/1'>x</a>"),
    })
    out = vc.harvest(tmp_path, c, sites=["v.example"])
    assert out == {"sites": 1, "boards": 2}
    assert "https://v.example/company/secret" not in c.session.calls
    assert "https://v.example/sitemap_jobs1.xml" not in c.session.calls
    assert "https://v.example/about" not in c.session.calls


def test_vc_site_disallowing_everything_is_not_read(tmp_path):
    c = client({"https://v.example/robots.txt": FakeResponse(200, "User-agent: *\nDisallow: /\n"), "https://v.example/jobs": FakeResponse(200, "https://jobs.lever.co/acme")})
    assert vc.harvest(tmp_path, c, sites=["v.example"])["boards"] == 0
    assert c.session.calls == ["https://v.example/robots.txt"]


def archive(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, body in files.items():
            z.writestr(name, body)
    return buf.getvalue()


def test_archive_reads_text_files_only_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data = archive({"jobs.csv": "url\nhttps://jobs.lever.co/acme\n", "../../evil.txt": "https://jobs.ashbyhq.com/beta", "run.py": "https://jobs.lever.co/notread", "pic.png": b"\x89PNG https://jobs.lever.co/nope"})
    found = kg.boards_in_archive(data, "src")
    assert sorted(k[1] for k in found) == ["acme", "beta"]
    assert list(tmp_path.iterdir()) == []


def test_archive_catches_a_url_split_across_chunks(monkeypatch):
    monkeypatch.setattr(kg, "CHUNK_CHARS", 20)
    found = kg.boards_in_archive(archive({"a.txt": "x" * 15 + " https://jobs.lever.co/splitco end"}), "src")
    assert ("lever", "splitco") in found


class Binary(FakeResponse):
    def __init__(self, content):
        super().__init__(200, "", "u")
        self.content = content


def test_kaggle_reads_only_permissive_datasets_under_the_size_limit(tmp_path):
    from urllib.parse import urlencode
    url = f"{kg.LIST}?{urlencode({'search': kg.QUERIES[0], 'page': 1, 'sortBy': 'hottest'})}"
    items = [{"ref": "a/open", "licenseName": "CC0: Public Domain", "totalBytes": 10}, {"ref": "b/sa", "licenseName": "CC BY-SA 4.0", "totalBytes": 10},
             {"ref": "c/big", "licenseName": "CC0: Public Domain", "totalBytes": kg.MAX_DATASET_BYTES + 1}]
    c = client({url: FakeResponse(200, json.dumps(items)), kg.DOWNLOAD + "a/open": Binary(archive({"x.csv": "https://jobs.ashbyhq.com/openco"}))})
    out = kg.harvest(tmp_path, c)
    assert out == {"datasets_read": 1, "skipped_for_license": 1, "skipped_for_size": 1, "boards": 1}
    assert kg.DOWNLOAD + "b/sa" not in c.session.calls and kg.DOWNLOAD + "c/big" not in c.session.calls


def test_new_sources_are_registered():
    names = {m.META.name for m in SOURCES}
    assert {pd.META.name, "vc_boards", "kaggle_datasets"} <= names
