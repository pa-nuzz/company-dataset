import gzip
import json

from company_dataset.sources import commoncrawl_extra as cx


def _line(surt, ts, url, status="200"):
    return f"{surt} {ts} " + json.dumps({"url": url, "status": status})


def test_observations_keep_first_and_last_capture_and_only_extra_ats():
    block = gzip.compress("\n".join([
        _line("com,bamboohr,acme)/careers", "20240101000000", "https://acme.bamboohr.com/careers", "404"),
        _line("com,bamboohr,acme)/jobs", "20250101000000", "https://acme.bamboohr.com/jobs/view.php?id=1"),
        _line("com,bamboohr,www)/", "20250101000000", "https://www.bamboohr.com/"),          # reserved label
        _line("com,greenhouse,x)/", "20250101000000", "https://boards.greenhouse.io/x"),      # other prefix
        _line("com,bamboohr,beta)/careers", "20230101000000", "https://beta.bamboohr.com/careers", "301"),
    ]).encode())
    obs = cx.observations_in_block(block, "com,bamboohr,")
    assert set(obs) == {("bamboohr", "acme", None), ("bamboohr", "beta", None)}
    first_ts, first_url, last_ts, last_url, n, ok = obs[("bamboohr", "acme", None)]
    assert (first_ts, last_ts, n, ok) == ("20240101000000", "20250101000000", 2, True)
    assert first_url.endswith("/careers") and "view.php" in last_url
    assert obs[("bamboohr", "beta", None)][5] is False


def test_tokens_file_aggregates_crawls_with_provenance(tmp_path):
    cache = {
        "CC-MAIN-2026-39|com,bamboohr,": {"blocks": [], "observations": [["bamboohr", "acme", None, "20260901", "u2", "20260902", "u3", 2, True]]},
        "CC-MAIN-2025-51|com,bamboohr,": {"blocks": [], "observations": [["bamboohr", "acme", None, "20251201", "u1", "20251201", "u1", 1, False]]},
    }
    path = tmp_path / "tokens.csv"
    assert cx.write_tokens(path, cache, ["CC-MAIN-2026-39", "CC-MAIN-2025-51"]) == 1
    recs, _ = cx.parse(path)
    (r,) = recs
    assert (r.ats, r.code, r.careersite_url) == ("bamboohr", "acme", "u3")
    assert "first seen CC-MAIN-2025-51 20251201" in r.row_ref and "crawls 2" in r.row_ref
