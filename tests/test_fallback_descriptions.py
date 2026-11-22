import json

import pytest

from conftest import FakeResponse, FakeSession

from company_dataset import fallback_descriptions as fd, store
from company_dataset.http import HttpClient

GOOD = "Acme builds warehouse robots for regional distribution centers across the whole of Europe."
PARA = "Acme Robotics was founded in 2015 to automate warehouses, and now serves distribution centers across Europe."


def client(script=None):
    return HttpClient(session=FakeSession(script or {}), sleep=lambda s: None, per_host_interval=0, max_attempts=1)


def target(ats="greenhouse", code="acme", name="Acme Robotics", website=None, jobs=3):
    return fd.Target(board_id=1, ats=ats, code=code, region="", name=name, website=website, jobs=jobs)


@pytest.mark.parametrize("html,expected", [
    (f"<h2>About Acme Robotics</h2><p>{PARA}</p><h2>The role</h2><p>You will do things.</p>", PARA),
    (f"<p><strong>About us</strong></p><p>{PARA}</p>", PARA),
    (f"<h3>Who we are</h3><p>{PARA}</p>", PARA),
    (f"<p>About Acme Robotics: {PARA}</p>", PARA),
    ("<h2>The role</h2><p>" + PARA + "</p>", None),
    ("<h2>About the role</h2><p>" + PARA + "</p>", None),
    ("<h2>About Acme Robotics</h2><p>Join us! We are hiring engineers for many open roles and positions across all of Europe.</p>", None),
    ("<h2>About us</h2><p>Some other firm entirely makes widgets for customers all around the wide world today.</p>", None),
])
def test_about_section(html, expected):
    assert fd.about_section(html, "Acme Robotics") == expected


@pytest.mark.parametrize("html,expected", [
    (f"<p>Short.</p><p>{PARA}</p>", PARA),
    ("<p>We use cookies to improve your experience on Acme Robotics, read our privacy policy to learn more about it.</p>" + f"<p>{PARA}</p>", PARA),
    ("<p>Acme Robotics is hiring for many open positions and you should apply today for these great job openings.</p>", None),
    ("<p>This paragraph is long enough but it never names the business that it is supposedly describing at all.</p>", None),
    ("<nav><p>" + PARA + "</p></nav>", None),
    ("<footer><p>" + PARA + "</p></footer>", None),
    ("<script>var x = '<p>" + PARA + "</p>';</script>", None),
])
def test_first_paragraph(html, expected):
    assert fd.first_paragraph(html, "Acme Robotics") == expected


def test_stage1_smartrecruiters_uses_the_structured_company_description():
    listing = "https://api.smartrecruiters.com/v1/companies/Bosch/postings?limit=1"
    detail = "https://api.smartrecruiters.com/v1/companies/Bosch/postings/99"
    c = client({listing: FakeResponse(200, json.dumps({"content": [{"id": "99"}]})),
                detail: FakeResponse(200, json.dumps({"jobAd": {"sections": {"companyDescription": {"text": f"<p>{GOOD}</p>"}}}}))})
    hit = fd.vendor_profile(c, target("smartrecruiters", "Bosch"))
    assert (hit.text, hit.method) == (GOOD, "smartrecruiters_company_description")


def test_stage1_smartrecruiters_skips_boards_without_jobs():
    c = client()
    assert fd.vendor_profile(c, target("smartrecruiters", "Bosch", jobs=0)) is None
    assert c.session.calls == []


def test_stage1_greenhouse_prefers_board_info_then_job_ad():
    info = "https://boards-api.greenhouse.io/v1/boards/acme"
    jobs = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
    c = client({info: FakeResponse(200, json.dumps({"content": f"<p>{GOOD}</p>"}))})
    assert fd.vendor_profile(c, target()).method == "greenhouse_board_info"
    c = client({info: FakeResponse(200, json.dumps({"content": ""})),
                jobs: FakeResponse(200, json.dumps({"jobs": [{"content": f"&lt;h2&gt;About Acme Robotics&lt;/h2&gt;&lt;p&gt;{PARA}&lt;/p&gt;"}]}))})
    hit = fd.vendor_profile(c, target())
    assert (hit.text, hit.method) == (PARA, "job_ad_about_section")


def test_stage1_lever_and_ashby_read_the_about_section_of_a_job_ad():
    lever = "https://api.lever.co/v0/postings/acme?mode=json&limit=3"
    c = client({lever: FakeResponse(200, json.dumps([{"description": f"<div>About Acme Robotics</div><div>{PARA}</div>", "additional": ""}]))})
    assert fd.vendor_profile(c, target("lever")).text == PARA
    ashby = "https://api.ashbyhq.com/posting-api/job-board/acme"
    c = client({ashby: FakeResponse(200, json.dumps({"jobs": [{"descriptionHtml": f"<h2>About us</h2><p>{PARA}</p>"}]}))})
    assert fd.vendor_profile(c, target("ashby")).text == PARA


def page(html, url):
    return FakeResponse(200, html, url)


def meta(text):
    return f"<title>Acme</title><meta property='og:description' content='{text}'>"


def test_cascade_order_vendor_then_meta_then_paragraph_then_board():
    site = "https://acme.com"
    info = "https://boards-api.greenhouse.io/v1/boards/acme"
    board = "https://job-boards.greenhouse.io/acme"
    # Meta on the homepage beats a paragraph on /about.
    c = client({site: page(meta(GOOD), site), site + "/about": page(f"<p>{PARA}</p>", site + "/about")})
    assert fd.cascade(c, target(website=site)).method == "homepage_meta"
    # No meta anywhere: the first clean paragraph, /about before the homepage.
    c = client({site: page("<title>Acme</title><p>Welcome to the site of a business that sells many fine goods to many happy people.</p>", site), site + "/about": page(f"<p>{PARA}</p>", site + "/about")})
    assert fd.cascade(c, target(website=site)).method == "about_page_paragraph"
    # Nothing on the site: text on the hosted board.
    c = client({info: FakeResponse(200, json.dumps({"content": ""})), board: page(f"<p>{PARA}</p>", board)})
    assert fd.cascade(c, target()).method == "board_page_paragraph"


def test_cascade_never_turns_a_board_title_into_a_description():
    board = "https://jobs.lever.co/acme"
    for html in ("<title>Acme Robotics</title><h1>Acme Robotics</h1>", "<h1>Job openings at Acme Robotics, a company with very many offices worldwide today</h1>",
                 "<meta name='description' content='Job openings at Acme Robotics, a company with very many offices worldwide'>"):
        c = client({board: page(html, board)})
        assert fd.cascade(c, target("lever")) is None


def test_cascade_ignores_a_site_that_redirected_elsewhere_or_is_parked():
    site = "https://acme.com"
    c = client({site: page(meta(GOOD), "https://another-brand.example")})
    assert fd.cascade(c, target(website=site)) is None
    c = client({site: page("<title>acme.com is for sale</title>" + f"<p>{PARA}</p>", site)})
    assert fd.cascade(c, target(website=site)) is None


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "s.db")
    yield c
    c.close()


def add_board(c, ats, code, status, name, *, outcome="verified_live", description=None):
    bid = store.board_id(c, ats, code)
    c.execute("INSERT INTO identities(board_id, run, status, name, basis, decided_at) VALUES (?,?,?,?,'vendor+list','t')", (bid, "r", status, name))
    c.execute("INSERT INTO verifications(board_id, checked_at, origin, outcome, jobs_count) VALUES (?,?,?,?,?)", (bid, "t", "r", outcome, 2))
    if status == "identity_confirmed":
        c.execute("INSERT INTO field_evidence(board_id, field, value, method, status, observed_at, run) VALUES (?,'name',?,'m','verified','t','r')", (bid, name))
    if description:
        c.execute("INSERT INTO field_evidence(board_id, field, value, method, status, observed_at, run) VALUES (?,'description',?,'m','verified','t','r')", (bid, description))
    return bid


def test_targets_are_live_named_boards_without_a_description(conn):
    add_board(conn, "greenhouse", "confirmedco", "identity_confirmed", "Confirmed Co")
    add_board(conn, "greenhouse", "openco", "identity_unresolved", "Open Co")
    add_board(conn, "greenhouse", "namelessco", "identity_unresolved", None)
    add_board(conn, "greenhouse", "hasco", "identity_confirmed", "Has Co", description=GOOD)
    add_board(conn, "greenhouse", "deadco", "identity_confirmed", "Dead Co", outcome="dead_removed")
    add_board(conn, "workable", "otherco", "identity_confirmed", "Other Co")
    assert sorted(t.code for t in fd.targets(conn, ["r"])) == ["confirmedco", "openco"]


def test_run_records_evidence_and_is_resumable(conn):
    bid = add_board(conn, "greenhouse", "acme", "identity_confirmed", "Acme Robotics")
    conn.commit()
    info = "https://boards-api.greenhouse.io/v1/boards/acme"
    stats = fd.run(conn, "fb", ["r"], client({info: FakeResponse(200, json.dumps({"content": f"<p>{GOOD}</p>"}))}), "2026-11-03T00:00:00Z", workers=1)
    assert stats == {"targets": 1, "recovered": {"greenhouse_board_info": 1}, "still_missing": 0}
    row = conn.execute("SELECT value, method, status, run FROM field_evidence WHERE board_id=? AND field='description' AND run='fb'", (bid,)).fetchone()
    assert tuple(row) == (GOOD, "greenhouse_board_info", "verified", "fb")
    second = client()
    assert fd.run(conn, "fb", ["r", "fb"], second, "2026-11-03T00:00:01Z", workers=1)["targets"] == 0
    assert second.session.calls == []


def test_targets_skip_open_boards_already_described_and_put_confirmed_first(conn):
    open_done = add_board(conn, "greenhouse", "aaadone", "identity_unresolved", "Done Co")
    conn.execute("INSERT INTO field_evidence(board_id, field, value, method, status, observed_at, run) VALUES (?,'description',?,'job_ad_about_section','verified','t','r')", (open_done, GOOD))
    slogan = add_board(conn, "greenhouse", "aaaslogan", "identity_unresolved", "Slogan Co")
    conn.execute("INSERT INTO field_evidence(board_id, field, value, method, status, observed_at, run) VALUES (?,'description',?,'board_page_meta','verified','t','r')", (slogan, "Making happier residents, one community at a time, all across the country."))
    add_board(conn, "greenhouse", "bbbopen", "identity_unresolved", "Open Co")
    add_board(conn, "greenhouse", "zzzconfirmed", "identity_confirmed", "Confirmed Co")
    assert [t.code for t in fd.targets(conn, ["r"])] == ["zzzconfirmed", "aaaslogan", "bbbopen"]


def test_targets_can_be_limited_to_confirmed_boards(conn):
    add_board(conn, "greenhouse", "confirmedco", "identity_confirmed", "Confirmed Co")
    add_board(conn, "greenhouse", "openco", "identity_unresolved", "Open Co")
    assert [t.code for t in fd.targets(conn, ["r"], confirmed_only=True)] == ["confirmedco"]
