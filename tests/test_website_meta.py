import pytest

from company_dataset.website_meta import og_description, twitter_description, jsonld_org_description, about_meta, _first_linkedin


def test_og_description_prefers_og_tag():
    html = '<meta property="og:description" content="Software for teams"/>'
    assert og_description(html) == "Software for teams"


def test_og_description_unescapes_entities():
    assert og_description('<meta property="og:description" content="A &amp; B"/>') == "A & B"


def test_twitter_description():
    assert twitter_description('<meta name="twitter:description" content="Ops"/>') == "Ops"


def test_jsonld_org_description():
    html = '<script type="application/ld+json">{"@type":"Organization","description":"We build pipelines"}</script>'
    assert jsonld_org_description(html) == "We build pipelines"


def test_jsonld_in_graph_flattened():
    html = ('<script type="application/ld+json">{"@graph":[{"@type":"WebSite"},'
            '{"@type":"Organization","description":"Real engines"}]}</script>')
    assert jsonld_org_description(html) == "Real engines"


def test_jsonld_ignores_broken_json():
    assert jsonld_org_description('<script type="application/ld+json">{oops</script>') == ""


def test_about_meta_returns_desc_and_linkedin():
    html = '<meta property="og:description" content="About us"/><a href="https://www.linkedin.com/company/acme-inc/">in</a>'
    desc, li = about_meta("https://acme.io/about", html)
    assert desc == "About us"
    assert li == "https://www.linkedin.com/company/acme-inc/"


def test_linkedin_picks_first_company_slug():
    html = '<a href="https://linkedin.com/company/foo/"></a><a href="https://www.linkedin.com/school/bar/">x</a>'
    assert _first_linkedin(html, "https://x.io/") == "https://www.linkedin.com/company/foo/"


def test_no_descriptions_when_absent():
    assert og_description("") == "" and twitter_description("") == "" and jsonld_org_description("") == ""
