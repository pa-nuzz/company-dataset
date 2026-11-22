import pytest

from company_dataset.identity import (
    CONFIRMED, HOMEPAGE, MISMATCH, SOURCE_LIST, STRUCTURED, UNRESOLVED, VENDOR_API, VENDOR_PAGE,
    NameClaim, cluster_companies, registrable_domain, resolve_identity,
)
from company_dataset.verification import classify


@pytest.mark.parametrize("status,http,error,jobs,expected", [
    ("valid", 200, None, 4, "verified_live"),
    ("valid", 200, None, 0, "verified_live_zero_jobs"),
    ("invalid", 404, "board_not_found", None, "dead_removed"),
    ("invalid", 422, "board_not_found", None, "invalid_identifier"),
    ("unresolved", 400, "http_400", None, "invalid_identifier"),
    ("unresolved", 410, "http_410", None, "dead_removed"),
    ("unresolved", 429, "http_429", None, "rate_limited"),
    ("unresolved", 403, "http_403", None, "blocked_by_security"),
    ("unresolved", 405, "http_405", None, "blocked_by_security"),
    ("unresolved", 502, "http_502", None, "temporarily_unavailable"),
    ("unresolved", None, "request_error: HTTPSConnectionPool timeout", None, "temporarily_unavailable"),
    ("unresolved", None, "request_error: Exceeded 30 redirects", None, "inconclusive"),
    ("unresolved", 200, "total_found_zero", None, "inconclusive"),
    ("unresolved", 200, "exists_no_identity", None, "inconclusive"),
    ("unresolved", 200, "invalid_json", None, "inconclusive"),
    ("unresolved", 200, "company_identifier_mismatch", None, "identity_mismatch"),
])
def test_board_outcomes(status, http, error, jobs, expected):
    assert classify(status, http, error, jobs) == expected


def test_vendor_name_plus_agreeing_list_confirms():
    r = resolve_identity([NameClaim("Acme Robotics", VENDOR_API, "greenhouse_api"), NameClaim("Acme Robotics Inc", SOURCE_LIST, "latmay")])
    assert (r.status, r.name, r.basis) == (CONFIRMED, "Acme Robotics", ["vendor", "list"])


def test_vendor_name_alone_is_not_confirmed():
    r = resolve_identity([NameClaim("Acme Robotics", VENDOR_API)])
    assert r.status == UNRESOLVED and r.name == "Acme Robotics"


def test_vendor_contradicted_by_list_is_mismatch_unless_homepage_corroborates_vendor():
    claims = [NameClaim("Nimbus Labs", VENDOR_API), NameClaim("Stratus Analytics", SOURCE_LIST, "kalil")]
    assert resolve_identity(claims).status == MISMATCH
    # A rename: the company's own homepage names the vendor's name.
    renamed = resolve_identity(claims + [NameClaim("Nimbus Labs | Home", HOMEPAGE)])
    assert renamed.status == CONFIRMED and renamed.name == "Nimbus Labs"


def test_list_names_alone_never_confirm_even_when_several_agree():
    r = resolve_identity([NameClaim("Orbit Fab", SOURCE_LIST, "kaleb"), NameClaim("Orbit Fab", SOURCE_LIST, "kalil")])
    assert r.status == UNRESOLVED


def test_nameless_vendor_needs_two_independent_classes():
    # Rippling/Pinpoint/Workday: hosted page + homepage agree -> confirmed.
    ok = resolve_identity([NameClaim("Orbit Fab", VENDOR_PAGE, "jsonld_organization"), NameClaim("Orbit Fab — Space refueling", HOMEPAGE)])
    assert ok.status == CONFIRMED
    # Hosted page + list agree -> confirmed (independent sources), list alone -> not.
    assert resolve_identity([NameClaim("Orbit Fab", VENDOR_PAGE), NameClaim("Orbit Fab Inc", SOURCE_LIST)], code="ofab").status == CONFIRMED
    assert resolve_identity([NameClaim("Orbit Fab", SOURCE_LIST), NameClaim("Orbit Fab", STRUCTURED)]).status == CONFIRMED
    assert resolve_identity([NameClaim("Orbit Fab", SOURCE_LIST)]).status == UNRESOLVED


def test_board_labels_and_slug_like_names_are_not_identity_evidence():
    r = resolve_identity([NameClaim("Careers", VENDOR_PAGE), NameClaim("orbit-fab", SOURCE_LIST)], code="orbit-fab")
    assert r.status == UNRESOLVED and r.name is None


def test_registrable_domain():
    assert registrable_domain("https://careers.acme.co.uk/x") == "acme.co.uk"
    assert registrable_domain("https://www.acme.com") == "acme.com"
    assert registrable_domain("jobs.acme.com") == "acme.com"


def _r(name, website="", linkedin="", ats="greenhouse", code="x", verified=True):
    return {"name": name, "website_url": website, "website_verified": verified, "linkedin_url": linkedin, "ats_system": ats, "ats_code": code}


def test_clusters_require_strong_key_and_name_match():
    rows = [
        _r("Acme", "https://acme.com", code="acme"),
        _r("Acme Inc", "https://careers.acme.com", ats="lever", code="acme"),
        _r("Acme", "", code="acme2"),                                         # same name, no key: separate
        _r("Zenith", "https://acme.com", code="zenith"),                     # same domain, different name: separate
        _r("Talkdesk", "", "https://www.linkedin.com/company/talkdesk", code="td1"),
        _r("Talkdesk", "https://talkdesk.com", "https://www.linkedin.com/company/talkdesk/", ats="ashby", code="td2"),
        _r("Patch", "https://patch.io", code="patch1"),
        _r("Patch", "https://patch.com", ats="lever", code="patch2"),         # namesakes on different domains
        _r("Unverified", "https://u.com", code="u1", verified=False),
        _r("Unverified", "https://u.com", ats="lever", code="u2", verified=False),  # unverified websites don't merge
    ]
    groups = [sorted(rows[i]["ats_system"] + ":" + rows[i]["ats_code"] for i in g) for g in cluster_companies(rows)]
    assert ["greenhouse:acme", "lever:acme"] in groups
    assert ["ashby:td2", "greenhouse:td1"] in groups
    assert len(groups) == 8


def test_list_name_that_only_echoes_the_slug_is_not_evidence():
    # Regression: kaleb/kalil "zbf" contradicted the vendor's "ZIEMLICH BESTE FREUNDE GmbH".
    r = resolve_identity([NameClaim("ZIEMLICH BESTE FREUNDE GmbH", VENDOR_API), NameClaim("zbf", SOURCE_LIST, "kaleb")], code="zbf")
    assert r.status == UNRESOLVED
    # Re-spacing or title-casing the slug adds nothing either.
    for listed in ("Attendi", "Orbit Fab", "Careeropportunities"):
        code = {"Attendi": "attendi", "Orbit Fab": "orbitfab", "Careeropportunities": "ecc.wd5/careeropportunities"}[listed]
        assert resolve_identity([NameClaim("Some Vendor Name", VENDOR_API), NameClaim(listed, SOURCE_LIST)], code=code).status == UNRESOLVED
    # A list naming the company beyond its code still counts.
    r = resolve_identity([NameClaim("Planned Parenthood Pacific", VENDOR_PAGE), NameClaim("Planned Parenthood Pacific Southwest", SOURCE_LIST)], code="pphp")
    assert r.status == CONFIRMED


def test_display_name_prefers_board_level_names_over_titles_and_legal_entities():
    r = resolve_identity([NameClaim("8974 DuPont Specialty Products", VENDOR_API, "workday_job_hiring_organization"),
                          NameClaim("DuPont", STRUCTURED, "wikidata")], code="dupont.wd5/jobs")
    assert (r.status, r.name) == (CONFIRMED, "DuPont")
    r = resolve_identity([NameClaim("Home ID-ware English", HOMEPAGE), NameClaim("ID-ware", STRUCTURED)], code="idw")
    assert r.name == "ID-ware"


def test_list_name_with_information_beyond_the_slug_corroborates_the_vendor():
    # Regression: these boards stayed unresolved because the name sits inside the code.
    for vendor, code in (("HubSpot", "hubspotjobs"), ("Interfold", "applytointerfold"), ("10a Labs", "10alabs"), ("Orbit Fab", "orbitfab")):
        r = resolve_identity([NameClaim(vendor, VENDOR_API), NameClaim(vendor, SOURCE_LIST, "lastroundai")], code=code)
        assert (r.status, r.name) == (CONFIRMED, vendor), code
        assert "list" in r.basis


def test_list_name_reproducible_from_the_slug_does_not_corroborate():
    # Title-casing or swapping separators needs no knowledge of the company.
    for name, code in (("Alcazar Energy", "alcazar-energy"), ("Foo Bar", "foo_bar"), ("Ether Fi", "ether.fi"), ("d-Matrix", "d-Matrix"),
                       ("Attendi", "attendi"), ("Careeropportunities", "ecc.wd5/careeropportunities")):
        r = resolve_identity([NameClaim(name, VENDOR_API), NameClaim(name, SOURCE_LIST, "joa")], code=code)
        assert r.status == UNRESOLVED, code
        assert "list" not in r.basis, code


def test_code_shaped_list_name_never_contradicts_the_vendor():
    # "d-Matrix" style lists next to a vendor tagline must not turn into a mismatch.
    for listed, code in (("Orbit Fab", "orbitfab"), ("Interfold", "applytointerfold")):
        r = resolve_identity([NameClaim("Some Vendor Tagline", VENDOR_API), NameClaim(listed, SOURCE_LIST, "kalil")], code=code)
        assert r.status == UNRESOLVED, code
        assert r.basis == ["vendor_only"], code
        assert r.conflicts == [], code
    # A list name that is not shaped like the code still contradicts.
    r = resolve_identity([NameClaim("Some Vendor Tagline", VENDOR_API), NameClaim("Another Company", SOURCE_LIST, "kalil")], code="orbitfab")
    assert r.status == MISMATCH


def test_code_shaped_list_name_cannot_confirm_without_a_vendor_name():
    # With no vendor record of who runs the board, a slug-shaped list name stays non-evidence.
    r = resolve_identity([NameClaim("Orbit Fab", HOMEPAGE, "homepage"), NameClaim("Orbit Fab", SOURCE_LIST, "kalil")], code="orbitfab")
    assert r.status == UNRESOLVED
