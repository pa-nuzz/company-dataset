import asyncio
import json
import subprocess

import pytest

from conftest import FakeResponse, FakeSession

from company_dataset import store
from company_dataset.http import HttpClient
from company_dataset.sources import corporate_graph as cg, dns_cname, github_lists as gl, search_dorks as sd, slug_prober as prober, yc_slugs
from company_dataset.sources._harvest import boards_in_text


def client(script=None):
    return HttpClient(session=FakeSession(script or {}), sleep=lambda s: None, per_host_interval=0, max_attempts=1)


def test_boards_in_text_handles_breezy_subdomains_and_new_greenhouse_hosts():
    text = """| [Acme](https://acme.breezy.hr/p/1-x) | <a href="https://job-boards.greenhouse.io/beta/jobs/1">x</a>
    https://jobs.lever.co/gamma?lever-source=x, https://jobs.ashbyhq.com/delta/abc https://example.com/jobs https://jobs.lever.co/$company"""
    found = boards_in_text(text, "src")
    assert sorted((a, v[0]) for (a, _), v in found.items()) == [("ashby", "delta"), ("breezy", "acme"), ("greenhouse", "beta"), ("lever", "gamma")]


# ---- GitHub lists

def test_raw_url():
    assert gl.raw_url("https://github.com/o/r/blob/abc123/dir/a%20b.md") == "https://raw.githubusercontent.com/o/r/abc123/dir/a b.md"
    assert gl.raw_url("https://github.com/o/r") is None
    assert gl.raw_url("https://example.com/o/r/blob/x/y") is None


def test_github_harvest_reads_seeds_and_discovered_files_and_reports_dead_seeds(tmp_path):
    def search(path, **params):
        if path == "search/repositories":
            return {"items": [{"full_name": "o/list"}]}
        if params["page"] == 1 and params["q"].startswith('"jobs.lever.co"') and "extension:md" in params["q"]:
            return {"items": [{"html_url": "https://github.com/o/r/blob/main/x.md", "file_size": 100}, {"html_url": "https://github.com/o/huge/blob/main/x.md", "file_size": gl.MAX_FILE_BYTES + 1}]}
        return {"items": []}

    seed_ok, seed_dead = "https://raw.githubusercontent.com/a/b/main/README.md", "https://raw.githubusercontent.com/a/gone/main/README.md"
    c = client({seed_ok: FakeResponse(200, "https://jobs.lever.co/seedco"),
                "https://raw.githubusercontent.com/o/r/main/x.md": FakeResponse(200, "https://jobs.ashbyhq.com/foundco"),
                "https://raw.githubusercontent.com/o/list/HEAD/README.md": FakeResponse(200, "https://acme.breezy.hr")})
    out = gl.harvest(tmp_path, c, search=search, sleep=lambda s: None, seeds=[seed_ok, seed_dead])
    assert out["boards"] == 3
    assert out["seeds_unavailable"] == [seed_dead]
    assert not any("huge" in call for call in c.session.calls)
    records, _ = gl.parse(tmp_path / gl.META.name / "tokens.csv")
    assert sorted(r.code for r in records) == ["acme", "foundco", "seedco"]
    assert all(r.name is None for r in records)
    assert json.loads((tmp_path / gl.META.name / "report.json").read_text())["files_with_boards"] == 3


def test_github_harvest_resumes_and_survives_a_failed_search(tmp_path):
    seed = "https://raw.githubusercontent.com/a/b/main/README.md"
    gl.harvest(tmp_path, client({seed: FakeResponse(200, "https://jobs.lever.co/seedco")}), sleep=lambda s: None, seeds=[seed], use_search=False)

    def broken(path, **params):
        raise subprocess.CalledProcessError(1, "gh")

    second = client()
    assert gl.harvest(tmp_path, second, search=broken, sleep=lambda s: None, seeds=[seed])["boards"] == 1
    assert second.session.calls == []


# ---- Search

def test_search_refuses_to_run_without_a_key(tmp_path):
    c = client()
    with pytest.raises(sd.NoSearchBackend):
        sd.harvest(tmp_path, c, max_queries=5, env={})
    assert c.session.calls == []


def test_query_builder_generates_direct_and_dynamic_queries():
    queries = sd.build_queries(["W24"])
    assert queries[0] == "site:boards.greenhouse.io"
    assert 'intext:"Powered by Lever" -site:lever.co' in queries
    assert 'site:jobs.ashbyhq.com "fintech"' in queries
    assert 'site:jobs.lever.co "kubernetes"' in queries
    assert 'site:breezy.hr "W24"' in queries
    assert len(queries) == len(set(queries))


def test_search_spends_no_more_than_the_budget_and_keeps_the_key_out_of_files(tmp_path):
    env = {"SERPAPI_KEY": "SECRETKEY"}
    _, build = sd.backend_from_env(env)
    queries = sd.build_queries()
    first = build(queries[0], 0)
    embed_link = "https://acme.example/careers"
    c = client({first: FakeResponse(200, json.dumps({"organic_results": [{"link": "https://boards.greenhouse.io/acme"}, {"link": embed_link}]})),
                embed_link: FakeResponse(200, "<title>x</title><a href='https://jobs.lever.co/embedded'>Jobs</a>", embed_link)})
    out = sd.harvest(tmp_path, c, max_queries=2, env=env)
    assert out["api_requests"] == 2
    records, _ = sd.parse(tmp_path / sd.META.name / "tokens.csv")
    assert sorted(r.code for r in records) == ["acme", "embedded"]
    for p in (tmp_path / sd.META.name).iterdir():
        assert "SECRETKEY" not in p.read_text()


# ---- DNS

class FakeAnswer:
    def __init__(self, target):
        self.target = target


class FakeResolver:
    def __init__(self, records):
        self.records = records

    async def resolve(self, name, kind):
        if name not in self.records:
            raise dns_cname.dns.resolver.NXDOMAIN()
        return [FakeAnswer(self.records[name] + ".")]


@pytest.mark.parametrize("target,hit", [
    ("jobs.lever.co.", True), ("acme.breezy.hr", True), ("custom.smartrecruiters.com", True), ("notlever.co", False), ("lever.co.evil.com", False), ("x.trafficmanager.net", False),
])
def test_ats_target_matches_whole_labels(target, hit):
    assert bool(dns_cname.ats_target(target)) is hit


def test_chain_is_followed_and_stops_on_loops():
    resolver = FakeResolver({"careers.acme.com": "a.cdn.net", "a.cdn.net": "acme.breezy.hr", "loop.a.com": "loop.b.com", "loop.b.com": "loop.a.com"})
    assert asyncio.run(dns_cname.cname_chain(resolver, "careers.acme.com")) == ["a.cdn.net", "acme.breezy.hr"]
    assert asyncio.run(dns_cname.cname_chain(resolver, "loop.a.com")) == ["loop.b.com", "loop.a.com"]


def test_dns_harvest_reads_slug_from_target_or_from_the_page(tmp_path):
    resolver = FakeResolver({"careers.acme.com": "acme.breezy.hr", "jobs.beta.com": "jobs.lever.co", "careers.gamma.com": "x.trafficmanager.net"})
    page = FakeResponse(200, "<title>Beta</title><a href='https://jobs.lever.co/betainc'>Jobs</a>", "https://jobs.beta.com")
    c = client({"https://jobs.beta.com": page})
    out = dns_cname.harvest(tmp_path, ["acme.com", "beta.com", "gamma.com"], c, resolver=resolver)
    assert out == {"domains_checked": 3, "aliases_on_ats": 2, "boards": 2}
    records, _ = dns_cname.parse(tmp_path / dns_cname.META.name / "tokens.csv")
    assert sorted((r.ats, r.code) for r in records) == [("breezy", "acme"), ("lever", "betainc")]


# ---- Corporate graph

def test_variant_slugs_follow_each_vendors_identifier_style():
    lever = cg.variant_slugs("lever", "acme")
    assert "acme-uk" in lever and "acme_uk" in lever and "acmeuk" not in lever
    assert "acmeuk" in cg.variant_slugs("greenhouse", "acme")
    assert "acme_uk" not in cg.variant_slugs("breezy", "acme")
    assert len(lever) == len(cg.SUFFIXES) * 2


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "s.db")
    yield c
    c.close()


def test_stored_pages_yield_unknown_linked_and_redirected_boards(conn):
    store.board_id(conn, "lever", "known")
    store.put_page(conn, "https://a.example", {"status": "ok", "final_url": "https://a.example", "board_refs": ["lever:known", "ashby:newco", "workable:nope", "lever:$bad"]})
    store.put_page(conn, "https://careers.b.example", {"status": "ok", "final_url": "https://jobs.lever.co/moved", "board_refs": []})
    found = cg.stored_page_boards(conn, {("lever", "known")})
    assert sorted(found) == [("ashby", "newco"), ("lever", "moved")]
    assert found[("lever", "moved")]["evidence"].startswith("redirect:")


def test_variant_plan_uses_confirmed_boards_only_and_skips_known_slugs(conn):
    good = store.board_id(conn, "lever", "acme")
    bad = store.board_id(conn, "lever", "open")
    conn.execute("INSERT INTO identities(board_id, run, status, decided_at) VALUES (?,?,?,?)", (good, "r", "identity_confirmed", "t"))
    conn.execute("INSERT INTO identities(board_id, run, status, decided_at) VALUES (?,?,?,?)", (bad, "r", "identity_unresolved", "t"))
    plan = cg.plan_variants(conn, {("lever", "acme-uk")}, limit=10, seed=1)
    assert "acme-uk" not in plan["lever"] and "acme_uk" in plan["lever"]
    assert not any(s.startswith("open") for s in plan["lever"])
    assert len(plan["lever"]) == 10


def test_probe_records_a_redirect_to_another_board_as_a_hit(tmp_path):
    url = prober.probe_url("smartrecruiters", "acme")
    c = client({url: FakeResponse(200, "<title>Careers</title>", "https://careers.smartrecruiters.com/AcmeGroup/acme-jobs")})
    cg.run_probes(tmp_path, {"smartrecruiters": ["acme"]}, "variant", client_factory=lambda: c)
    assert cg.rebuild_tokens(tmp_path) == 2
    records, _ = cg.parse(tmp_path / cg.META.name / "tokens.csv")
    assert sorted(r.code for r in records) == ["AcmeGroup", "acme"]


def test_breezy_probe_treats_404_as_missing():
    url = prober.probe_url("breezy", "acme")
    assert url == "https://acme.breezy.hr/json"
    assert prober.probe(client({url: FakeResponse(404, "")}), "breezy", "acme") == "missing"


def test_related_companies_parse_wikidata_and_skip_failed_batches():
    def payload(*rows):
        return json.dumps({"results": {"bindings": [{"relatedLabel": {"value": n}, "site": {"value": s}} for n, s in rows]}})

    hosts = [f"h{i}.com" for i in range(cg.WIKIDATA_BATCH + 1)]
    from urllib.parse import quote
    first = cg.ENDPOINT + quote(cg.subsidiary_query(hosts[:cg.WIKIDATA_BATCH]))
    second = cg.ENDPOINT + quote(cg.subsidiary_query(hosts[cg.WIKIDATA_BATCH:]))
    c = client({first: FakeResponse(200, payload(("Sub Co", "https://sub.co"))), second: FakeResponse(500, "")})
    assert cg.related_companies(c, hosts) == [("Sub Co", "https://sub.co")]


def test_yc_companies_need_a_name_and_website():
    assert yc_slugs.companies([{"name": "A", "website": "https://a.co"}, {"name": "B", "website": ""}, {"website": "https://c.co"}]) == [("A", "https://a.co")]


def test_all_new_sources_are_registered_and_not_downloadable():
    from company_dataset.sources import SOURCES

    by_name = {m.META.name: m.META for m in SOURCES}
    for name in ("search_dorks", "github_lists", "dns_cname", "corporate_graph", "yc_slugs_2026_11_03"):
        assert by_name[name].downloadable is False


def test_yc_plan_for_one_vendor_skips_what_the_pilot_already_tried():
    companies = [(f"Co{i}", f"https://co{i}.com") for i in range(30)]
    pilot = prober.plan_slugs(companies, set(), limit=10, seed=1)
    rest = yc_slugs.remaining_plan(companies, set(), vendors=["ashby"], tried_per_vendor=10, seed=1)
    assert list(rest) == ["ashby"]
    assert not set(rest["ashby"]) & set(pilot["ashby"])
    assert len(rest["ashby"]) == 20


def test_yc_rejects_a_vendor_it_does_not_know():
    with pytest.raises(ValueError):
        yc_slugs.remaining_plan([], set(), vendors=["workday"], tried_per_vendor=0, seed=1)


def test_prober_run_can_keep_earlier_hits(tmp_path):
    out = tmp_path / prober.META.name
    out.mkdir()
    (out / "tokens.csv").write_text("ats,code,region,evidence\nlever,fly,,slug_probe\nashby,magic,,slug_probe\n")
    (out / "probe_report.json").write_text(json.dumps({"lever": {"tried": 5000}}))
    url = prober.probe_url("ashby", "newco")
    report = prober.run(tmp_path, {"ashby": ["newco"]}, client_factory=lambda: client({url: FakeResponse(200, "{}", url)}), keep_existing=True, report_key="ashby_full")
    assert report["ashby_full"]["live"] == 1 and report["lever"] == {"tried": 5000}
    records, _ = prober.parse(out / "tokens.csv")
    assert sorted(r.code for r in records) == ["fly", "magic", "newco"]
