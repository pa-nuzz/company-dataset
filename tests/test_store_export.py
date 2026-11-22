import pytest

from company_dataset import store
from company_dataset.export import run_export


def _seed(conn):
    def board(ats, code):
        return store.board_id(conn, ats, code)

    def check(bid, outcome, jobs=1):
        conn.execute("INSERT INTO verifications(board_id, checked_at, origin, endpoint, http_status, outcome, jobs_count) VALUES (?,?,?,?,?,?,?)",
                     (bid, "2026-10-31T10:00:00Z", "r1", f"https://api/{bid}", 200, outcome, jobs))

    def ident(bid, status, name):
        conn.execute("INSERT INTO identities(board_id, run, status, name, basis, decided_at) VALUES (?,?,?,?,?,?)", (bid, "r1", status, name, "vendor+homepage", "t"))

    def ev(bid, field, value, status="verified", method="m"):
        conn.execute("INSERT INTO field_evidence(board_id, field, value, method, status, observed_at, run) VALUES (?,?,?,?,?,?,?)", (bid, field, value, method, status, "t", "r1"))

    full = {"description": "Acme builds robots.", "linkedin_url": "https://www.linkedin.com/company/acme", "website_url": "https://acme.com"}
    a = board("greenhouse", "acme"); check(a, "verified_live", 5); ident(a, "identity_confirmed", "Acme")
    b = board("lever", "acme"); check(b, "verified_live_zero_jobs", 0); ident(b, "identity_confirmed", "Acme Inc")
    for bid, ats, code in ((a, "greenhouse", "acme"), (b, "lever", "acme")):
        ev(bid, "name", "Acme" if bid == a else "Acme Inc"); ev(bid, "ats_system", ats); ev(bid, "ats_code", code)
        ev(bid, "careersite_url", f"https://jobs/{code}")
        for f, v in full.items():
            ev(bid, f, v)
    c = board("ashby", "nolinkedin"); check(c, "verified_live"); ident(c, "identity_confirmed", "No LinkedIn Co")
    for f, v in (("name", "No LinkedIn Co"), ("ats_system", "ashby"), ("ats_code", "nolinkedin"), ("careersite_url", "https://jobs/x"), ("website_url", "https://nl.co"), ("description", "Makes things.")):
        ev(c, f, v)
    d = board("workable", "blocked"); check(d, "verified_live"); ident(d, "identity_confirmed", "Blocked Co")
    for f, v in (("name", "Blocked Co"), ("ats_system", "workable"), ("ats_code", "blocked"), ("description", "x is a firm."), ("linkedin_url", "https://www.linkedin.com/company/b"), ("website_url", "https://b.co")):
        ev(d, f, v)
    ev(d, "careersite_url", "https://apply.workable.com/blocked/", status="candidate")
    e = board("rippling", "unknown"); check(e, "verified_live"); ident(e, "identity_unresolved", None)
    f_ = board("lever", "dead"); check(f_, "dead_removed", None); ident(f_, "not_live", None)
    conn.commit()


def test_export_separates_counts_and_is_byte_reproducible(tmp_path):
    conn = store.connect(tmp_path / "s.db")
    _seed(conn)
    c1 = run_export(conn, "r1", tmp_path / "e1")
    c2 = run_export(conn, "r1", tmp_path / "e2")
    assert c1 == {**c2}
    assert (tmp_path / "e1" / "SHA256SUMS").read_text() == (tmp_path / "e2" / "SHA256SUMS").read_text()
    assert c1["candidate_boards_in_run"] == 6
    assert c1["live_boards"] == 5
    assert c1["identity_confirmed_live_boards"] == 4
    assert c1["unique_companies"] == 3          # the two Acme boards are one company
    assert c1["complete_companies"] == 1        # missing LinkedIn / page-blocked careersite are not complete
    links = (tmp_path / "e1" / "company_boards.csv").read_text().splitlines()
    assert len(links) == 1 + 4                  # every confirmed board kept, even when merged
    unresolved = (tmp_path / "e1" / "unresolved_boards.csv").read_text()
    assert "unknown" in unresolved and "dead" in unresolved


def test_export_never_overwrites(tmp_path):
    conn = store.connect(tmp_path / "s.db")
    (tmp_path / "e").mkdir()
    with pytest.raises(FileExistsError):
        run_export(conn, "r1", tmp_path / "e")
