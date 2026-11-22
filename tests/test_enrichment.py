"""Page parsing and value cleaning used by enrichment.

Fixtures mirror real markup (Lever footer, Ashby window.__appData) and the bad values
found in the October 2026 audit of the previous output.
"""
from __future__ import annotations

from conftest import FakeResponse, FakeSession

from company_dataset.enrichment import (
    Board,
    ashby_facts,
    greenhouse_facts,
    lever_facts,
    normalize_country_name,
    strip_html,
    ashby_organization,
    board_refs,
    careersite_status,
    clean_description,
    clean_name,
    fetch_page,
    homepage_description,
    homepage_supports,
    lever_home_page,
    linkedin_company_slugs,
    names_match,
    parse_page,
    site_name,
    summarize_page,
    vendor_websites,
)
from company_dataset.http import HttpClient

LEVER_PAGE = """<html><head><title>Deliverect</title></head><body>
<a href="https://jobs.lever.co/deliverect" class="main-header-logo"><img alt="Deliverect logo"></a>
<a href="https://www.linkedin.com/company/deliverecthq">LinkedIn</a>
<div class="main-footer"><a href="https://www.deliverect.com/en/careers/">Deliverect Home Page</a>
<a href="https://www.lever.co/job-seeker-support/">Jobs powered by</a></div></body></html>"""

ASHBY_PAGE = """<html><head><title>Bree Jobs</title></head><body><script nonce="x">
      window.__appData = {"environment":"production","organization":{"organizationId":"b6","name":"Bree","publicWebsite":"https://www.trybree.com/","hostedJobsPageSlug":"bree"},"posting":null};
      // trailing comment that broke a naive regex
</script></body></html>"""

ASHBY_NO_ORG = """<script>window.__appData = {"organization":null,"posting":null,"jobBoard":null};</script>"""


class TestLever:
    def test_home_page_link_is_the_website(self):
        assert lever_home_page(parse_page(LEVER_PAGE)) == "https://www.deliverect.com"

    def test_does_not_fall_back_to_arbitrary_links(self):
        html = '<a href="https://app.careerpuck.com/x">Apply</a><a href="https://fonts.googleapis.com/css">f</a>'
        assert lever_home_page(parse_page(html)) is None

    def test_malformed_home_page_rejected(self):
        for href in ("https://https:", "javascript:void(0);", "http://Beaver Process Equipment"):
            html = f'<a href="{href}">Acme Home Page</a>'
            assert lever_home_page(parse_page(html)) is None, href


class TestAshby:
    def test_organization_parsed_despite_trailing_comment(self):
        org = ashby_organization(ASHBY_PAGE)
        assert org["name"] == "Bree"
        assert org["publicWebsite"] == "https://www.trybree.com/"

    def test_missing_organization(self):
        assert ashby_organization(ASHBY_NO_ORG) is None
        assert ashby_organization("<html></html>") is None


class TestLinkedin:
    def test_company_slugs_in_order_and_deduped(self):
        html = ('<a href="https://www.linkedin.com/company/acme/">x</a>'
                '<a href="https://linkedin.com/company/ACME?trk=1">y</a>'
                '<a href="https://www.linkedin.com/in/jane">person</a>'
                '<a href="https://uk.linkedin.com/company/partner-co">z</a>')
        assert linkedin_company_slugs(parse_page(html)) == ["acme", "partner-co"]


class TestDescription:
    def test_meta_with_apostrophe_and_entities_kept_whole(self):
        html = '<meta name="description" content="Acme is the world&#39;s leading maker of anvils &amp; rockets for coyotes.">'
        assert homepage_description(parse_page(html)) == "Acme is the world's leading maker of anvils & rockets for coyotes."

    def test_og_description_fallback(self):
        html = '<meta property="og:description" content="Acme builds payroll software for small businesses.">'
        assert homepage_description(parse_page(html)) == "Acme builds payroll software for small businesses."

    def test_boilerplate_rejected(self):
        for text in (
            "null",
            "Test Job Board",
            "We are looking for our next great team members. Take a look at our open positions below.",
            "Our Pre-Employment Stages: Submit a recent copy of your resume if you meet the minimum requirements.",
            "Please enable JavaScript to view this website properly and continue browsing.",
            "Join our growing team of individuals dedicated to collaboration and life-long learning.",
            "Diligent is a place where you will learn and grow. This is a rare opportunity to join a team.",
            "At Thunes, our customers come from all walks of life and so do we. We hire great people.",
            "We’re seeking thinkers and doers with a passion for progress at our agency.",
            "Join Tripledot Studios, a leading mobile game studio in London. Explore careers.",
        ):
            assert clean_description(text) is None, text

    def test_real_descriptions_mentioning_cookies_or_teams_kept(self):
        assert clean_description("Find fresh-baked Crumbl cookies near you. 6 classics plus new weekly flavors.")
        assert clean_description("We are building Helsing: a software company that keeps our democracies from harm.")

    def test_about_label_stripped_and_long_text_cut_at_sentence(self):
        text = "About Us: Acme makes anvils for the construction industry. " + "It ships worldwide to many customers. " * 30
        out = clean_description(text)
        assert out.startswith("Acme makes anvils")
        assert len(out) <= 600 and out.endswith(".")


class TestNames:
    def test_board_labels_rejected(self):
        for name in ("Open Roles", "Global Career Website", "AssureSoft - Careers", "Rothesay Graduates", "Robinhood Contractors", "Soho House via Hosco"):
            assert clean_name(name, "x") is None, name

    def test_slug_shaped_names_rejected_but_brands_kept(self):
        assert clean_name("rebel-space-technologies", "rebel-space-technologies") is None
        assert clean_name("sweetgreen", "sweetgreen") == "sweetgreen"
        assert clean_name("Acme Inc", "acme") == "Acme Inc"

    def test_test_and_demo_boards_detected_but_brands_kept(self):
        from company_dataset.enrichment import is_test_board

        for name, code in (("🔴 Sandbox", "sandbox"), ("Lever Demo 2", "leverdemo"), ("Sales Demo - Juliet Rausch", "salesdemo-jr"),
                           ("Merge API Integration Sandbox", "mergeapiintegrationsandbox"), ("testing", "testing"),
                           ("Staging Site Board", "staging"), ("[DO NOT USE FOR REFERRALS] ZipRecruiter Inc.", "zip"),
                           ("UiPath Workday Sandbox", "demo.uipath.com"), ("Example Corp", "examplecorpsandbox")):
            assert is_test_board(name, code), name
        for name, code in (("SandboxAQ", "sandboxaq"), ("Sandbox VR", "sandboxvr"), ("Sandboxx", "sandboxx"), ("#paid", "hashtagpaid"), ("Demostack", "demostack")):
            assert not is_test_board(name, code), name

    def test_auxiliary_boards_detected(self):
        from company_dataset.enrichment import is_auxiliary_board

        for name in ("Coupang Internal", "AlphaSense - for Referrals", "Buildkite Talent Community", "Bloomerang - General Application",
                     "Talent Pool", "Home Chef (Internal Employees)", "Sprout General Referrals"):
            assert is_auxiliary_board(name), name
        for name in ("Tester", "QA Wolf", "Democracy Forward", "Talentful"):
            assert not is_auxiliary_board(name), name

    def test_mojibake_repaired(self):
        from company_dataset.enrichment import fix_mojibake

        assert fix_mojibake("KI-LÃ¶sungen fÃ¼r Unternehmen") == "KI-Lösungen für Unternehmen"
        assert fix_mojibake("Weâ€™re building") == "We’re building"
        assert fix_mojibake("plain text") == "plain text"
        assert fix_mojibake("un cabinet indÃ©pendant basÃ© Ã Rabat") == "un cabinet indépendant basé à Rabat"

    def test_dash_mojibake_and_invisible_marks_fixed(self):
        assert clean_description("AI-powered counter-drone defense systems. Edda â\x80\x94 fast, low-cost.") == \
            "AI-powered counter-drone defense systems. Edda — fast, low-cost."
        assert clean_description("Aegis ‌Ventures is a NYC-based venture studio that partners with founders.") == \
            "Aegis Ventures is a NYC-based venture studio that partners with founders."

    def test_is_english(self):
        from company_dataset.enrichment import is_english

        english = [
            "Real estate data management made beautifully simple.",
            "Squarepoint-Capital.com - quantitative investment manager",
            "Cogstate combines cognitive science, operational excellence, and digital innovations to simplify cognitive assessment.",
            "Café Nero serves great coffee across the UK and Ireland.",
        ]
        other = [
            "ReHire conecta al mejor talento con las mejores empresas. Headhunting y staffing.",
            "Die benutzerfreundliche, modulare und datengetriebene Softwareplattform für Kliniken.",
            "No Pagar.me, conectamos milhares de sites e aplicativos a toda a cadeia de pagamentos.",
            "Vi vil at det skal være like fantastisk å kjøpe en bil som det er å drømme om dem.",
            "42dot은 현대자동차그룹의 글로벌 소프트웨어 센터로서 그룹의 SDV (Software-Defined Vehicles)",
            "Pinkoi 販售設計商品、數位創作、體驗活動",
        ]
        assert all(is_english(t) for t in english)
        assert not any(is_english(t) for t in other)

    def test_unrepairable_mojibake_description_dropped(self):
        polish = "home.pl jest najwiÄ\x99kszym w Polsce dostawcÄ usÅ\x82ug internetowych dla firm."
        assert clean_description(polish) is None

    def test_decorations_and_invisible_marks_stripped(self):
        assert clean_name("*Owl Labs*", "owllabs") == "Owl Labs"
        assert clean_name("‎ConnectWise", "connectwise") == "ConnectWise"
        assert clean_name("[solidcore]", "solidcore") == "[solidcore]"

    def test_names_match_ignores_legal_suffix(self):
        assert names_match("Acme, Inc.", "ACME")
        assert not names_match("Acme", "Zenith")
        assert not names_match("AB", "AB")


class TestPages:
    def test_summary_keeps_only_derived_fields(self):
        entry = summarize_page("ok", "https://www.deliverect.com/", LEVER_PAGE)
        assert entry["lever_home_page"] == "https://www.deliverect.com"
        assert entry["linkedin_slugs"] == ["deliverecthq"]
        assert "html" not in entry

    def test_board_refs_from_embeds(self):
        html = '<iframe src="https://boards.greenhouse.io/embed/job_board?for=Acme"></iframe><a href="https://jobs.lever.co/acme/123">job</a>'
        assert set(board_refs(parse_page(html))) == {"greenhouse:acme", "lever:acme"}

    def test_site_name_prefers_og(self):
        assert site_name(parse_page('<meta property="og:site_name" content="Acme"><title>Home | Acme Corp</title>')) == "Acme"
        assert site_name(parse_page("<title>Acme Corp | Home</title>")) == "Acme Corp"

    def test_fetch_errors_are_not_final(self):
        session = FakeSession({"https://acme.com": ConnectionError("boom")})
        client = HttpClient(session=session, sleep=lambda s: None, per_host_interval=0, max_attempts=1)
        assert fetch_page(client, "https://acme.com")["status"] == "error"
        session = FakeSession({"https://acme.com": FakeResponse(404)})
        client = HttpClient(session=session, sleep=lambda s: None, per_host_interval=0, max_attempts=1)
        assert fetch_page(client, "https://acme.com")["status"] == "http_404"


def _board(ats, code="acme"):
    return Board(key=f"{ats}:{code}", ats=ats, code=code, region=None, careersite_url=f"https://board/{code}")


class TestBoardEvidence:
    def test_lever_404_page_is_dead_careersite(self):
        assert careersite_status(_board("lever"), {"status": "http_404"}) == "dead"
        assert careersite_status(_board("lever"), {"status": "error"}) == "unknown"
        assert careersite_status(_board("lever"), {"status": "ok"}) == "ok"

    def test_ashby_page_without_org_is_dead(self):
        assert careersite_status(_board("ashby"), {"status": "ok", "ashby_appdata": True, "ashby_org": None}) == "dead"

    def test_career_subdomain_tries_parent_first_and_blocks_platforms(self):
        sites = vendor_websites(_board("greenhouse"), {"website_url": "https://careers.acme.com"}, None)
        assert sites == [("https://acme.com", "greenhouse_job_url_origin_parent"), ("https://careers.acme.com", "greenhouse_job_url_origin")]
        assert vendor_websites(_board("greenhouse"), {"website_url": "https://app.careerpuck.com"}, None) == []

    def test_homepage_identity_by_board_link_or_name(self):
        board = _board("greenhouse")
        assert homepage_supports({"status": "ok", "board_refs": ["greenhouse:acme"]}, board, None)
        assert homepage_supports({"status": "ok", "site_name": "Acme", "board_refs": []}, board, "Acme Inc")
        assert not homepage_supports({"status": "ok", "site_name": "Zenith", "board_refs": []}, board, "Acme Inc")
        assert not homepage_supports({"status": "http_404"}, board, "Acme")


# --- facts extracted from vendor API payloads ---


def test_strip_html():
    assert strip_html("<p>Hello  <b>world</b></p>") == "Hello world"
    assert strip_html("<p>We are & build things.</p>") == "We are & build things."
    assert strip_html("") == ""
    assert strip_html(None) == ""


def test_greenhouse_facts():
    payload = {
        "jobs": [
            {"company_name": "Acme", "absolute_url": "https://acme.com/jobs?gh_jid=1"},
            {"company_name": "Acme", "absolute_url": "https://acme.com/careers?gh_jid=2"},
            {"company_name": "Acme", "absolute_url": "https://careers.acme.com/jobs?gh_jid=3"},
            {"company_name": "Acme", "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/4"},
        ],
        "meta": {"total": 4},
    }
    facts = greenhouse_facts(payload)
    assert facts["jobs_count"] == 4
    assert facts["company_name"] == "Acme"
    assert facts["website_url"] == "https://acme.com"


def test_greenhouse_facts_no_name_no_website():
    payload = {"jobs": [{"absolute_url": "https://job-boards.greenhouse.io/x/jobs/1"}], "meta": {"total": 1}}
    facts = greenhouse_facts(payload)
    assert facts["jobs_count"] == 1
    assert facts["company_name"] is None
    assert facts["website_url"] is None


def test_greenhouse_facts_empty_jobs():
    facts = greenhouse_facts({"jobs": [], "meta": {"total": 0}})
    assert facts["jobs_count"] == 0
    assert facts["company_name"] is None


def test_lever_facts_country():
    assert lever_facts([{"country": "US"}, {"country": None}])["country"] == "US"
    assert lever_facts([{"country": "US"}, {"country": "GB"}])["country"] is None
    facts = lever_facts([])
    assert facts == {"jobs_count": 0, "country": None}


def test_ashby_facts_strict_unanimity():
    canada = {"jobs": [
        {"address": {"postalAddress": {"addressCountry": "Canada"}}},
        {"address": {"postalAddress": {"addressCountry": "Canada"}}},
    ]}
    assert ashby_facts(canada)["country"] == "CA"

    mixed = {"jobs": [
        {"address": {"postalAddress": {"addressCountry": "Canada"}}},
        {"address": {"postalAddress": {"addressCountry": "Germany"}}},
    ]}
    assert ashby_facts(mixed)["country"] is None

    unmapped = {"jobs": [{"address": {"postalAddress": {"addressCountry": "Ruritania"}}}]}
    assert ashby_facts(unmapped)["country"] is None

    no_address = {"jobs": [{"address": None}, {}]}
    assert ashby_facts(no_address)["country"] is None


def test_normalize_country_name():
    assert normalize_country_name("USA") == "US"
    assert normalize_country_name("United States") == "US"
    assert normalize_country_name("United Kingdom") == "GB"
    assert normalize_country_name("UK") == "GB"
    assert normalize_country_name("CANADA") == "CA"
    assert normalize_country_name("") is None
    assert normalize_country_name("Atlantis") is None
    assert normalize_country_name(None) is None


def test_vendor_sandbox_and_seeded_candidate_boards_are_test_boards():
    from company_dataset.enrichment import is_test_board

    assert is_test_board("Sandbox: Millennium Hotels", "sandboxmillenniumhotels-demo")
    assert is_test_board("Sandbox: Artea Bankas", "x")
    assert is_test_board("ACME candidate 1", "acme")
    assert is_test_board("Whatever", "acme-demo")
    assert not is_test_board("Sandbox VR", "sandboxvr")
    assert not is_test_board("Sandbox Interactive", "sandboxinteractive")


def test_page_title_ignores_inline_svg_titles():
    html = ("<html><head><title>Squarepoint | Home</title></head><body>"
            "<svg><title>Squarepoint logo</title></svg><svg><title>Facebook</title></svg></body></html>")
    assert parse_page(html).title == "Squarepoint | Home"
    # A title that only appears after an icon is still the page's title.
    assert parse_page("<svg><title>Menu</title></svg><title>Acme</title>").title == "Acme"
    # Only the first document title counts.
    assert parse_page("<title>First</title><title>Second</title>").title == "First"
    # An entity split across parser callbacks still reads as one title.
    assert parse_page("<title>Tom &amp; Jerry</title>").title == "Tom & Jerry"
