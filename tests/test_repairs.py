import csv
import json

import pytest

from conftest import FakeResponse, FakeSession

from company_dataset import repairs, store
from company_dataset.http import HttpClient
from company_dataset.identity import CONFIRMED, UNRESOLVED


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "s.db")
    c.execute("INSERT INTO sources(id, name, imported_at) VALUES (1, 'lastroundai', 't'), (2, 'kaleb', 't')")
    yield c
    c.close()


def board(c, ats, code, *, outcome="verified_live", jobs=3, provider=None, website=None, description=None, identity=None, basis="vendor_only", name=None, run="fullrun", lists=()):
    bid = store.board_id(c, ats, code)
    c.execute("INSERT INTO verifications(board_id, checked_at, origin, outcome, legacy_status, jobs_count, code_validated, provider_name, provider_website, provider_description)"
              " VALUES (?,?,?,?,?,?,?,?,?,?)", (bid, "2026-11-01T00:00:00Z", run, outcome, "valid", jobs, code, provider, website, description))
    if identity:
        c.execute("INSERT INTO identities(board_id, run, status, name, basis, conflicts, decided_at) VALUES (?,?,?,?,?,'','2026-11-01T00:00:00Z')", (bid, run, identity, name, basis))
    for i, (source, listed) in enumerate(lists):
        seq = c.execute("SELECT COUNT(*) FROM source_rows WHERE source_id=?", (source,)).fetchone()[0]
        c.execute("INSERT INTO source_rows(source_id, seq, row_ref, board_id, ats, code, name) VALUES (?,?,?,?,?,?,?)", (source, seq, f"r{i}", bid, ats, code, listed))
    return bid


def page(c, url, final=None, title="", status="ok", **extra):
    store.put_page(c, url, {"status": status, "final_url": final or url, "fetched_at": "2026-11-01T00:00:00Z", "title": title, **extra})


def identity_of(c, bid, run):
    return c.execute("SELECT status, name, basis FROM identities WHERE board_id=? AND run=?", (bid, run)).fetchone()


def test_backfill_marks_generic_redirect_boards_invalid_and_keeps_history(conn):
    gone = board(conn, "smartrecruiters", "1000mercis", outcome="verified_live_zero_jobs", jobs=0, identity=CONFIRMED, name="1000mercis")
    real = board(conn, "smartrecruiters", "10pearls", outcome="verified_live_zero_jobs", jobs=0, identity=CONFIRMED, name="10Pearls")
    jobs = board(conn, "smartrecruiters", "busy", identity=CONFIRMED, name="Busy")
    page(conn, "https://careers.smartrecruiters.com/1000mercis", "https://jobs.smartrecruiters.com", "SmartRecruiters Job Search")
    page(conn, "https://careers.smartrecruiters.com/10pearls", title="Careers at 10Pearls")
    page(conn, "https://careers.smartrecruiters.com/busy", "https://jobs.smartrecruiters.com")

    assert repairs.backfill_smartrecruiters_existence(conn, "fix", "2026-11-03T00:00:00Z") == {"invalidated": 1}

    assert store.latest_verifications(conn)[gone]["outcome"] == "invalid_identifier"
    assert conn.execute("SELECT COUNT(*) FROM verifications WHERE board_id=?", (gone,)).fetchone()[0] == 2
    assert identity_of(conn, gone, "fix")["status"] == "not_live"
    assert store.latest_verifications(conn)[real]["outcome"] == "verified_live_zero_jobs"
    assert store.latest_verifications(conn)[jobs]["outcome"] == "verified_live"
    # Re-running finds nothing new.
    assert repairs.backfill_smartrecruiters_existence(conn, "fix2", "2026-11-03T00:00:01Z") == {"invalidated": 0}


def test_reresolve_confirms_only_with_a_new_corroboration(conn):
    hubspot = board(conn, "greenhouse", "hubspotjobs", provider="HubSpot", identity=UNRESOLVED, name="HubSpot", lists=[(1, "HubSpot"), (2, "hubspotjobs")])
    echo = board(conn, "greenhouse", "attendi", provider="Attendi", identity=UNRESOLVED, name="Attendi", lists=[(2, "Attendi")])
    alone = board(conn, "greenhouse", "soloco", provider="Solo Co", identity=UNRESOLVED, name="Solo Co")
    dead = board(conn, "greenhouse", "goneco", outcome="dead_removed", jobs=None, provider="Gone Co", identity=UNRESOLVED, name="Gone Co", lists=[(1, "Gone Co")])
    other_ats = board(conn, "workable", "acmeco", provider="Acme Co", identity=UNRESOLVED, name="Acme Co", lists=[(1, "Acme Co")])

    stats = repairs.reresolve_identities(conn, "fix", ["fullrun"], "2026-11-03T00:00:00Z")

    assert stats["confirmed"] == 1
    assert stats["by_ats"] == {"greenhouse": 1}
    row = identity_of(conn, hubspot, "fix")
    assert (row["status"], row["name"]) == (CONFIRMED, "HubSpot")
    assert "list" in row["basis"]
    name = conn.execute("SELECT value, status, run FROM field_evidence WHERE board_id=? AND field='name'", (hubspot,)).fetchone()
    assert tuple(name) == ("HubSpot", "verified", "fix")
    for bid in (echo, alone, dead, other_ats):
        assert identity_of(conn, bid, "fix") is None


def test_reresolve_never_touches_confirmed_boards(conn):
    bid = board(conn, "greenhouse", "stripe", provider="Stripe", identity=CONFIRMED, name="Stripe", basis="vendor+homepage", lists=[(1, "Stripe")])
    assert repairs.reresolve_identities(conn, "fix", ["fullrun"], "2026-11-03T00:00:00Z")["confirmed"] == 0
    assert identity_of(conn, bid, "fix") is None


def test_reresolve_uses_the_stored_vendor_name_when_the_check_has_none(conn):
    # The hosted-page name was only kept on the identity row: raw pages are not stored.
    bid = board(conn, "smartrecruiters", "SSENSE1", outcome="verified_live_zero_jobs", jobs=0, identity=UNRESOLVED, name="SSENSE", lists=[(1, "SSENSE")])
    assert repairs.reresolve_identities(conn, "fix", ["fullrun"], "2026-11-03T00:00:00Z")["confirmed"] == 1
    assert identity_of(conn, bid, "fix")["name"] == "SSENSE"


def test_reresolve_does_not_treat_a_list_chosen_name_as_the_vendor(conn):
    bid = board(conn, "lever", "acmeworks", identity=UNRESOLVED, name="Acme Works", basis="insufficient_independent_evidence", lists=[(1, "Acme Works")])
    assert repairs.reresolve_identities(conn, "fix", ["fullrun"], "2026-11-03T00:00:00Z")["confirmed"] == 0
    assert identity_of(conn, bid, "fix") is None


def recovery(c, ats, code, website, *, method="reference", linkedin=None, **kw):
    bid = board(c, ats, code, identity=UNRESOLVED, **kw)
    c.execute("INSERT INTO identities(board_id, run, status, name, basis, conflicts, decided_at) VALUES (?,'recovery1',?,?,?,'','2026-11-01T01:00:00Z')",
              (bid, CONFIRMED, kw.get("name"), "provider_name+reference"))
    c.execute("INSERT INTO field_evidence(board_id, field, value, method, status, observed_at, run) VALUES (?,'website_url',?,?,'verified','t','recovery1')", (bid, website, method))
    if linkedin:
        c.execute("INSERT INTO field_evidence(board_id, field, value, method, status, observed_at, run) VALUES (?,'linkedin_url',?,'reference_linkedin','verified','t','recovery1')", (bid, linkedin))
    return bid


def test_reverify_keeps_a_site_that_names_the_company_and_adds_the_scheme(conn):
    bid = recovery(conn, "greenhouse", "buildops", "buildops.com", provider="BuildOps", name="BuildOps", linkedin="https://www.linkedin.com/company/buildops/")
    page(conn, "https://buildops.com", title="BuildOps | Commercial contractor software", description="BuildOps is the leading software platform for commercial contractors everywhere in the field.")
    stats = repairs.reverify_recovery(conn, "rv", "recovery1", HttpClient(session=FakeSession()), "2026-11-03T00:00:00Z")
    assert stats["kept"] == 1
    assert identity_of(conn, bid, "rv")["status"] == CONFIRMED
    fields = {r["field"]: r["value"] for r in conn.execute("SELECT field, value FROM field_evidence WHERE board_id=? AND run='rv' AND status='verified'", (bid,))}
    assert fields["website_url"] == "https://buildops.com"
    assert fields["linkedin_url"] == "https://www.linkedin.com/company/buildops"
    assert fields["description"].startswith("BuildOps is the leading")
    assert fields["name"] == "BuildOps"


def test_reverify_drops_a_namesake_domain_that_does_not_name_the_company(conn):
    # BigPicture's name join gave B-Stock a site that does not identify as B-Stock.
    bid = recovery(conn, "greenhouse", "bstocksolutions", "bstock.lk", provider="B-Stock", name="B-Stock")
    page(conn, "https://bstock.lk", title="Stock Management Lanka")
    stats = repairs.reverify_recovery(conn, "rv", "recovery1", HttpClient(session=FakeSession()), "2026-11-03T00:00:00Z")
    assert stats["kept"] == 0
    assert stats["dropped"]["homepage_does_not_name_company"] == 1
    assert identity_of(conn, bid, "rv") is None


def test_reverify_rejects_a_self_naming_site_with_nothing_tying_it_to_the_board(conn):
    # The page says "Apollo" and so does the vendor, but neither domain nor slug agree: a namesake.
    bid = recovery(conn, "greenhouse", "xyz123", "greekgods.example", provider="Apollo", name="Apollo")
    page(conn, "https://greekgods.example", title="Apollo")
    stats = repairs.reverify_recovery(conn, "rv", "recovery1", HttpClient(session=FakeSession()), "2026-11-03T00:00:00Z")
    assert stats["dropped"]["possible_namesake"] == 1
    assert identity_of(conn, bid, "rv") is None


def test_reverify_fetches_sites_never_checked_and_stores_the_page(conn):
    bid = recovery(conn, "lever", "fetchco", "fetchco.com", provider="FetchCo", name="FetchCo")
    html = "<html><head><title>FetchCo</title><meta name='description' content='FetchCo builds logistics software for regional freight carriers across the world.'></head></html>"
    session = FakeSession({"https://fetchco.com": FakeResponse(200, html, "https://fetchco.com")})
    stats = repairs.reverify_recovery(conn, "rv", "recovery1", HttpClient(session=session, sleep=lambda s: None, per_host_interval=0), "2026-11-03T00:00:00Z")
    assert session.calls == ["https://fetchco.com"]
    assert stats["fetched"] == 1
    assert json.loads(conn.execute("SELECT summary FROM pages WHERE url='https://fetchco.com'").fetchone()[0])["title"] == "FetchCo"
    assert identity_of(conn, bid, "rv") is not None


GOOD = "Acme builds warehouse robots for regional distribution centers across the whole of Europe."


def confirmed(c, ats, code, name, *, website=None, provider_description=None):
    bid = board(c, ats, code, provider=name, description=provider_description, identity=CONFIRMED, name=name, basis="vendor+list")
    c.execute("INSERT INTO field_evidence(board_id, field, value, method, status, observed_at, run) VALUES (?,'name',?,'m','verified','t','fullrun')", (bid, name))
    c.execute("INSERT INTO field_evidence(board_id, field, value, method, status, observed_at, run) VALUES (?,'ats_system',?,'m','verified','t','fullrun')", (bid, ats))
    c.execute("INSERT INTO field_evidence(board_id, field, value, method, status, observed_at, run) VALUES (?,'ats_code',?,'m','verified','t','fullrun')", (bid, code))
    if website:
        c.execute("INSERT INTO field_evidence(board_id, field, value, method, status, observed_at, run) VALUES (?,'website_url',?,'board_link+homepage_names_company','verified','t','fullrun')", (bid, website))
    return bid


def description_of(c, bid, run):
    return c.execute("SELECT value, method FROM field_evidence WHERE board_id=? AND run=? AND field='description'", (bid, run)).fetchone()


@pytest.mark.parametrize("text,accepted", [
    (GOOD, True),
    ("An Innovative and Tech-Powered Cross-Border Logistics Solutions company", True),
    ("Job openings at Planned Parenthood of Southern New England, Inc.", False),
    ("Grow your career in a supportive atmosphere and unlock your earning potential", False),
    ("Descubre nuestros vacantes disponibles en Centro America para ti", False),
    ("See all open roles at Acme Robotics and apply for the position today", False),
    ("Browse current vacancies and talent opportunities across our many sites", False),
])
def test_job_copy_is_rejected_from_board_and_careers_pages(text, accepted):
    assert repairs.usable_page_description(text) == (text if accepted else None)


def test_cached_recovery_prefers_vendor_text_then_homepage_then_board_page(conn):
    vendor = confirmed(conn, "greenhouse", "vendorco", "Vendor Co", provider_description=GOOD)
    home = confirmed(conn, "ashby", "homeco", "Home Co", website="https://homeco.com")
    page(conn, "https://homeco.com", title="Home Co", description=GOOD)
    meta = confirmed(conn, "breezy", "metaco", "Meta Co")
    page(conn, "https://metaco.breezy.hr", title="Meta Co", description=GOOD)
    filler = confirmed(conn, "lever", "fillerco", "Filler Co")
    page(conn, "https://jobs.lever.co/fillerco", title="Filler Co", description="Job openings at Filler Co, a company with many offices worldwide")
    nothing = confirmed(conn, "lever", "emptyco", "Empty Co")

    stats = repairs.recover_descriptions_cached(conn, "desc", ["fullrun"], "2026-11-03T00:00:00Z")

    assert stats["missing"] == 5
    assert stats["recovered"] == {"board_page_meta": 1, "homepage_meta": 1, "provider_description": 1}
    assert description_of(conn, vendor, "desc")["method"] == "provider_description"
    assert description_of(conn, home, "desc")["method"] == "homepage_meta"
    assert description_of(conn, meta, "desc")["method"] == "board_page_meta"
    assert description_of(conn, filler, "desc") is None
    assert description_of(conn, nothing, "desc") is None


def test_cached_recovery_leaves_boards_with_a_description_and_open_boards(conn):
    has = confirmed(conn, "greenhouse", "hasco", "Has Co", provider_description="Different text about this company and all of the things that it does.")
    conn.execute("INSERT INTO field_evidence(board_id, field, value, method, status, observed_at, run) VALUES (?,'description',?,'m','verified','t','fullrun')", (has, GOOD))
    unresolved = board(conn, "greenhouse", "openco", provider="Open Co", description=GOOD, identity=UNRESOLVED, name="Open Co")
    stats = repairs.recover_descriptions_cached(conn, "desc", ["fullrun"], "2026-11-03T00:00:00Z")
    assert stats["missing"] == 0
    assert description_of(conn, has, "desc") is None
    assert description_of(conn, unresolved, "desc") is None


def page_html(description):
    return f"<html><head><title>Net Co</title><meta name='description' content='{description}'></head></html>"


def network_client(script):
    return HttpClient(session=FakeSession(script), sleep=lambda s: None, per_host_interval=0, max_attempts=1)


def test_network_recovery_uses_the_homepage_first(conn):
    bid = confirmed(conn, "greenhouse", "netco", "Net Co", website="https://netco.com")
    client = network_client({"https://netco.com": FakeResponse(200, page_html(GOOD), "https://netco.com")})
    stats = repairs.recover_descriptions_network(conn, "desc", ["fullrun"], client, "2026-11-03T00:00:00Z")
    assert stats["recovered"] == {"homepage_meta": 1}
    assert description_of(conn, bid, "desc")["value"].startswith("Acme builds")
    assert client.session.calls == ["https://netco.com"]


def test_network_recovery_falls_back_to_about_and_careers_with_the_job_filter(conn):
    about = confirmed(conn, "greenhouse", "aboutco", "About Co", website="https://aboutco.com")
    careers = confirmed(conn, "greenhouse", "careersco", "Careers Co", website="https://careersco.com")
    jobs = confirmed(conn, "greenhouse", "jobsco", "Jobs Co", website="https://jobsco.com")
    client = network_client({
        "https://aboutco.com": FakeResponse(200, "<title>About Co</title>", "https://aboutco.com"),
        "https://aboutco.com/about": FakeResponse(200, page_html(GOOD), "https://aboutco.com/about"),
        "https://careersco.com": FakeResponse(200, "<title>Careers Co</title>", "https://careersco.com"),
        "https://careersco.com/careers": FakeResponse(200, page_html(GOOD), "https://careersco.com/careers"),
        "https://jobsco.com": FakeResponse(200, "<title>Jobs Co</title>", "https://jobsco.com"),
        "https://jobsco.com/careers": FakeResponse(200, page_html("Browse all current job openings at Jobs Co in our offices across the world"), "https://jobsco.com/careers"),
    })
    stats = repairs.recover_descriptions_network(conn, "desc", ["fullrun"], client, "2026-11-03T00:00:00Z")
    assert stats["recovered"] == {"about_page_meta": 1, "careers_page_meta": 1}
    assert description_of(conn, about, "desc")["method"] == "about_page_meta"
    assert description_of(conn, careers, "desc")["method"] == "careers_page_meta"
    assert description_of(conn, jobs, "desc") is None


def test_network_recovery_rejects_a_page_that_redirects_to_another_site(conn):
    bid = confirmed(conn, "greenhouse", "movedco", "Moved Co", website="https://movedco.com")
    client = network_client({"https://movedco.com": FakeResponse(200, page_html(GOOD), "https://otherbrand.example")})
    assert repairs.recover_descriptions_network(conn, "desc", ["fullrun"], client, "2026-11-03T00:00:00Z")["recovered"] == {}
    assert description_of(conn, bid, "desc") is None


def test_select_open_boards_covers_unresolved_and_confirmed_without_website_or_description(conn):
    from company_dataset.repairs import open_board_ids

    unresolved = board(conn, "lever", "openco", provider="Open Co", identity=UNRESOLVED, name="Open Co")
    bare = confirmed(conn, "lever", "bareco", "Bare Co")
    with_site = confirmed(conn, "lever", "siteco", "Site Co", website="https://siteco.com")
    dead = board(conn, "lever", "goneco", outcome="dead_removed", jobs=None, identity=UNRESOLVED, name="Gone Co")
    other = board(conn, "workable", "workco", identity=UNRESOLVED, name="Work Co")
    assert open_board_ids(conn, ["fullrun"]) == {unresolved, bare}
    assert with_site not in open_board_ids(conn, ["fullrun"])
    assert dead not in open_board_ids(conn, ["fullrun"]) and other not in open_board_ids(conn, ["fullrun"])


FRESH_JOBS = json.dumps({"jobs": [{"company_name": "Acme", "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/1"}], "meta": {"total": 1}})


def fresh_setup(c, code="acme", website="https://acme.com"):
    bid = confirmed(c, "greenhouse", code, code.title(), website=website)
    c.execute("INSERT INTO field_evidence(board_id, field, value, method, status, observed_at, run) VALUES (?,'description',?,'m','verified','t','fullrun')", (bid, GOOD))
    c.execute("INSERT INTO field_evidence(board_id, field, value, method, status, observed_at, run) VALUES (?,'careersite_url',?,'m','verified','t','fullrun')", (bid, f"https://job-boards.greenhouse.io/{code}"))
    return bid


def api(code, *responses):
    return {f"https://boards-api.greenhouse.io/v1/boards/{code}/jobs": list(responses)}


def run_fresh(c, script):
    from company_dataset.validate import AtsRateLimiter, BoardValidator

    client = HttpClient(session=FakeSession(script), sleep=lambda s: None, per_host_interval=0, max_attempts=1)
    return repairs.final_checks(c, "qa", ["fullrun"], client, BoardValidator(client, rate_limiter=AtsRateLimiter({})), "2026-11-03T00:00:00Z", workers=2), client


def rejected(c, bid):
    return {r["field"] for r in c.execute("SELECT field FROM field_evidence WHERE board_id=? AND run='qa' AND status='rejected'", (bid,))}


def test_final_checks_record_a_fresh_verification_and_leave_good_urls(conn):
    bid = fresh_setup(conn)
    stats, _ = run_fresh(conn, {**api("acme", FakeResponse(200, FRESH_JOBS)),
                                "https://job-boards.greenhouse.io/acme": FakeResponse(200, "<title>Acme</title>", "https://job-boards.greenhouse.io/acme"),
                                "https://acme.com": FakeResponse(200, "<title>Acme</title>", "https://acme.com")})
    assert stats["boards"] == 1
    assert stats["outcomes"] == {"verified_live": 1}
    latest = store.latest_verifications(conn)[bid]
    assert (latest["origin"], latest["outcome"]) == ("qa", "verified_live")
    assert rejected(conn, bid) == set()


def test_final_checks_reject_dead_and_parked_urls_but_not_blocked_ones(conn):
    dead = fresh_setup(conn, "deadsite", "https://deadsite.com")
    parked = fresh_setup(conn, "parkedsite", "https://parkedsite.com")
    blocked = fresh_setup(conn, "blockedsite", "https://blockedsite.com")
    script = {}
    for code in ("deadsite", "parkedsite", "blockedsite"):
        script.update(api(code, FakeResponse(200, FRESH_JOBS.replace("Acme", code))))
        script[f"https://job-boards.greenhouse.io/{code}"] = FakeResponse(200, f"<title>{code}</title>", f"https://job-boards.greenhouse.io/{code}")
    script["https://deadsite.com"] = FakeResponse(404, "", "https://deadsite.com")
    script["https://parkedsite.com"] = FakeResponse(200, "<title>parkedsite.com is for sale</title>", "https://parkedsite.com")
    script["https://blockedsite.com"] = FakeResponse(403, "", "https://blockedsite.com")
    run_fresh(conn, script)
    assert rejected(conn, dead) == {"website_url"}
    assert rejected(conn, parked) == {"website_url"}
    assert rejected(conn, blocked) == set()


def test_final_checks_retry_inconclusive_boards_and_record_dead_ones(conn):
    flaky = fresh_setup(conn, "flakyco", None)
    gone = fresh_setup(conn, "goneco2", None)
    stats, _ = run_fresh(conn, {
        **api("flakyco", FakeResponse(503, ""), FakeResponse(200, FRESH_JOBS)),
        **api("goneco2", FakeResponse(404, "")),
        "https://job-boards.greenhouse.io/flakyco": FakeResponse(200, "<title>Flaky</title>", "https://job-boards.greenhouse.io/flakyco"),
    })
    latest = store.latest_verifications(conn)
    assert latest[flaky]["outcome"] == "verified_live"
    assert latest[gone]["outcome"] == "dead_removed"
    assert stats["outcomes"] == {"dead_removed": 1, "verified_live": 1}


def test_final_checks_resume_without_rechecking_finished_boards(conn):
    bid = fresh_setup(conn, "doneco", None)
    conn.execute("INSERT INTO verifications(board_id, checked_at, origin, outcome, jobs_count) VALUES (?,?,?,?,?)", (bid, "2026-11-03T00:00:00Z", "qa", "verified_live", 1))
    conn.commit()
    stats, client = run_fresh(conn, {})
    assert stats["boards"] == 0
    assert client.session.calls == []


def test_final_checks_skip_rows_that_cannot_be_verified_anyway(conn):
    no_description = confirmed(conn, "greenhouse", "nodesc", "No Desc")
    stats, client = run_fresh(conn, {})
    assert stats["boards"] == 0
    assert no_description not in store.latest_verifications(conn) or client.session.calls == []


def named(conn, ats, code, name, basis, jobs=3, outcome="verified_live", provider=None, run="fullrun"):
    return board(conn, ats, code, outcome=outcome, jobs=jobs, provider=provider, identity=UNRESOLVED, name=name, basis=basis, run=run)


def promotion(conn, bid, run):
    return conn.execute("SELECT status, name, basis FROM identities WHERE board_id=? AND run=?", (bid, run)).fetchone()


def test_promote_vendor_confirms_live_vendor_named_boards(conn):
    live = named(conn, "greenhouse", "hubspotjobs", "HubSpot", "vendor_only")
    fallback = board(conn, "greenhouse", "insuffico", provider="Insuffico", identity=UNRESOLVED, name=None, basis="insufficient_independent_evidence")
    dead = named(conn, "greenhouse", "goneco", "Gone Co", "vendor_only", outcome="dead_removed", jobs=None)
    mismatch = named(conn, "greenhouse", "clashco", "Clash Co", "vendor_only", provider=None)  # a list contradicts, stay out
    conn.execute("UPDATE identities SET status='identity_mismatch' WHERE board_id=?", (mismatch,))
    untouched = board(conn, "lever", "listonlyco", identity=UNRESOLVED, name="List Only Co", basis="insufficient_independent_evidence")
    alread = board(conn, "smartrecruiters", "acmesr", identity=CONFIRMED, name="Acme SR", basis="vendor+list")

    stats = repairs.promote_vendor_identities(conn, "promo", ["fullrun"], "2026-11-03T00:00:00Z")

    assert stats["promoted"] == 2
    assert stats["by_ats"] == {"greenhouse": 2}
    assert stats["by_source"] == {"identity:vendor_only": 1, "provider_name": 1}
    assert (promotion(conn, live, "promo")["status"], promotion(conn, live, "promo")["name"]) == (CONFIRMED, "HubSpot")
    assert promotion(conn, fallback, "promo")["name"] == "Insuffico"
    assert promotion(conn, fallback, "promo")["basis"] == "vendor_promoted:provider_name"
    name = conn.execute("SELECT value, status, run FROM field_evidence WHERE board_id=? AND field='name'", (fallback,)).fetchone()
    assert name and name["status"] == "verified" and name["run"] == "promo"
    for bid in (dead, mismatch, untouched, alread):
        assert promotion(conn, bid, "promo") is None
    # Re-running with its own run as the source sees nothing open.
    assert repairs.promote_vendor_identities(conn, "promo2", ["promo"], "2026-11-03T00:05:00Z")["promoted"] == 0


def test_promote_vendor_skips_test_and_auxiliary_board_names(conn):
    testy = board(conn, "lever", "levertest", identity=UNRESOLVED, name="Lever Test 12345", basis="vendor_only")
    sandboxy = named(conn, "greenhouse", "democo", "Demo Sandbox", "vendor_only")
    aux = named(conn, "greenhouse", "talentco", "Talent Pool", "vendor_only")
    coming = named(conn, "greenhouse", "comingco", "Coming Soon Corp", "vendor_only")
    already = named(conn, "greenhouse", "goodco", "Good Co", "vendor_only")

    stats = repairs.promote_vendor_identities(conn, "promo", ["fullrun"], "2026-11-03T00:00:00Z")

    assert stats["promoted"] == 1
    assert stats["by_ats"] == {"greenhouse": 1}
    assert set(stats["skipped"]) == {"not_a_name", "test_board", "auxiliary_board", "placeholder_name"}
    for bid in (testy, sandboxy, aux, coming):
        assert promotion(conn, bid, "promo") is None
    assert (promotion(conn, already, "promo")["status"], promotion(conn, already, "promo")["name"]) == (CONFIRMED, "Good Co")


def test_promote_vendor_writes_an_even_sample_csv(conn, tmp_path):
    for i in range(5):
        named(conn, "ashby", f"code{i}", f"Co {i}", "vendor_only")
    out = tmp_path / "sample.csv"
    stats = repairs.promote_vendor_identities(conn, "promo", ["fullrun"], "2026-11-03T00:00:00Z", sample_out=out, sample_size=3)

    assert stats["promoted"] == 5
    rows = [r for r in csv.DictReader(open(out))]
    assert [r["ats_code"] for r in rows] == ["code0", "code2", "code4"]
    assert rows[0]["company_name"] == "Co 0"


def test_job_board_orgs_promotes_lever_and_ashby_boards_with_rented_job_boards(conn):
    ashby = board(conn, "ashby", "arenaio", identity=UNRESOLVED, name=None, basis="insufficient_independent_evidence", jobs=4)
    page(conn, "https://jobs.ashbyhq.com/arenaio", title="Jobs", ashby_org={"name": "Arena AI", "website": "https://arena.ai"})
    lever = board(conn, "lever", "acentarios", identity=UNRESOLVED, name=None, basis="insufficient_independent_evidence", jobs=2)
    page(conn, "https://jobs.lever.co/acentarios", title="Acntorios")
    no_jobs = board(conn, "ashby", "quietorg", identity=UNRESOLVED, name=None, basis="insufficient_independent_evidence", jobs=0)
    page(conn, "https://jobs.ashbyhq.com/quietorg", title="Jobs", ashby_org={"name": "Quiet Org"})
    vendor_named = named(conn, "ashby", "bestco", "Best Co", "vendor_only", jobs=7)

    stats = repairs.promote_job_board_orgs(conn, "orgpromo", ["fullrun"], "2026-11-03T00:00:00Z")

    assert stats["promoted"] == 2
    assert stats["by_ats"] == {"ashby": 1, "lever": 1}
    assert promotion(conn, ashby, "orgpromo")["name"] == "Arena AI"
    assert promotion(conn, ashby, "orgpromo")["basis"] == "vendor_promoted:ashby_appdata_organization"
    assert promotion(conn, lever, "orgpromo")["name"] == "Acntorios"
    assert promotion(conn, no_jobs, "orgpromo") is None
    assert promotion(conn, vendor_named, "orgpromo") is None


def test_job_board_orgs_skip_unusable_page_identities(conn):
    page(conn, "https://jobs.ashbyhq.com/badorg", title="Jobs", ashby_org={"name": "Staging Sandbox Account"})
    bad = board(conn, "ashby", "badorg", identity=UNRESOLVED, name=None, basis="insufficient_independent_evidence", jobs=3)
    page(conn, "https://jobs.ashbyhq.com/nopage")
    nopage = board(conn, "ashby", "nopage", identity=UNRESOLVED, name=None, basis="insufficient_independent_evidence", jobs=3)
    slugname = board(conn, "lever", "samsproduct", identity=UNRESOLVED, name=None, basis="insufficient_independent_evidence", jobs=1)
    page(conn, "https://jobs.lever.co/samsproduct", title="samsproduct")

    stats = repairs.promote_job_board_orgs(conn, "orgpromo", ["fullrun"], "2026-11-03T00:00:00Z")

    assert stats["promoted"] == 0
    assert set(stats["skipped"]) == {"test_board", "no_page_identity", "not_a_name"}
    for bid in (bad, nopage, slugname):
        assert promotion(conn, bid, "orgpromo") is None


def test_promoted_board_recovery_description_from_vendor_record(conn):
    bid = named(conn, "greenhouse", "mandco", "Mand Co", "vendor_only", provider="Mand Co")
    conn.execute("INSERT INTO verifications(board_id, checked_at, origin, endpoint, outcome, legacy_status, jobs_count, code_validated, provider_name, provider_description)"
                 " VALUES (?,?,?,?,?,?,?,?,?,?)", (bid, "2026-11-02T00:00:00Z", "fullrun", f"https://api/{bid}", "verified_live", "valid", 1, "mandco", "Mand Co", GOOD))
    repairs.promote_vendor_identities(conn, "promo", ["fullrun"], "2026-11-03T00:00:00Z")
    stats = repairs.recover_descriptions_cached(conn, "desc", ["fullrun", "promo"], "2026-11-03T00:10:00Z")
    assert stats["recovered"] == {"provider_description": 1}
    assert description_of(conn, bid, "desc")["method"] == "provider_description"
