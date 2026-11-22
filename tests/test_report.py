import csv
import json

from company_dataset.legacy.report import run_report


def test_report_states_counts_gap_duplicates_and_sources(tmp_path):
    dirs = {k: tmp_path / k for k in ("raw", "cache", "intermediate", "output", "reports")}
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    (dirs["intermediate"] / "build_stats.json").write_text(json.dumps({"unique_candidates": 10}))
    (dirs["intermediate"] / "validation_stats.json").write_text(json.dumps({"by_status": {"valid": 6, "invalid": 4}}))
    fields = ("name", "description", "linkedin_url", "website_url", "careersite_url", "ats_system", "ats_code")
    (dirs["intermediate"] / "export_stats.json").write_text(json.dumps({
        "complete_companies": 4,
        "live_companies_after_collapse": 5,
        "incomplete_companies": 1,
        "boards_merged_into_companies": 1,
        "same_name_not_merged": 2,
        "same_name_not_merged_in_deliverable": 1,
        "completeness_live": {f: {"non_null": 4 if f == "linkedin_url" else 5, "total": 5, "pct": 80.0 if f == "linkedin_url" else 100.0} for f in fields},
        "missing_field_counts": {"linkedin_url": 1},
        "country_complete": {"rows_with_country": 4, "us_uk_ca": 3, "breakdown": {"US": 2, "GB": 1, "DE": 1}},
        "post_validation_rejects": {"careersite_unavailable": 1, "duplicate_company_board": 1},
        "source_overlap": {"by_source_count": {"1": 2, "2": 3}, "in_live_boards": {"latmay": 4}, "sole_source_for": {"latmay": 2}},
    }))
    with open(dirs["output"] / "source_manifest.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["source", "role", "license"])
        w.writeheader()
        w.writerow({"source": "latmay", "role": "boards", "license": "CC-BY-4.0"})

    run_report(dirs)
    text = (dirs["reports"] / "quality_report.md").read_text(encoding="utf-8")
    assert "`companies_complete.csv` has **4 companies**" in text
    assert "That is 19,996 short of the 20,000" in text
    assert "| `linkedin_url` | 4 | 5 | 80.0% |" in text
    assert "| **Live companies** | **5** |" in text
    assert "Boards folded into another company's row: 1" in text
    assert "2 (1 of them in `companies_complete.csv`)" in text
    assert "2 US, 1 UK, 0 Canada (75.0% of rows)" in text
    assert "| latmay | board slugs | CC-BY-4.0 | 4 | 2 |" in text
    assert "60.0% of live boards turn up in two or more" in text


def test_report_includes_smartrecruiters_and_breezy_ats_table(tmp_path):
    """Test that the report includes SmartRecruiters and Breezy HR in the ATS table."""
    dirs = {k: tmp_path / k for k in ("raw", "cache", "intermediate", "output", "reports")}
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    (dirs["intermediate"] / "build_stats.json").write_text(json.dumps({"unique_candidates": 10}))
    (dirs["intermediate"] / "validation_stats.json").write_text(json.dumps({
        "by_status": {"valid": 8, "invalid": 2},
        "by_ats": {
            "greenhouse": {"valid": 3, "invalid": 1},
            "lever": {"valid": 2, "invalid": 0},
            "ashby": {"valid": 1, "invalid": 1},
            "smartrecruiters": {"valid": 1, "invalid": 0},
            "breezy": {"valid": 1, "invalid": 0},
        }
    }))
    fields = ("name", "description", "linkedin_url", "website_url", "careersite_url", "ats_system", "ats_code")
    (dirs["intermediate"] / "export_stats.json").write_text(json.dumps({
        "complete_companies": 4,
        "live_companies_after_collapse": 5,
        "incomplete_companies": 1,
        "boards_merged_into_companies": 1,
        "same_name_not_merged": 2,
        "same_name_not_merged_in_deliverable": 1,
        "completeness_live": {f: {"non_null": 4 if f == "linkedin_url" else 5, "total": 5, "pct": 80.0 if f == "linkedin_url" else 100.0} for f in fields},
        "missing_field_counts": {"linkedin_url": 1},
        "country_complete": {"rows_with_country": 4, "us_uk_ca": 3, "breakdown": {"US": 2, "GB": 1, "DE": 1}},
        "post_validation_rejects": {"careersite_unavailable": 1, "duplicate_company_board": 1},
        "source_overlap": {"by_source_count": {"1": 2, "2": 3}, "in_live_boards": {"latmay": 4}, "sole_source_for": {"latmay": 2}},
        "live_by_ats": {"greenhouse": 3, "lever": 2, "ashby": 1, "smartrecruiters": 1, "breezy": 1},
        "complete_by_ats": {"greenhouse": 2, "lever": 1, "ashby": 1, "smartrecruiters": 1, "breezy": 0},
    }))
    with open(dirs["output"] / "source_manifest.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["source", "role", "license"])
        w.writeheader()
        w.writerow({"source": "latmay", "role": "boards", "license": "CC-BY-4.0"})

    run_report(dirs)
    text = (dirs["reports"] / "quality_report.md").read_text(encoding="utf-8")
    
    # Check that SmartRecruiters and Breezy HR appear in the ATS table
    assert "| SmartRecruiters |" in text
    assert "| Breezy HR |" in text
    assert "| 1 |" in text  # live count for SmartRecruiters and Breezy
    
    # Check that the table has the right structure
    assert "| ATS | Live | Complete |" in text


def test_report_includes_unresolved_dead_reasons_per_ats(tmp_path):
    """Test that the report includes a section with unresolved/dead reason counts per ATS."""
    dirs = {k: tmp_path / k for k in ("raw", "cache", "intermediate", "output", "reports")}
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    (dirs["intermediate"] / "build_stats.json").write_text(json.dumps({"unique_candidates": 10}))
    (dirs["intermediate"] / "validation_stats.json").write_text(json.dumps({
        "by_status": {"valid": 6, "invalid": 2, "unresolved": 2},
        "by_ats": {
            "greenhouse": {"valid": 2, "invalid": 1, "unresolved": 1},
            "smartrecruiters": {"valid": 1, "invalid": 0, "unresolved": 1},
        },
        "unresolved_reasons": {
            "greenhouse": {"total_found_zero": 1},
            "smartrecruiters": {"company_identifier_mismatch": 1},
        }
    }))
    fields = ("name", "description", "linkedin_url", "website_url", "careersite_url", "ats_system", "ats_code")
    (dirs["intermediate"] / "export_stats.json").write_text(json.dumps({
        "complete_companies": 4,
        "live_companies_after_collapse": 5,
        "incomplete_companies": 1,
        "boards_merged_into_companies": 1,
        "same_name_not_merged": 2,
        "same_name_not_merged_in_deliverable": 1,
        "completeness_live": {f: {"non_null": 4 if f == "linkedin_url" else 5, "total": 5, "pct": 80.0 if f == "linkedin_url" else 100.0} for f in fields},
        "missing_field_counts": {"linkedin_url": 1},
        "country_complete": {"rows_with_country": 4, "us_uk_ca": 3, "breakdown": {"US": 2, "GB": 1, "DE": 1}},
        "post_validation_rejects": {"careersite_unavailable": 1, "duplicate_company_board": 1},
        "source_overlap": {"by_source_count": {"1": 2, "2": 3}, "in_live_boards": {"latmay": 4}, "sole_source_for": {"latmay": 2}},
        "live_by_ats": {"greenhouse": 2, "smartrecruiters": 1},
        "complete_by_ats": {"greenhouse": 1, "smartrecruiters": 1},
    }))
    with open(dirs["output"] / "source_manifest.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["source", "role", "license"])
        w.writeheader()
        w.writerow({"source": "latmay", "role": "boards", "license": "CC-BY-4.0"})

    run_report(dirs)
    text = (dirs["reports"] / "quality_report.md").read_text(encoding="utf-8")
    
    # Check for the unresolved/dead reasons section
    assert "## Unresolved & Dead Reasons by ATS" in text
    assert "| ATS | Reason | Count |" in text
    assert "| Greenhouse | total_found_zero | 1 |" in text
    assert "| SmartRecruiters | company_identifier_mismatch | 1 |" in text


def test_report_no_overwrite_check(tmp_path):
    """Test that report fails if quality_report.md already exists."""
    from company_dataset.legacy.report import run_report
    
    dirs = {k: tmp_path / k for k in ("raw", "cache", "intermediate", "output", "reports")}
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    (dirs["intermediate"] / "build_stats.json").write_text(json.dumps({"unique_candidates": 10}))
    (dirs["intermediate"] / "validation_stats.json").write_text(json.dumps({"by_status": {"valid": 6, "invalid": 4}}))
    fields = ("name", "description", "linkedin_url", "website_url", "careersite_url", "ats_system", "ats_code")
    (dirs["intermediate"] / "export_stats.json").write_text(json.dumps({
        "complete_companies": 4,
        "live_companies_after_collapse": 5,
        "incomplete_companies": 1,
        "boards_merged_into_companies": 1,
        "same_name_not_merged": 2,
        "same_name_not_merged_in_deliverable": 1,
        "completeness_live": {f: {"non_null": 4 if f == "linkedin_url" else 5, "total": 5, "pct": 80.0 if f == "linkedin_url" else 100.0} for f in fields},
        "missing_field_counts": {"linkedin_url": 1},
        "country_complete": {"rows_with_country": 4, "us_uk_ca": 3, "breakdown": {"US": 2, "GB": 1, "DE": 1}},
        "post_validation_rejects": {"careersite_unavailable": 1, "duplicate_company_board": 1},
        "source_overlap": {"by_source_count": {"1": 2, "2": 3}, "in_live_boards": {"latmay": 4}, "sole_source_for": {"latmay": 2}},
    }))
    with open(dirs["output"] / "source_manifest.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["source", "role", "license"])
        w.writeheader()
        w.writerow({"source": "latmay", "role": "boards", "license": "CC-BY-4.0"})

    # First run should succeed
    run_report(dirs)
    assert (dirs["reports"] / "quality_report.md").exists()
    
    # Second run should fail (no-overwrite)
    import pytest
    with pytest.raises(FileExistsError):
        run_report(dirs)
