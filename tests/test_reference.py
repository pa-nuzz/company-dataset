import csv
import gzip

from company_dataset.reference import Reference, build


def _write_bp(path, rows):
    with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["handle", "name", "website", "industry", "size", "type", "founded", "city", "state", "country_code"])
        w.writerows(rows)


def test_build_filters_to_wanted_domains_and_names(tmp_path):
    path = tmp_path / "bp.csv.gz"
    _write_bp(path, [
        ["company/acme", "Acme Inc", "acme.com", "", "", "", "", "", "", "US"],
        ["company/other", "Other", "other.com", "", "", "", "", "", "", "GB"],
        ["company/zen", "Zenith", "zenith.io", "", "", "", "", "", "", "CA"],
    ])
    ref = build(path, domains={"acme.com"}, names={"zenith"})
    assert set(ref.by_domain) == {"acme.com", "zenith.io"}
    assert ref.linkedin_slug("https://www.acme.com", "Acme") == "acme"
    assert ref.country("https://about.acme.com", None) == "US"


def test_ambiguous_domain_needs_matching_name():
    ref = Reference(by_domain={"pp.org": [
        {"name": "Planned A", "website": "pp.org", "handle": "company/a", "country": "US"},
        {"name": "Planned B", "website": "pp.org", "handle": "company/b", "country": "US"},
    ]})
    assert ref.linkedin_slug("https://pp.org", None) is None
    assert ref.linkedin_slug("https://pp.org", "Planned B") == "b"


def test_name_candidates_require_slug_agreement_and_uniqueness():
    ref = Reference(by_name={"atlas": [
        {"name": "Atlas", "website": "atlas.com", "handle": "", "country": "US"},
        {"name": "Atlas", "website": "atlasmoving.com", "handle": "", "country": "US"},
    ]})
    assert ref.name_candidates("Atlas", "atlas") == ["https://atlas.com"]
    assert ref.name_candidates("Atlas", "zzz") == []
    ref.by_name["atlas"].append({"name": "Atlas", "website": "atlas.co.uk", "handle": "", "country": "GB"})
    assert ref.name_candidates("Atlas", "atlas") == []


ROWS = [
    ["company/acme", "Acme Inc", "acme.com", "", "", "", "", "", "", "US"],
    ["company/acme-eu", "Acme Inc", "acme.eu", "", "", "", "", "", "", "DE"],
    ["company/acme2", "Acme Holdings", "acme.com", "", "", "", "", "", "", "GB"],
    ["company/nosite", "No Site Ltd", "", "", "", "", "", "", "", "GB"],
    ["company/quote", 'He said "hi", ok', "quote.io", "", "", "", "", "", "", "CA"],
    ["company/newline", "Line\nBreak Co", "newline.io", "", "", "", "", "", "", "US"],
    ["company/other", "Other", "other.com", "", "", "", "", "", "", "GB"],
]


def _snapshot(ref):
    return ref.by_domain, ref.by_name


def test_cached_index_returns_exactly_what_a_scan_returns(tmp_path):
    from company_dataset.enrichment import name_key

    path = tmp_path / "bp.csv.gz"
    _write_bp(path, ROWS)
    cases = [
        ({"acme.com"}, set()),
        (set(), {name_key("Acme Inc")}),
        ({"quote.io", "newline.io"}, {name_key("Other"), name_key("No Site Ltd")}),
        (set(), set()),
        ({"unknown.example"}, {"unknownkey"}),
    ]
    for domains, names in cases:
        assert _snapshot(build(path, domains, names, cache_dir=tmp_path / "cache")) == _snapshot(build(path, domains, names))


def test_second_build_does_not_read_the_gzip_again(tmp_path, monkeypatch):
    import company_dataset.reference as reference

    path = tmp_path / "bp.csv.gz"
    _write_bp(path, ROWS)
    first = build(path, {"acme.com"}, set(), cache_dir=tmp_path / "cache")

    def fail(*args, **kwargs):
        raise AssertionError("scanned the source file despite a cached index")

    monkeypatch.setattr(reference, "_scan", fail)
    assert _snapshot(build(path, {"acme.com"}, set(), cache_dir=tmp_path / "cache")) == _snapshot(first)


def test_changed_source_file_gets_a_new_index(tmp_path):
    path = tmp_path / "bp.csv.gz"
    _write_bp(path, ROWS)
    assert "acme.com" in build(path, {"acme.com"}, set(), cache_dir=tmp_path / "cache").by_domain
    _write_bp(path, [["company/new", "New Co", "new.com", "", "", "", "", "", "", "US"]])
    rebuilt = build(path, {"acme.com", "new.com"}, set(), cache_dir=tmp_path / "cache")
    assert list(rebuilt.by_domain) == ["new.com"]


def test_interrupted_index_build_leaves_no_usable_cache(tmp_path, monkeypatch):
    import pytest

    import company_dataset.reference as reference

    path = tmp_path / "bp.csv.gz"
    _write_bp(path, ROWS)

    def broken(_path):
        yield {"name": "x", "website": "x.com", "handle": "", "country": "", "key": "x"}
        raise OSError("disk full")

    with monkeypatch.context() as m:
        m.setattr(reference, "_scan", broken)
        with pytest.raises(OSError):
            build(path, {"x.com"}, set(), cache_dir=tmp_path / "cache")
    assert list((tmp_path / "cache").glob("*.duckdb")) == []
    assert "acme.com" in build(path, {"acme.com"}, set(), cache_dir=tmp_path / "cache").by_domain
