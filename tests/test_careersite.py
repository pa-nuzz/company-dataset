from company_dataset import careersite, wikidata

PAGE = """<html><head><title>Careers at Orbit Fab | Rippling</title>
<meta property="og:site_name" content="Rippling">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"JobPosting","title":"Engineer",
 "hiringOrganization":{"@type":"Organization","name":"Orbit Fab","url":"https://www.orbitfab.com",
 "sameAs":["https://www.linkedin.com/company/orbit-fab/"]}}</script></head>
<body><a href="https://www.orbitfab.com">Visit our website</a>
<a href="https://www.linkedin.com/company/orbit-fab">LinkedIn</a>
<a href="https://www.linkedin.com/in/some-person">Founder</a>
<a href="https://www.rippling.com/">Powered by Rippling</a>
<a href="https://twitter.com/orbitfab">x</a><a href="/jobs/123">Engineer</a>
<a href="https://careers.orbitfab.com/benefits">Benefits</a></body></html>"""


def test_extract_names_websites_and_company_linkedin_only():
    x = careersite.extract(PAGE, "https://ats.rippling.com/orbit-fab/jobs")
    names = careersite.board_page_names(x, "orbit-fab")
    assert ("Orbit Fab", "jsonld_organization") in names
    assert all(n != "Rippling" for n, _ in names)  # vendor brand filtered
    sites = [s for s, _, _ in x["websites"]]
    assert sites[0] == "https://www.orbitfab.com"
    assert "https://www.rippling.com" not in sites and not any("twitter" in s for s in sites)
    assert x["linkedin_slugs"] == ["orbit-fab"]  # person profiles never count


def test_title_name_patterns():
    assert careersite.title_name("Careers at Acme | Lever") == "Acme"
    assert careersite.title_name("Acme Careers") == "Acme"
    assert careersite.title_name("Jobs bei Müller GmbH") == "Müller GmbH"
    assert careersite.title_name("Home") is None


def test_rank_prefers_domain_matching_name_or_code_over_frequent_links():
    ranked = careersite.rank_websites([("https://shopify.com", "board_link", 9), ("https://acme.io", "board_link", 1)], "Acme", "acme")
    assert ranked[0][0] == "https://acme.io"


def test_page_without_structured_data_or_links_yields_nothing():
    x = careersite.extract("<html><title>Jobs</title></html>", "https://acme.bamboohr.com/careers")
    assert careersite.board_page_names(x, "acme") == [] and x["websites"] == [] and x["linkedin_slugs"] == []


def test_wikidata_parse_groups_by_host_and_item():
    payload = {"results": {"bindings": [
        {"u": {"value": "https://www.acme.com/"}, "item": {"value": "http://www.wikidata.org/entity/Q1"}, "itemLabel": {"value": "Acme"},
         "li": {"value": "acme-corp"}, "iso": {"value": "US"}},
        {"u": {"value": "https://acme.com"}, "item": {"value": "http://www.wikidata.org/entity/Q1"}, "itemLabel": {"value": "Acme"}},
    ]}}
    assert wikidata.parse(payload) == {"acme.com": [{"qid": "Q1", "label": "Acme", "linkedin": ["acme-corp"], "country": ["US"]}]}
    q = wikidata.query_for(["acme.com"])
    assert "<https://www.acme.com/>" in q and "<http://acme.com>" in q
