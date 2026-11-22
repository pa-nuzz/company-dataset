from collections import Counter

from company_dataset.pilot import stratified_sample, verify_homepage


def _boards():
    out = []
    for i in range(80):
        out.append({"id": i, "ats": "lever", "code": f"a{i}", "prior": "verified_live" if i < 60 else "dead_removed", "sources": ["kalil"], "names": []})
    for i in range(80, 100):
        out.append({"id": i, "ats": "lever", "code": f"b{i}", "prior": "unchecked", "sources": ["latmay"], "names": []})
    out.append({"id": 100, "ats": "join", "code": "only", "prior": "unchecked", "sources": ["kalil"], "names": []})
    return out


def test_sample_is_proportional_per_ats_deterministic_and_takes_all_when_small():
    s1 = stratified_sample(_boards(), 10, seed=7)
    s2 = stratified_sample(_boards(), 10, seed=7)
    assert [b["id"] for b in s1] == [b["id"] for b in s2]
    lever = Counter(b["prior"] for b in s1 if b["ats"] == "lever")
    assert lever == {"verified_live": 6, "dead_removed": 2, "unchecked": 2}
    assert [b["code"] for b in s1 if b["ats"] == "join"] == ["only"]


def test_homepage_must_name_company_or_link_back_to_board():
    b = {"ats": "lever", "code": "acme"}
    assert verify_homepage({"status": "ok", "title": "Acme — Robots", "site_name": ""}, b, ["Acme"])
    assert verify_homepage({"status": "ok", "title": "Welcome", "board_refs": ["lever:acme"]}, b, [])
    assert verify_homepage({"status": "ok", "title": "Shopify", "site_name": "Shopify"}, b, ["Acme"]) is None
    assert verify_homepage({"status": "http_403"}, b, ["Acme"]) is None


def test_interleave_round_robins_across_ats():
    from company_dataset.pilot import interleave

    bs = [{"ats": "a", "i": 1}, {"ats": "a", "i": 2}, {"ats": "b", "i": 3}]
    assert [b["i"] for b in interleave(bs)] == [1, 3, 2]


def test_wilson_interval():
    from company_dataset.pilot_report import wilson

    lo, hi = wilson(30, 300)
    assert 0.07 < lo < 0.1 < hi < 0.14
    assert wilson(0, 0) == (0.0, 0.0)
