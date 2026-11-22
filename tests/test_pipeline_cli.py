import pytest

from company_dataset import pipeline


def test_unknown_or_missing_stage_prints_usage(capsys):
    assert pipeline.main([]) == 2
    assert pipeline.main(["nonsense"]) == 2
    assert "ingest -> identity -> enrich -> export" in capsys.readouterr().err


@pytest.mark.parametrize("stage", sorted(pipeline.STAGES))
def test_stage_receives_the_remaining_arguments(monkeypatch, stage):
    seen = {}

    class Stage:
        @staticmethod
        def main(argv):
            seen["argv"] = argv
            return 7

    monkeypatch.setattr(pipeline.importlib, "import_module", lambda name: Stage if name == pipeline.STAGES[stage] else None)
    assert pipeline.main([stage, "--db", "x"]) == 7
    assert seen["argv"] == ["--db", "x"]
