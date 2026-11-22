"""The deliverable: verified means the four mandatory fields are present on a confirmed, live board."""
import csv
import json

import pytest

from company_dataset import export, store
from company_dataset.export import apply_linkedin_policy, build_export

DESC = "Acme builds warehouse robots for regional distribution centers across the whole of Europe."
FRESH = "qa1"


def seed(conn, ats, code, name, *, status="identity_confirmed", outcome="verified_live", jobs=3, description=DESC, website=None, linkedin=None,
         careersite=None, run="r1", fresh=True, website_method="board_link+homepage_names_company"):
    bid = store.board_id(conn, ats, code)
    conn.execute("INSERT INTO identities(board_id, run, status, name, basis, decided_at) VALUES (?,?,?,?,'vendor+list','2026-11-01T00:00:00Z')", (bid, run, status, name))
    conn.execute("INSERT INTO verifications(board_id, checked_at, origin, endpoint, outcome, jobs_count) VALUES (?,?,?,?,?,?)", (bid, "2026-11-01T00:00:00Z", run, f"https://api/{code}", outcome, jobs))
    if fresh:
        conn.execute("INSERT INTO verifications(board_id, checked_at, origin, endpoint, outcome, jobs_count) VALUES (?,?,?,?,?,?)", (bid, "2026-11-03T00:00:00Z", FRESH, f"https://api/{code}", outcome, jobs))
    def ev(field, value, method="m"):
        conn.execute("INSERT INTO field_evidence(board_id, field, value, method, status, observed_at, run) VALUES (?,?,?,?,'verified','2026-11-01T00:00:00Z',?)", (bid, field, value, method, run))
    if status == "identity_confirmed":
        ev("name", name)
    ev("ats_system", ats)
    ev("ats_code", code)
    if description:
        ev("description", description, "homepage_meta")
    if website:
        ev("website_url", website, website_method)
    if linkedin:
        ev("linkedin_url", linkedin)
    if careersite:
        ev("careersite_url", careersite)
    return bid


def read(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "s.db")
    c.execute("INSERT INTO sources(id, name, url, license, rows_read, imported_at) VALUES (1, 'kalil', 'https://x', 'MIT', 2, 't')")
    yield c
    c.close()


def run(conn, out):
    conn.commit()
    return build_export(conn, ["r1", FRESH], out, fresh_origin=FRESH)


def test_verified_needs_only_the_four_mandatory_fields(conn, tmp_path):
    seed(conn, "greenhouse", "acme", "Acme")
    counts = run(conn, tmp_path / "out")
    rows = read(tmp_path / "out" / "companies_verified.csv")
    assert list(rows[0]) == ["company_name", "description", "ats_system", "ats_code", "linkedin_url", "website_url", "careersite_url"]
    assert rows[0]["company_name"] == "Acme"
    assert rows[0]["linkedin_url"] == rows[0]["website_url"] == rows[0]["careersite_url"] == ""
    assert counts["gates"]["passed"] is True


def test_exactly_the_seven_deliverable_files(conn, tmp_path):
    seed(conn, "greenhouse", "acme", "Acme")
    run(conn, tmp_path / "out")
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [
        "SHA256SUMS", "companies_incomplete.csv", "companies_verified.csv", "evidence.csv", "qa_report.json", "sources.json", "unresolved_boards.csv"]


def test_missing_description_goes_to_incomplete_with_the_reason(conn, tmp_path):
    seed(conn, "greenhouse", "acme", "Acme", description=None)
    run(conn, tmp_path / "out")
    assert read(tmp_path / "out" / "companies_verified.csv") == []
    row = read(tmp_path / "out" / "companies_incomplete.csv")[0]
    assert (row["company_name"], row["missing_fields"]) == ("Acme", "description")


def test_unresolved_boards_are_listed_separately(conn, tmp_path):
    seed(conn, "lever", "soloco", None, status="identity_unresolved")
    run(conn, tmp_path / "out")
    assert read(tmp_path / "out" / "companies_verified.csv") == read(tmp_path / "out" / "companies_incomplete.csv") == []
    assert read(tmp_path / "out" / "unresolved_boards.csv")[0]["ats_code"] == "soloco"


def test_only_target_ats_are_exported(conn, tmp_path):
    seed(conn, "workable", "acme", "Acme")
    counts = run(conn, tmp_path / "out")
    assert read(tmp_path / "out" / "companies_verified.csv") == []
    assert counts["boards_decided"] == 0


def test_zero_job_boards_stay_verified_and_their_count_is_in_the_evidence(conn, tmp_path):
    seed(conn, "greenhouse", "quiet", "Quiet Co", outcome="verified_live_zero_jobs", jobs=0)
    run(conn, tmp_path / "out")
    assert len(read(tmp_path / "out" / "companies_verified.csv")) == 1
    jobs = [r for r in read(tmp_path / "out" / "evidence.csv") if r["field"] == "jobs_count"]
    assert [(r["ats_code"], r["value"]) for r in jobs] == [("quiet", "0")]


def test_urls_are_normalized(conn, tmp_path):
    seed(conn, "greenhouse", "buildops", "BuildOps", website="buildops.com", linkedin="https://www.linkedin.com/company/buildops/", careersite="https://job-boards.greenhouse.io/buildops")
    run(conn, tmp_path / "out")
    row = read(tmp_path / "out" / "companies_verified.csv")[0]
    assert row["website_url"] == "https://buildops.com"
    assert row["linkedin_url"] == "https://www.linkedin.com/company/buildops"


def test_a_description_failing_the_current_rules_is_removed(conn, tmp_path):
    seed(conn, "greenhouse", "tiny", "Tiny Co", description="Hi there")
    run(conn, tmp_path / "out")
    assert read(tmp_path / "out" / "companies_incomplete.csv")[0]["missing_fields"] == "description"


def test_url_rejected_by_a_later_check_is_blanked(conn, tmp_path):
    bid = seed(conn, "greenhouse", "acme", "Acme", website="https://acme.com", careersite="https://job-boards.greenhouse.io/acme")
    conn.execute("INSERT INTO field_evidence(board_id, field, value, method, status, observed_at, run) VALUES (?,?,?,?,'rejected','2026-11-03T00:00:00Z',?)", (bid, "website_url", "https://acme.com", "fresh_check", FRESH))
    run(conn, tmp_path / "out")
    row = read(tmp_path / "out" / "companies_verified.csv")[0]
    assert row["website_url"] == ""
    assert row["careersite_url"] == "https://job-boards.greenhouse.io/acme"


def test_board_dead_on_the_fresh_check_leaves_the_verified_file(conn, tmp_path):
    bid = seed(conn, "greenhouse", "acme", "Acme", fresh=False)
    conn.execute("INSERT INTO verifications(board_id, checked_at, origin, endpoint, outcome) VALUES (?,?,?,?,?)", (bid, "2026-11-03T00:00:00Z", FRESH, "u", "dead_removed"))
    run(conn, tmp_path / "out")
    assert read(tmp_path / "out" / "companies_verified.csv") == []


def test_inconclusive_fresh_check_does_not_hide_the_live_answer_but_blocks_verified(conn, tmp_path):
    bid = seed(conn, "greenhouse", "acme", "Acme", fresh=False)
    conn.execute("INSERT INTO verifications(board_id, checked_at, origin, endpoint, outcome) VALUES (?,?,?,?,?)", (bid, "2026-11-03T00:00:00Z", FRESH, "u", "rate_limited"))
    run(conn, tmp_path / "out")
    assert read(tmp_path / "out" / "companies_verified.csv") == []
    assert read(tmp_path / "out" / "companies_incomplete.csv")[0]["missing_fields"] == "fresh_check_not_passed"


def test_export_is_byte_reproducible_and_checksums_match(conn, tmp_path):
    import hashlib

    seed(conn, "greenhouse", "acme", "Acme", website="https://acme.com")
    seed(conn, "lever", "soloco", None, status="identity_unresolved")
    run(conn, tmp_path / "a")
    run(conn, tmp_path / "b")
    for p in (tmp_path / "a").iterdir():
        assert p.read_bytes() == (tmp_path / "b" / p.name).read_bytes(), p.name
    for line in (tmp_path / "a" / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ")
        assert hashlib.sha256((tmp_path / "a" / name).read_bytes()).hexdigest() == digest


def test_failed_gate_writes_only_a_report_beside_the_output(conn, tmp_path, monkeypatch):
    seed(conn, "greenhouse", "acme", "Acme")
    conn.commit()
    monkeypatch.setattr(export, "quality_gates", lambda *a, **k: {"passed": False, "failure_counts": {"unique_boards": 1}, "failures": {"unique_boards": ["acme"]}})
    with pytest.raises(export.ExportGateError):
        build_export(conn, ["r1", FRESH], tmp_path / "out", fresh_origin=FRESH)
    assert not (tmp_path / "out").exists()
    assert json.loads((tmp_path / "out.failed" / "qa_report.json").read_text())["gates"]["passed"] is False


def test_gates_catch_a_populated_field_with_no_evidence_and_bad_urls():
    row = {"board_id": 1, "company_name": "Acme", "description": DESC, "ats_system": "greenhouse", "ats_code": "acme", "linkedin_url": "https://www.linkedin.com/company/acme/",
           "website_url": "acme.com", "careersite_url": "", "verification_status": "verified_live", "identity_status": "identity_confirmed", "verification_origin": FRESH}
    gates = export.quality_gates([row, dict(row)], {(1, "name"): {"Acme"}}, FRESH)
    assert gates["passed"] is False
    assert {"evidence", "url_format", "unique_boards"} <= set(gates["failures"])


def test_gates_catch_placeholder_values():
    row = {"board_id": 1, "company_name": "Test Company", "description": "Lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor.", "ats_system": "greenhouse", "ats_code": "x",
           "linkedin_url": "", "website_url": "", "careersite_url": "", "verification_status": "verified_live", "identity_status": "identity_confirmed", "verification_origin": FRESH}
    evidence = {(1, f): {row[k]} for f, k in (("name", "company_name"), ("description", "description"), ("ats_system", "ats_system"), ("ats_code", "ats_code"))}
    assert "placeholder" in export.quality_gates([row], evidence, FRESH)["failures"]


def company(name, linkedin):
    return {"company_name": name, "linkedin_url": linkedin, "linkedin_method": "m"}


@pytest.mark.parametrize("handle,kept,cleared", [
    ("ogilvy", ["Ogilvy"], ["Ogilvy Australia", "Ogilvy Canada", "Ogilvy Health Canada", "Ogilvy MENA"]),
    ("kpmg", ["KPMG LLP"], ["KPMG Australia", "KPMG Nederland"]),
    ("virtual-gaming-worlds", ["VGW"], ["VGW Australia", "VGW Canada", "VGW EU", "VGW USA"]),
    ("loopme", ["LoopMe"], ["Chartboost"]),
    ("bluevine", [], ["Bluevine - India", "Bluevine - Israel", "Bluevine - US"]),
    ("10pearls", ["10Pearls"], ["10Pearls - UK"]),
    ("fender-musical-instruments", ["Fender"], ["Fender EMEA"]),
    ("aime-leon-dore", ["Aimé Leon Dore"], ["Aimé Leon Dore EU"]),
    ("524282", ["New Era Technology"], ["New Era Technology NZ"]),
    ("xebia-uk", ["Xebia UK"], ["Xebia Global"]),
])
def test_shared_linkedin_handle_stays_only_on_the_company_it_names(handle, kept, cleared):
    url = f"https://www.linkedin.com/company/{handle}"
    rows = [company(n, url) for n in kept + cleared]
    assert apply_linkedin_policy(rows) == len(cleared)
    assert [r["company_name"] for r in rows if r["linkedin_url"]] == kept
    assert all(r["linkedin_method"] == "" for r in rows if not r["linkedin_url"])


def test_a_handle_on_one_row_is_left_alone():
    rows = [company("Ogilvy Canada", "https://www.linkedin.com/company/ogilvy"), company("Other", "https://www.linkedin.com/company/other")]
    assert apply_linkedin_policy(rows) == 0
    assert all(r["linkedin_url"] for r in rows)


def test_a_row_missing_a_mandatory_field_is_not_also_reported_as_unchecked(conn, tmp_path):
    seed(conn, "greenhouse", "acme", "Acme", description=None, fresh=False)
    run(conn, tmp_path / "out")
    assert read(tmp_path / "out" / "companies_incomplete.csv")[0]["missing_fields"] == "description"


def test_dormant_smartrecruiters_board_matched_only_by_name_is_not_a_confirmed_company(conn, tmp_path):
    # SmartRecruiters serves a page for any signed-up identifier: "cdc" plus cdc.gov calling itself CDC proves nothing.
    seed(conn, "smartrecruiters", "cdc", "cdc", outcome="verified_live_zero_jobs", jobs=0, website="https://cdc.gov", website_method="reference_name_slug_domain+homepage_names_company")
    seed(conn, "smartrecruiters", "linked", "Linked Co", outcome="verified_live_zero_jobs", jobs=0, website="https://linked.com", website_method="board_link_home_text+homepage_names_company")
    seed(conn, "smartrecruiters", "busy", "Busy Co", website="https://busy.com", website_method="reference_name_slug_domain+homepage_names_company")
    seed(conn, "greenhouse", "other", "Other Co", outcome="verified_live_zero_jobs", jobs=0, website="https://other.com", website_method="reference_name_slug_domain+homepage_names_company")
    run(conn, tmp_path / "out")
    assert sorted(r["ats_code"] for r in read(tmp_path / "out" / "companies_verified.csv")) == ["busy", "linked", "other"]
    row = read(tmp_path / "out" / "unresolved_boards.csv")[0]
    assert (row["ats_code"], row["identity_basis"]) == ("cdc", export.SR_NAME_GUESS)


def test_stored_slogans_from_board_pages_do_not_count_as_descriptions(conn, tmp_path):
    bid = seed(conn, "breezy", "nimble", "Nimble Credit Solutions", description=None)
    conn.execute("INSERT INTO field_evidence(board_id, field, value, method, status, observed_at, run) VALUES (?,?,?,?,'verified','t','r1')",
                 (bid, "description", "BECOME A PART OF THE NIMBLE SUCCESS STORY NOW! Great things are happening here at this company.", "board_page_meta"))
    run(conn, tmp_path / "out")
    assert read(tmp_path / "out" / "companies_incomplete.csv")[0]["missing_fields"] == "description"
