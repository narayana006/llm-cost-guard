import json
from datetime import datetime, timezone

import pytest

from llm_cost_guard import CostGuard
from llm_cost_guard.cli import main

PRICES = {"small-model": {"input": 1.0, "output": 2.0}}


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "costs.db"
    days = iter([datetime(2025, 3, d, 9, tzinfo=timezone.utc) for d in (1, 1, 2, 3, 3)])
    guard = CostGuard(PRICES, db_path=path, clock=lambda: next(days), budget_usd_per_user_day=1.0)
    guard.record(user="alice", feature="chat", model="small-model", input_tokens=2_000_000)  # day 1
    guard.record(user="bob", feature="search", model="small-model", input_tokens=500_000)  # day 1
    guard.record(user="bob", feature="chat", model="small-model", output_tokens=1_000_000)  # day 2
    guard.record(user="alice", feature="chat", model="small-model", input_tokens=1_500_000)  # day 3
    with pytest.raises(Exception):
        guard.call(lambda: None, user="alice", feature="chat", model="small-model")  # day 3, blocked
    guard.close()
    return str(path)


def test_report_by_user(db, capsys):
    assert main(["report", "--db", db]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "Spend by user, all time (UTC)"
    assert lines[2].split() == ["user", "calls", "blocked", "input", "tok", "output", "tok", "cost", "USD"]
    assert lines[4].split() == ["alice", "2", "1", "3,500,000", "0", "3.5000"]
    assert lines[5].split() == ["bob", "2", "0", "500,000", "1,000,000", "2.5000"]
    assert lines[-1].split() == ["TOTAL", "4", "1", "4,000,000", "1,000,000", "6.0000"]


def test_report_date_range_and_grouping(db, capsys):
    assert main(["report", "--db", db, "--by", "day", "--since", "2025-03-02", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["key"] for r in rows] == ["2025-03-02", "2025-03-03"]
    assert rows[1]["blocked"] == 1

    assert main(["report", "--db", db, "--by", "feature", "--until", "2025-03-01"]) == 0
    out = capsys.readouterr().out
    assert "until 2025-03-01" in out
    assert out.splitlines()[-1].split()[-1] == "2.5000"


def test_report_top(db, capsys):
    assert main(["report", "--db", db, "--top", "1"]) == 0
    out = capsys.readouterr().out
    assert "bob" not in out
    assert "Showing 1 of 2 rows" in out


def test_report_with_no_rows_in_period(db, capsys):
    assert main(["report", "--db", db, "--since", "2030-01-01"]) == 0
    assert "No calls recorded" in capsys.readouterr().out


def test_missing_database_exits_two_without_creating_it(tmp_path, capsys):
    missing = tmp_path / "nope.db"
    assert main(["report", "--db", str(missing)]) == 2
    assert "no database" in capsys.readouterr().err
    assert not missing.exists()


def test_bad_date_is_rejected(db):
    with pytest.raises(SystemExit):
        main(["report", "--db", db, "--since", "yesterday"])
