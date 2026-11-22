from company_dataset.models import BoardValidation


def test_company_identifier_round_trips():
    validation = BoardValidation(
        ats="breezy",
        code="10percent",
        status="valid",
        company_identifier="10percent",
    )

    payload = validation.to_dict()

    assert payload["company_identifier"] == "10percent"
    assert BoardValidation.from_dict(payload) == validation


def test_from_dict_accepts_old_cache_entry_without_company_identifier():
    validation = BoardValidation.from_dict(
        {"ats": "greenhouse", "code": "acme", "status": "valid", "jobs_count": 3}
    )

    assert validation.company_identifier is None
    assert validation.jobs_count == 3


def test_from_dict_ignores_unknown_fields():
    validation = BoardValidation.from_dict(
        {
            "ats": "breezy",
            "code": "10percent",
            "status": "valid",
            "company_identifier": "10percent",
            "future_field": "ignored",
        }
    )

    assert validation.code == "10percent"
    assert validation.company_identifier == "10percent"
