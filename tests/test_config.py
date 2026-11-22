from company_dataset.config import ATS_RATE_LIMITS, resolve_rate_limits


def test_resolve_rate_limits_uses_current_defaults():
    assert resolve_rate_limits(env={}) == ATS_RATE_LIMITS


def test_resolve_rate_limits_reads_environment(monkeypatch):
    for var in (
        "COMPANY_DATASET_SMARTRECRUITERS_MAX_CONCURRENT",
        "COMPANY_DATASET_SMARTRECRUITERS_DELAY_SECONDS",
        "COMPANY_DATASET_BREEZY_MAX_CONCURRENT",
        "COMPANY_DATASET_BREEZY_DELAY_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("COMPANY_DATASET_SMARTRECRUITERS_MAX_CONCURRENT", "1")
    monkeypatch.setenv("COMPANY_DATASET_SMARTRECRUITERS_DELAY_SECONDS", "2.5")
    limits = resolve_rate_limits()
    assert limits["smartrecruiters"] == {"max_concurrent": 1, "delay_seconds": 2.5}
    assert limits["breezy"] == {"max_concurrent": 2, "delay_seconds": 0.5}


def test_resolve_rate_limits_explicit_overrides_win():
    limits = resolve_rate_limits(
        overrides={"breezy": {"max_concurrent": 5, "delay_seconds": 1.25}},
        env={},
    )
    assert limits["breezy"] == {"max_concurrent": 5, "delay_seconds": 1.25}
    assert limits["smartrecruiters"] == {"max_concurrent": 2, "delay_seconds": 0.5}
