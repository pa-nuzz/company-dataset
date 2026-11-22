import csv
import json

from company_dataset.legacy.export import (
    collapse_companies,
    drop_conflicting_linkedin,
    drop_shared_descriptions,
    run_export,
)
from company_dataset.models import REQUIRED_FIELDS
from company_dataset.reference import Reference


def _dirs(tmp_path):
    dirs = {
        "data": tmp_path,
        "raw": tmp_path / "raw",
        "cache": tmp_path / "cache",
        "intermediate": tmp_path / "intermediate",
        "output": tmp_path / "output",
        "review": tmp_path / "review",
        "reports": tmp_path / "reports",
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    return dirs


def _write_candidates(dirs, rows):
    path = dirs["intermediate"] / "candidates.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ats", "code", "alt_codes", "region", "name", "name_source", "careersite_urls", "sources"])
        for row in rows:
            writer.writerow(row)


def _valid(ats, code, **kw):
    base = {
        "ats": ats, "code": code, "status": "valid", "http_status": 200, "jobs_count": 5,
        "company_name": None, "description": None, "website_url": None, "country": None,
        "country_basis": None, "region_answered": None, "code_validated": code, "error": None,
        "checked_at": "2026-10-17T00:00:00Z",
    }
    base.update(kw)
    return base


def _read(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


HOME = {
    "status": "ok", "final_url": "https://acme.com/", "site_name": "Acme", "title": "Acme",
    "description": "Acme makes anvils for the construction industry.",
    "linkedin_slugs": ["acme"], "board_refs": [],
}


def test_required_fields_is_the_real_seven_business_fields():
    assert tuple(REQUIRED_FIELDS) == (
        "name", "description", "linkedin_url", "website_url", "careersite_url", "ats_system", "ats_code",
    )


def test_complete_rows_in_companies_csv_incomplete_separate(tmp_path):
    dirs = _dirs(tmp_path)
    _write_candidates(dirs, [
        ["greenhouse", "acme", "", "", "Acme Inc", "lastroundai", "", "latmay"],
        ["greenhouse", "bare", "", "", "", "", "", "latmay"],
    ])
    cache = {
        "greenhouse:acme": _valid("greenhouse", "acme", company_name="Acme", website_url="https://acme.com"),
        "greenhouse:bare": _valid("greenhouse", "bare", company_name="Bare Co"),
    }
    (dirs["cache"] / "validation.json").write_text(json.dumps(cache))
    (dirs["cache"] / "pages.json").write_text(json.dumps({"https://acme.com": HOME}))

    stats = run_export(dirs, ref=Reference())

    complete = _read(dirs["output"] / "companies_complete.csv")
    assert list(complete[0]) == list(REQUIRED_FIELDS)
    assert [r["ats_code"] for r in complete] == ["acme"]
    row = complete[0]
    assert all(row[f] for f in REQUIRED_FIELDS)
    assert row["linkedin_url"] == "https://www.linkedin.com/company/acme"
    assert row["careersite_url"] == "https://job-boards.greenhouse.io/acme"
    incomplete = _read(dirs["output"] / "companies_incomplete.csv")
    assert list(incomplete[0]) == list(REQUIRED_FIELDS) + ["missing_fields"]
    assert incomplete[0]["ats_code"] == "bare"
    assert incomplete[0]["missing_fields"] == "description;linkedin_url;website_url"
    assert stats["complete_companies"] == 1
    detailed = _read(dirs["review"] / "companies_detailed.csv")
    assert [(r["ats_code"], r["complete"]) for r in detailed] == [("acme", "yes"), ("bare", "no")]
    assert detailed[0]["website_source"] == "greenhouse_job_url_origin"
    total = _read(dirs["output"] / "companies_total.csv")
    evidence_cols = ["http_status", "checked_at", "jobs_count", "endpoint", "verification_tier"]
    assert list(total[0]) == list(REQUIRED_FIELDS) + ["complete", "missing_fields"] + evidence_cols
    assert [(r["ats_code"], r["complete"], r["missing_fields"]) for r in total] == [("acme", "yes", ""), ("bare", "no", "description;linkedin_url;website_url")]
    assert sorted(f.name for f in dirs["output"].iterdir()) == ["companies_complete.csv", "companies_incomplete.csv", "companies_total.csv", "source_manifest.csv"]


def test_dead_careersite_and_invalid_boards_are_rejected(tmp_path):
    dirs = _dirs(tmp_path)
    _write_candidates(dirs, [
        ["lever", "siepe", "", "", "", "", "", "latmay"],
        ["lever", "deadco", "", "", "", "", "", "latmay"],
        ["ashby", "notyet", "", "", "", "", "", "latmay"],
    ])
    cache = {
        "lever:siepe": _valid("lever", "siepe", jobs_count=0),
        "lever:deadco": _valid("lever", "deadco", status="invalid", http_status=404, error="board_not_found"),
    }
    (dirs["cache"] / "validation.json").write_text(json.dumps(cache))
    (dirs["cache"] / "pages.json").write_text(json.dumps({"https://jobs.lever.co/siepe": {"status": "http_404"}}))

    stats = run_export(dirs, ref=Reference())

    assert stats["live_boards"] == 0
    reasons = sorted(r["reason"] for r in _read(dirs["review"] / "rejected_records.csv"))
    assert reasons == ["careersite_unavailable", "not_validated", "validation_failed"]


def _row(code, website, linkedin="", description="", ats="greenhouse", name=None, jobs=1):
    return {
        "name": name or code.title(), "description": description, "linkedin_url": linkedin,
        "website_url": website, "careersite_url": f"https://b/{code}", "ats_system": ats, "ats_code": code,
        "jobs_count": jobs, "linkedin_source": "x", "description_source": "x", "also_on_ats": "",
    }


def test_shared_linkedin_kept_only_where_it_matches():
    rows = [
        _row("charliehealth", "https://charliehealth.com", "https://www.linkedin.com/company/charlie-health"),
        _row("brighthire", "https://brighthire.com", "https://www.linkedin.com/company/charlie-health"),
    ]
    assert drop_conflicting_linkedin(rows) == 1
    assert rows[0]["linkedin_url"] and not rows[1]["linkedin_url"]


def test_template_description_shared_by_many_companies_dropped():
    text = "It takes a big heart to shape little minds and we love it."
    rows = [_row(f"school{i}", f"https://school{i}.com", description=text) for i in range(3)]
    assert drop_shared_descriptions(rows) == 3


def test_one_row_per_company_across_ats_and_boards():
    rows = [
        _row("robinhood", "https://robinhood.com", "https://www.linkedin.com/company/robinhood", "Robinhood is a brokerage app.", jobs=50),
        _row("robinhoodcontractors", "https://www.robinhood.com", jobs=3),
        _row("robinhood", "https://robinhood.com", ats="lever", jobs=1),
        _row("talkdesk2", "", "https://www.linkedin.com/company/talkdesk", name="Talkdesk"),
        _row("talkdesk", "https://talkdesk.com", "https://www.linkedin.com/company/talkdesk", ats="ashby", name="Talkdesk"),
        _row("acme-sub", "https://acmesub.com", "https://www.linkedin.com/company/acme", name="Zenith Sub"),
        _row("acme", "https://acme.com", "https://www.linkedin.com/company/acme", name="Acme"),
    ]
    kept, extras = collapse_companies(rows)
    assert sorted(r["ats_code"] for r in kept) == ["acme", "acme-sub", "robinhood", "talkdesk"]
    rh = next(r for r in kept if r["ats_code"] == "robinhood")
    assert rh["ats_system"] == "greenhouse"
    assert rh["also_on_ats"] == "lever"
    assert rh["other_boards"] == "greenhouse:robinhoodcontractors;lever:robinhood"
    assert next(r for r in kept if r["ats_code"] == "talkdesk")["other_boards"] == "greenhouse:talkdesk2"


def test_verification_endpoint_uses_correct_url_per_ats(tmp_path):
    """Test that _validation_endpoint returns the correct URL for each ATS."""
    dirs = _dirs(tmp_path)
    
    # Test with old-format cache (no code_validated)
    from company_dataset.legacy.export import _validation_endpoint
    
    # SmartRecruiters - uses original case token in API URL
    assert _validation_endpoint("smartrecruiters", "AbbVie", {}) == "https://api.smartrecruiters.com/v1/companies/AbbVie/postings?limit=100"
    
    # Lever - uses region if provided
    assert _validation_endpoint("lever", "acme", {}) == "https://api.lever.co/v0/postings/acme?mode=json"
    assert _validation_endpoint("lever", "acme", {"region_answered": "eu"}) == "https://api.eu.lever.co/v0/postings/acme?mode=json"
    
    # Greenhouse
    assert _validation_endpoint("greenhouse", "acme", {}) == "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
    
    # Ashby
    assert _validation_endpoint("ashby", "acme", {}) == "https://api.ashbyhq.com/posting-api/job-board/acme"
    
    # Breezy
    assert _validation_endpoint("breezy", "testbox", {}) == "https://testbox.breezy.hr/json"
    
    # code_validated overrides code
    assert _validation_endpoint("smartrecruiters", "AbbVie", {"code_validated": "AbbVie"}) == "https://api.smartrecruiters.com/v1/companies/AbbVie/postings?limit=100"
    assert _validation_endpoint("smartrecruiters", "AbbVie", {"code_validated": "other"}) == "https://api.smartrecruiters.com/v1/companies/other/postings?limit=100"


def test_verification_tier_rules(tmp_path):
    """Test verification_tier logic per Stage C rules."""
    dirs = _dirs(tmp_path)
    from company_dataset.legacy.export import _verification_tier
    from company_dataset.models import Candidate
    
    # Mock objects
    class MockBoard:
        def __init__(self, ats, code):
            self.ats = ats
            self.code = code
            self.careersite_url = ""
    
    class MockRes:
        def __init__(self, name=""):
            self.name = name
            self.country = None
    
    board_gh = MockBoard("greenhouse", "acme")
    board_lv = MockBoard("lever", "acme")
    board_ash = MockBoard("ashby", "acme")
    board_br = MockBoard("breezy", "testbox")
    board_sr = MockBoard("smartrecruiters", "abbvie")
    
    res_with_name = MockRes(name="TestBox")
    res_empty = MockRes(name="")
    
    # Candidate with name for matching
    cand_with_name = Candidate(ats="greenhouse", codes=["acme"], name="Acme")
    cand_no_name = Candidate(ats="greenhouse", codes=["acme"], name=None)
    cand_lever_eu = Candidate(ats="lever", codes=["acme"], region="eu", name="Acme")
    cand_lever_us = Candidate(ats="lever", codes=["acme"], region=None, name="Acme")
    
    # Tier A: jobs > 0 (no Lever region conflict)
    assert _verification_tier("greenhouse", {"status": "valid", "jobs_count": 5}, board_gh, res_empty, cand_with_name) == "A"
    assert _verification_tier("ashby", {"status": "valid", "jobs_count": 1}, board_ash, res_empty, cand_with_name) == "A"
    assert _verification_tier("smartrecruiters", {"status": "valid", "jobs_count": 10}, board_sr, res_empty, cand_with_name) == "A"
    assert _verification_tier("lever", {"status": "valid", "jobs_count": 3}, board_lv, res_empty, cand_lever_us) == "A"
    assert _verification_tier("breezy", {"status": "valid", "jobs_count": 2}, board_br, res_empty, cand_with_name) == "A"
    
    # Tier B: Lever with jobs > 0 where candidate region differs from region_answered
    # candidate region=EU, region_answered=US (None) -> mismatch -> B
    assert _verification_tier("lever", {"status": "valid", "jobs_count": 5, "region_answered": None}, board_lv, res_empty, cand_lever_eu) == "B"
    # candidate region=US, region_answered=EU -> mismatch -> B
    assert _verification_tier("lever", {"status": "valid", "jobs_count": 5, "region_answered": "eu"}, board_lv, res_empty, cand_lever_us) == "B"
    # candidate region=EU, region_answered=EU -> match -> A
    assert _verification_tier("lever", {"status": "valid", "jobs_count": 5, "region_answered": "eu"}, board_lv, res_empty, cand_lever_eu) == "A"
    # candidate region=US, region_answered=US (None) -> match -> A
    assert _verification_tier("lever", {"status": "valid", "jobs_count": 5, "region_answered": None}, board_lv, res_empty, cand_lever_us) == "A"
    # Lever with no candidate region info -> defaults to A (no conflict detected)
    assert _verification_tier("lever", {"status": "valid", "jobs_count": 5}, board_lv, res_empty) == "A"
    
    # Tier C: zero jobs with independent evidence
    # Breezy C only when hosted page confirmed identity
    # Breezy: a valid empty board already passed hosted-page identity in validation
    assert _verification_tier("breezy", {"status": "valid", "jobs_count": 0}, board_br, res_with_name, cand_with_name) == "C"
    assert _verification_tier("breezy", {"status": "valid", "jobs_count": 0}, board_br, res_empty, cand_with_name) == "C"
    assert _verification_tier("breezy", {"status": "valid", "jobs_count": 0}, board_br, MockRes(name="Breezy HR"), None) == "C"
    
    # Greenhouse C only if names_match(outcome company_name, candidate name) is true
    # If no candidate name, blank
    assert _verification_tier("greenhouse", {"status": "valid", "jobs_count": 0, "company_name": "Acme"}, board_gh, res_empty, cand_with_name) == "C"
    assert _verification_tier("greenhouse", {"status": "valid", "jobs_count": 0, "company_name": "Acme"}, board_gh, res_empty, cand_no_name) == ""
    assert _verification_tier("greenhouse", {"status": "valid", "jobs_count": 0}, board_gh, res_empty, cand_with_name) == ""
    # names_match with different name -> blank
    cand_different = Candidate(ats="greenhouse", codes=["acme"], name="Different")
    assert _verification_tier("greenhouse", {"status": "valid", "jobs_count": 0, "company_name": "Acme"}, board_gh, res_empty, cand_different) == ""
    
    # Lever/Ashby zero-job: blank
    assert _verification_tier("lever", {"status": "valid", "jobs_count": 0}, board_lv, res_empty, cand_with_name) == ""
    assert _verification_tier("ashby", {"status": "valid", "jobs_count": 0}, board_ash, res_empty, cand_with_name) == ""
    
    # SmartRecruiters zero-job: blank
    assert _verification_tier("smartrecruiters", {"status": "valid", "jobs_count": 0}, board_sr, res_empty, cand_with_name) == ""
    
    # No tier for unresolved or invalid
    assert _verification_tier("greenhouse", {"status": "unresolved", "jobs_count": 0}, board_gh, res_empty, cand_with_name) == ""
    assert _verification_tier("greenhouse", {"status": "invalid", "jobs_count": 0}, board_gh, res_empty, cand_with_name) == ""
    assert _verification_tier("breezy", {"status": "unresolved", "jobs_count": 0}, board_br, res_with_name, cand_with_name) == ""
    assert _verification_tier("lever", {"status": "invalid", "jobs_count": 5}, board_lv, res_empty, cand_with_name) == ""


def test_uncertain_and_dead_companies_csv_generated(tmp_path):
    """Test that uncertain_companies.csv and dead_companies.csv are generated with reason column."""
    dirs = _dirs(tmp_path)
    _write_candidates(dirs, [
        ["greenhouse", "validco", "", "", "ValidCo", "latmay", "", "latmay"],
        ["greenhouse", "unresolvedco", "", "", "UnresolvedCo", "latmay", "", "latmay"],
        ["greenhouse", "invalidco", "", "", "InvalidCo", "latmay", "", "latmay"],
        ["greenhouse", "notvalidatedco", "", "", "NotValidatedCo", "latmay", "", "latmay"],
        ["breezy", "breezyempty", "", "", "BreezyEmpty", "latmay", "", "latmay"],
    ])
    cache = {
        "greenhouse:validco": _valid("greenhouse", "validco", company_name="ValidCo", jobs_count=5),
        "greenhouse:unresolvedco": _valid("greenhouse", "unresolvedco", status="unresolved", error="total_found_zero"),
        "greenhouse:invalidco": _valid("greenhouse", "invalidco", status="invalid", http_status=404, error="board_not_found"),
        "breezy:breezyempty": _valid("breezy", "breezyempty", status="valid", jobs_count=0),
    }
    (dirs["cache"] / "validation.json").write_text(json.dumps(cache))
    (dirs["cache"] / "pages.json").write_text(json.dumps({
        "https://breezyempty.breezy.hr": {"status": "http_404"},
    }))

    stats = run_export(dirs, ref=Reference())

    # Check uncertain_companies.csv exists and has correct columns
    uncertain_path = dirs["review"] / "uncertain_companies.csv"
    assert uncertain_path.exists()
    uncertain = _read(uncertain_path)
    assert len(uncertain) >= 2  # unresolvedco + notvalidatedco
    assert "reason" in uncertain[0]
    assert "ats_system" in uncertain[0]
    assert "ats_code" in uncertain[0]
    assert "name" in uncertain[0]
    assert "careersite_url" in uncertain[0]
    
    reasons = {r["reason"] for r in uncertain}
    assert "total_found_zero" in reasons or "validation_unresolved" in reasons
    assert "not_validated" in reasons

    # Check dead_companies.csv exists and has correct columns
    dead_path = dirs["review"] / "dead_companies.csv"
    assert dead_path.exists()
    dead = _read(dead_path)
    assert len(dead) >= 1  # invalidco
    assert "reason" in dead[0]
    assert "ats_system" in dead[0]
    assert "ats_code" in dead[0]
    
    dead_reasons = {r["reason"] for r in dead}
    assert "board_not_found" in dead_reasons or "validation_failed" in dead_reasons

    # Verify specific reasons
    for row in uncertain:
        if row["ats_code"] == "unresolvedco":
            assert row["reason"] in ("total_found_zero", "validation_unresolved")
        if row["ats_code"] == "notvalidatedco":
            assert row["reason"] == "not_validated"
    
    for row in dead:
        if row["ats_code"] == "invalidco":
            assert row["reason"] in ("board_not_found", "validation_failed")


def test_run_dir_no_overwrite_check(tmp_path):
    """Test that --run-dir fails if the directory already exists."""
    from company_dataset.legacy.export import run_export
    
    # First run should succeed
    dirs1 = _dirs(tmp_path)
    _write_candidates(dirs1, [["greenhouse", "acme", "", "", "Acme", "latmay", "", "latmay"]])
    cache = {"greenhouse:acme": _valid("greenhouse", "acme", company_name="Acme", jobs_count=5)}
    (dirs1["cache"] / "validation.json").write_text(json.dumps(cache))
    (dirs1["cache"] / "pages.json").write_text(json.dumps({
        "https://job-boards.greenhouse.io/acme": {"status": "ok", "final_url": "https://acme.com/", "site_name": "Acme", "title": "Acme", "description": "Acme makes things.", "linkedin_slugs": ["acme"], "board_refs": []},
    }))
    
    run_dir = tmp_path / "run1"
    stats = run_export(dirs1, ref=Reference(), run_dir=str(run_dir))
    assert stats["live_companies_after_collapse"] == 1
    
    # Second run with same run_dir should fail (no-overwrite)
    import pytest
    with pytest.raises(FileExistsError):
        run_export(dirs1, ref=Reference(), run_dir=str(run_dir))


def test_default_mode_no_overwrite_check(tmp_path):
    """Test that default mode (no --run-dir) fails if any target file already exists."""
    from company_dataset.legacy.export import run_export
    
    # First run should succeed
    dirs1 = _dirs(tmp_path)
    _write_candidates(dirs1, [["greenhouse", "acme", "", "", "Acme", "latmay", "", "latmay"]])
    cache = {"greenhouse:acme": _valid("greenhouse", "acme", company_name="Acme", jobs_count=5)}
    (dirs1["cache"] / "validation.json").write_text(json.dumps(cache))
    (dirs1["cache"] / "pages.json").write_text(json.dumps({
        "https://job-boards.greenhouse.io/acme": {"status": "ok", "final_url": "https://acme.com/", "site_name": "Acme", "title": "Acme", "description": "Acme makes things.", "linkedin_slugs": ["acme"], "board_refs": []},
    }))
    
    stats = run_export(dirs1, ref=Reference())
    assert stats["live_companies_after_collapse"] == 1
    
    # Second run with same dirs (default mode) should fail (no-overwrite)
    import pytest
    with pytest.raises(FileExistsError):
        run_export(dirs1, ref=Reference())


def test_old_cache_compatibility_exports_with_new_evidence_columns(tmp_path):
    """Old-format validation.json (without company_identifier, code_validated, etc.) should
    still export correctly. The 9 original column values should be unchanged; only new
    evidence columns (http_status, checked_at, jobs_count, endpoint, verification_tier)
    are added."""
    dirs = _dirs(tmp_path)
    _write_candidates(dirs, [
        ["greenhouse", "acme", "", "", "Acme Inc", "lastroundai", "", "latmay"],
        ["lever", "leverco", "", "", "LeverCo", "latmay", "", "latmay"],
        ["ashby", "ashbyco", "", "", "AshbyCo", "latmay", "", "latmay"],
    ])
    # Old-format cache entries missing new fields: company_identifier, code_validated,
    # region_answered, country_basis, error, etc.
    cache = {
        "greenhouse:acme": {
            "ats": "greenhouse", "code": "acme", "status": "valid", "http_status": 200,
            "jobs_count": 5, "company_name": "Acme", "description": None,
            "website_url": "https://acme.com", "country": "US",
            "checked_at": "2026-10-17T00:00:00Z",
        },
        "lever:leverco": {
            "ats": "lever", "code": "leverco", "status": "valid", "http_status": 200,
            "jobs_count": 3, "company_name": "LeverCo", "description": None,
            "website_url": "https://lever.co", "country": "US",
            "region_answered": None, "checked_at": "2026-10-17T00:00:00Z",
        },
        "ashby:ashbyco": {
            "ats": "ashby", "code": "ashbyco", "status": "valid", "http_status": 200,
            "jobs_count": 2, "company_name": "AshbyCo", "description": None,
            "website_url": "https://ashby.com", "country": "GB",
            "checked_at": "2026-10-17T00:00:00Z",
        },
    }
    (dirs["cache"] / "validation.json").write_text(json.dumps(cache))
    (dirs["cache"] / "pages.json").write_text(json.dumps({
        "https://job-boards.greenhouse.io/acme": {"status": "ok", "final_url": "https://acme.com/", "site_name": "Acme", "title": "Acme", "description": "Acme makes things.", "linkedin_slugs": ["acme"], "board_refs": []},
        "https://jobs.lever.co/leverco": {"status": "ok", "final_url": "https://lever.co/", "site_name": "LeverCo", "title": "LeverCo", "description": "Lever makes things.", "linkedin_slugs": ["leverco"], "board_refs": []},
        "https://jobs.ashbyhq.com/ashbyco": {"status": "ok", "final_url": "https://ashby.com/", "site_name": "AshbyCo", "title": "AshbyCo", "description": "Ashby makes things.", "linkedin_slugs": ["ashbyco"], "board_refs": []},
    }))

    stats = run_export(dirs, ref=Reference())

    total = _read(dirs["output"] / "companies_total.csv")
    assert len(total) == 3

    # Check that original 9 REQUIRED_FIELDS + complete/missing are present and correct
    # The new evidence columns should be added
    required_plus = list(REQUIRED_FIELDS) + ["complete", "missing_fields", "http_status", "checked_at", "jobs_count", "endpoint", "verification_tier"]
    for row in total:
        for col in required_plus:
            assert col in row, f"Missing column {col} in total.csv"

# Verify evidence columns populated
        acme_row = next(r for r in total if r["ats_code"] == "acme")
        assert acme_row["http_status"] == "200"
        assert acme_row["checked_at"] == "2026-10-17T00:00:00Z"
        assert acme_row["jobs_count"] == "5"
        assert "boards-api.greenhouse.io/v1/boards/acme" in acme_row["endpoint"]
        assert acme_row["verification_tier"] in ("A", "B", "C", "")

    # Same for lever and ashby
    lever_row = next(r for r in total if r["ats_code"] == "leverco")
    assert lever_row["http_status"] == "200"
    assert "api.lever.co/v0/postings/leverco" in lever_row["endpoint"]

    ashby_row = next(r for r in total if r["ats_code"] == "ashbyco")
    assert ashby_row["http_status"] == "200"
    assert "api.ashbyhq.com/posting-api/job-board/ashbyco" in ashby_row["endpoint"]

    # Verify original REQUIRED_FIELDS columns are present (values may come from resolution)
    for row in total:
        for f in REQUIRED_FIELDS:
            assert f in row, f"Missing REQUIRED_FIELD {f}"

    # companies_detailed.csv should also have evidence columns
    detailed = _read(dirs["review"] / "companies_detailed.csv")
    for col in ["http_status", "checked_at", "jobs_count", "endpoint", "verification_tier"]:
        assert col in detailed[0], f"Missing {col} in detailed.csv"


def test_dead_and_uncertain_routing_uses_exact_reasons(tmp_path):
    """Substring matches must not route a reason: hosted_page_http_404 and http_404_unrecognized
    contain "404" but are unresolved, so they stay uncertain; only board_not_found is dead."""
    dirs = _dirs(tmp_path)
    _write_candidates(dirs, [
        ["greenhouse", "goneco", "", "", "", "", "", "latmay"],
        ["breezy", "hostedgone", "", "", "", "", "", "latmay"],
        ["breezy", "oddfour", "", "", "", "", "", "latmay"],
        ["lever", "throttled", "", "", "", "", "", "latmay"],
        ["ashby", "timeout", "", "", "", "", "", "latmay"],
        ["smartrecruiters", "ZeroCo", "", "", "", "", "", "latmay"],
    ])
    cache = {
        "greenhouse:goneco": _valid("greenhouse", "goneco", status="invalid", http_status=404, error="board_not_found"),
        "breezy:hostedgone": _valid("breezy", "hostedgone", status="unresolved", jobs_count=0, error="hosted_page_http_404"),
        "breezy:oddfour": _valid("breezy", "oddfour", status="unresolved", http_status=404, error="http_404_unrecognized"),
        "lever:throttled": _valid("lever", "throttled", status="unresolved", http_status=429, error="http_429"),
        "ashby:timeout": _valid("ashby", "timeout", status="unresolved", http_status=None, error="request_error: timed out"),
        "smartrecruiters:zeroco": _valid("smartrecruiters", "ZeroCo", status="unresolved", jobs_count=0, error="total_found_zero"),
    }
    (dirs["cache"] / "validation.json").write_text(json.dumps(cache))
    (dirs["cache"] / "pages.json").write_text(json.dumps({}))

    run_export(dirs, ref=Reference())

    dead = {r["ats_code"]: r["reason"] for r in _read(dirs["review"] / "dead_companies.csv")}
    uncertain = {r["ats_code"]: r["reason"] for r in _read(dirs["review"] / "uncertain_companies.csv")}
    assert dead == {"goneco": "board_not_found"}
    assert uncertain == {
        "hostedgone": "hosted_page_http_404",
        "oddfour": "http_404_unrecognized",
        "throttled": "http_429",
        "timeout": "request_error: timed out",
        "ZeroCo": "total_found_zero",
    }


def test_careersite_unavailable_valid_board_listed_in_dead_companies(tmp_path):
    dirs = _dirs(tmp_path)
    _write_candidates(dirs, [
        ["lever", "siepe", "", "", "", "", "", "latmay"],
        ["greenhouse", "acme", "", "", "Acme", "latmay", "", "latmay"],
    ])
    cache = {
        "lever:siepe": _valid("lever", "siepe", jobs_count=0),
        "greenhouse:acme": _valid("greenhouse", "acme", company_name="Acme"),
    }
    (dirs["cache"] / "validation.json").write_text(json.dumps(cache))
    (dirs["cache"] / "pages.json").write_text(json.dumps({"https://jobs.lever.co/siepe": {"status": "http_404"}}))

    run_export(dirs, ref=Reference())

    dead = _read(dirs["review"] / "dead_companies.csv")
    assert [(r["ats_system"], r["ats_code"], r["reason"], r["http_status"]) for r in dead] == [
        ("lever", "siepe", "careersite_unavailable", "200"),
    ]
    assert dead[0]["endpoint"] == "https://api.lever.co/v0/postings/siepe?mode=json"
    assert [r["ats_code"] for r in _read(dirs["output"] / "companies_total.csv")] == ["acme"]


def test_validate_sample_is_per_ats_and_deterministic_with_seed(tmp_path):
    from conftest import FakeSession

    from company_dataset.config import resolve_dirs
    from company_dataset.http import HttpClient
    from company_dataset.validate import AtsRateLimiter, run_validate

    def run(seed, sub):
        dirs = resolve_dirs(tmp_path / sub)
        lines = ["ats,code,alt_codes,region,name,name_source,careersite_urls,sources"]
        lines += [f"greenhouse,gh{i},,,,,,test" for i in range(20)]
        lines += [f"ashby,ab{i},,,,,,test" for i in range(20)]
        (dirs["intermediate"] / "candidates.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
        session = FakeSession({})
        client = HttpClient(session=session, sleep=lambda s: None, per_host_interval=0.0, max_attempts=1)
        stats = run_validate(dirs, client=client, sample=3, seed=seed, rate_limiter=AtsRateLimiter({}), max_workers=1)
        cache = json.loads((dirs["cache"] / "validation.json").read_text(encoding="utf-8"))
        return stats, sorted(cache)

    stats_a, keys_a = run(7, "a")
    _, keys_b = run(7, "b")
    _, keys_c = run(8, "c")
    assert stats_a["candidates_total"] == 6
    assert stats_a["by_ats"] == {"ashby": {"invalid": 3}, "greenhouse": {"invalid": 3}}
    assert keys_a == keys_b
    assert keys_a != keys_c


def test_smartrecruiters_case_variants_collapse_to_one_board(tmp_path):
    from company_dataset.legacy.build import merge_records
    from company_dataset.models import SourceRecord

    records = [
        SourceRecord(source="moonie", ats="smartrecruiters", code="AbbVie", row_ref="1"),
        SourceRecord(source="latmay", ats="smartrecruiters", code="abbvie", row_ref="2"),
        SourceRecord(source="kaleb", ats="smartrecruiters", code="ABBVIE", row_ref="3"),
    ]
    candidates, stats = merge_records(records)
    assert len(candidates) == 1
    assert candidates[0].key == "smartrecruiters:abbvie"
    assert candidates[0].codes == ["AbbVie", "abbvie", "ABBVIE"]
    assert stats.duplicate_rows == 2

    dirs = _dirs(tmp_path)
    _write_candidates(dirs, [["smartrecruiters", "AbbVie", "abbvie;ABBVIE", "", "", "", "", "moonie;latmay;kaleb"]])
    cache = {"smartrecruiters:abbvie": _valid(
        "smartrecruiters", "AbbVie", company_name="AbbVie", company_identifier="AbbVie", jobs_count=40,
    )}
    (dirs["cache"] / "validation.json").write_text(json.dumps(cache))
    (dirs["cache"] / "pages.json").write_text(json.dumps({}))

    run_export(dirs, ref=Reference())

    total = _read(dirs["output"] / "companies_total.csv")
    assert [(r["ats_system"], r["ats_code"]) for r in total] == [("smartrecruiters", "AbbVie")]
    assert total[0]["endpoint"] == "https://api.smartrecruiters.com/v1/companies/AbbVie/postings?limit=100"


def test_verification_tier_for_added_platforms():
    from company_dataset.legacy.export import _verification_tier
    from company_dataset.models import Candidate

    cand = Candidate(ats="workable", codes=["botpress"], name="Botpress")
    assert _verification_tier("workable", {"status": "valid", "jobs_count": 3}, None, None, cand) == "A"
    assert _verification_tier("workable", {"status": "valid", "jobs_count": 0, "company_name": "Botpress Technologies Inc."}, None, None, cand) == "C"
    assert _verification_tier("workable", {"status": "valid", "jobs_count": 0, "company_name": "Other Co"}, None, None, cand) == ""
    assert _verification_tier("rippling", {"status": "valid", "jobs_count": 0}, None, None, Candidate(ats="rippling", codes=["x"], name="X")) == ""
    assert _verification_tier("workday", {"status": "unresolved", "jobs_count": 5}, None, None, cand) == ""


def test_same_name_on_different_ats_never_merges_without_a_shared_key():
    # Regression: final4 merged lever/ashby/teamtailor/workable "proxima" on name alone.
    rows = [
        _row("proxima", "", ats="lever", name="Proxima"),
        _row("proxima", "", ats="ashby", name="Proxima"),
        _row("proxima", "", ats="teamtailor", name="Proxima"),
        _row("northwind", "https://northwind.com", ats="workable", name="Northwind Traders"),
        _row("northwind", "https://northwind.de", ats="breezy", name="Northwind Traders"),
    ]
    kept, extras = collapse_companies(rows)
    assert len(kept) == 5 and extras == []


def test_shared_domain_with_different_names_is_not_one_company():
    # Regression: one wrong website (nba.com) merged the Warriors, the Clippers and Kia Forum;
    # separately incorporated affiliates on one parent domain were collapsed.
    rows = [
        _row("goldenstate", "https://www.nba.com", name="Golden State Warriors"),
        _row("laclippers", "https://www.nba.com", name="LA Clippers"),
        _row("pphp", "https://www.plannedparenthood.org", ats="lever", name="Planned Parenthood Pacific"),
        _row("ppgnnj", "https://www.plannedparenthood.org", ats="lever", name="Planned Parenthood Greater Northern New Jersey"),
    ]
    kept, _ = collapse_companies(rows)
    assert len(kept) == 4


def test_same_domain_and_matching_name_is_one_company_including_subdomains():
    rows = [
        _row("acme", "https://acme.co.uk", name="Acme"),
        _row("acmeinterns", "https://careers.acme.co.uk", ats="lever", name="Acme Ltd"),
        _row("acmeus", "https://acme.com", name="Acme"),
    ]
    kept, extras = collapse_companies(rows)
    assert sorted(r["ats_code"] for r in kept) == ["acme", "acmeus"]
    assert [(r["ats_system"], r["ats_code"]) for r in extras] == [("lever", "acmeinterns")]


def test_zero_job_board_without_identity_tier_is_not_delivered(tmp_path):
    # Regression: 2,738 zero-job rows with no tier were exported (667 marked complete).
    dirs = _dirs(tmp_path)
    _write_candidates(dirs, [
        ["lever", "emptyco", "", "", "Empty Co", "kalil", "", "kalil"],
        ["lever", "busyco", "", "", "Busy Co", "kalil", "", "kalil"],
    ])
    cache = {
        "lever:emptyco": _valid("lever", "emptyco", jobs_count=0),
        "lever:busyco": _valid("lever", "busyco", jobs_count=2),
    }
    (dirs["cache"] / "validation.json").write_text(json.dumps(cache))
    (dirs["cache"] / "pages.json").write_text(json.dumps({}))

    run_export(dirs, ref=Reference())

    assert [r["ats_code"] for r in _read(dirs["output"] / "companies_total.csv")] == ["busyco"]
    uncertain = {r["ats_code"]: r["reason"] for r in _read(dirs["review"] / "uncertain_companies.csv")}
    assert uncertain["emptyco"] == "identity_not_established"


def test_valid_board_without_company_identity_is_uncertain_not_delivered(tmp_path):
    dirs = _dirs(tmp_path)
    _write_candidates(dirs, [
        ["rippling", "noname", "", "", "", "", "", "feashliaa"],
        ["rippling", "named", "", "", "Named Co", "kalil", "", "kalil"],
    ])
    cache = {
        "rippling:noname": _valid("rippling", "noname", jobs_count=3),
        "rippling:named": _valid("rippling", "named", jobs_count=3),
    }
    (dirs["cache"] / "validation.json").write_text(json.dumps(cache))
    (dirs["cache"] / "pages.json").write_text(json.dumps({}))

    stats = run_export(dirs, ref=Reference())

    assert [r["ats_code"] for r in _read(dirs["output"] / "companies_total.csv")] == ["named"]
    uncertain = _read(dirs["review"] / "uncertain_companies.csv")
    assert [(r["ats_code"], r["reason"], r["http_status"]) for r in uncertain] == [("noname", "identity_not_established", "200")]
    assert stats["post_validation_rejects"]["identity_not_established"] == 1
