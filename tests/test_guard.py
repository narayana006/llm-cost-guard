import doctest
import threading
import warnings
from datetime import datetime, timezone

import pytest

import llm_cost_guard.guard
import llm_cost_guard.prices
from llm_cost_guard import (
    Budget,
    BudgetExceeded,
    CostGuard,
    PriceTable,
    UnknownModelError,
    Usage,
    UsageWarning,
)

PRICES = {"small-model": {"input": 1.0, "output": 2.0}, "large-model": {"input": 10.0, "output": 20.0}}


class Clock:
    def __init__(self):
        self.now = datetime(2025, 3, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class FakeModel:
    """Returns a fixed usage and counts how often it was really called."""

    def __init__(self, input_tokens=100_000, output_tokens=50_000):
        self.usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {"text": "ok", "usage": self.usage}


@pytest.fixture
def clock():
    return Clock()


def make_guard(clock, **kwargs):
    return CostGuard(PRICES, db_path=":memory:", clock=clock, **kwargs)


# 100k in + 50k out on small-model = 0.10 + 0.10 = $0.20 per call
CALL_COST = 0.20


def test_call_forwards_arguments_returns_result_and_records_cost(clock):
    guard = make_guard(clock)
    model = FakeModel()
    result = guard.call(model, "hello", temperature=0, user="u1", feature="chat", model="small-model")
    assert result["text"] == "ok"
    assert model.calls == [(("hello",), {"temperature": 0})]
    assert guard.spent_today(user="u1") == pytest.approx(CALL_COST)
    assert guard.spent_today(feature="chat") == pytest.approx(CALL_COST)
    assert guard.spent_today(model="small-model") == pytest.approx(CALL_COST)
    assert guard.spent_today() == pytest.approx(CALL_COST)
    assert guard.spent_today(user="someone-else") == 0


def test_usage_can_be_numbers_an_extractor_or_an_object(clock):
    guard = make_guard(clock)
    guard.call(lambda: "text only", user="a", model="small-model", usage=(1_000_000, 0))
    assert guard.spent_today(user="a") == pytest.approx(1.0)
    guard.call(lambda: "text only", user="b", model="small-model", usage=Usage(0, 1_000_000))
    assert guard.spent_today(user="b") == pytest.approx(2.0)
    guard.call(
        lambda: ("text", 500_000, 0),
        user="c",
        model="small-model",
        usage=lambda result: {"prompt_tokens": result[1], "completion_tokens": result[2]},
    )
    assert guard.spent_today(user="c") == pytest.approx(0.5)

    class Response:
        class usage:
            input_tokens = 1_000_000
            output_tokens = 1_000_000

    guard.call(Response, user="d", model="small-model")
    assert guard.spent_today(user="d") == pytest.approx(3.0)


def test_guard_level_extractor(clock):
    guard = make_guard(clock, usage_extractor=lambda text: (len(text), 0), default_model="small-model")
    guard.call(lambda: "x" * 1_000_000, user="u1")
    assert guard.spent_today(user="u1") == pytest.approx(1.0)


def test_unreadable_usage_warns_and_records_a_zero_token_call(clock):
    guard = make_guard(clock)
    with pytest.warns(UsageWarning, match="zero tokens"):
        assert guard.call(lambda: "just text", user="u1", model="small-model") == "just text"
    (row,) = guard.store.summary("user")
    assert row["calls"] == 1 and row["cost_usd"] == 0


def test_user_budget_blocks_before_calling_the_model(clock):
    guard = make_guard(clock, budget_usd_per_user_day=0.30)
    model = FakeModel()
    guard.call(model, user="u1", model="small-model")  # 0.20 spent, under the limit
    guard.call(model, user="u1", model="small-model")  # 0.40 spent, this call crossed it
    with pytest.raises(BudgetExceeded) as excinfo:
        guard.call(model, user="u1", model="small-model")
    assert len(model.calls) == 2
    event = excinfo.value.event
    assert (event.scope, event.key, event.limit_usd) == ("user", "u1", 0.30)
    assert event.spent_usd == pytest.approx(0.40)
    assert "user 'u1'" in str(excinfo.value)
    # Other users are unaffected, and the blocked attempt is counted.
    guard.call(model, user="u2", model="small-model")
    rows = {r["key"]: r for r in guard.store.summary("user")}
    assert rows["u1"]["calls"] == 2 and rows["u1"]["blocked"] == 1
    assert rows["u2"]["blocked"] == 0


def test_budget_resets_on_the_next_utc_day(clock):
    guard = make_guard(clock, budget_usd_per_user_day=0.10)
    model = FakeModel()
    guard.call(model, user="u1", model="small-model")
    with pytest.raises(BudgetExceeded):
        guard.call(model, user="u1", model="small-model")
    clock.now = datetime(2025, 3, 2, 0, 0, 1, tzinfo=timezone.utc)
    guard.call(model, user="u1", model="small-model")
    assert guard.spent_today(user="u1") == pytest.approx(CALL_COST)


def test_feature_and_total_budgets(clock):
    guard = make_guard(clock, budget_usd_per_feature_day=0.20, budget_usd_per_day=0.50)
    model = FakeModel()
    guard.call(model, user="u1", feature="chat", model="small-model")
    with pytest.raises(BudgetExceeded) as excinfo:
        guard.call(model, user="u2", feature="chat", model="small-model")
    assert excinfo.value.event.scope == "feature"
    guard.call(model, user="u2", feature="search", model="small-model")
    guard.call(model, user="u3", feature="export", model="small-model")  # total now 0.60
    with pytest.raises(BudgetExceeded) as excinfo:
        guard.call(model, user="u4", feature="other", model="small-model")
    assert excinfo.value.event.scope == "total"
    assert excinfo.value.event.key is None


def test_specific_key_budget_overrides_the_general_one(clock):
    guard = make_guard(
        clock, budgets=[Budget("user", 0.10), Budget("user", 1.00, key="vip")]
    )
    model = FakeModel()
    for _ in range(3):
        guard.call(model, user="vip", model="small-model")
    guard.call(model, user="regular", model="small-model")
    with pytest.raises(BudgetExceeded):
        guard.call(model, user="regular", model="small-model")


def test_callback_budget_fires_once_when_crossed_and_does_not_block(clock):
    events = []
    guard = make_guard(clock, budget_usd_per_user_day=0.30, on_exceed=events.append)
    model = FakeModel()
    for _ in range(4):
        guard.call(model, user="u1", model="small-model")
    assert len(model.calls) == 4
    assert len(events) == 1
    assert events[0].spent_usd == pytest.approx(0.40)
    assert events[0].user == "u1"


def test_callback_may_raise_to_stop_the_caller(clock):
    def stop(event):
        raise RuntimeError("paging the on-call")

    guard = make_guard(clock, budgets=[Budget("model", 0.10, action=stop)])
    with pytest.raises(RuntimeError, match="paging"):
        guard.call(FakeModel(), user="u1", model="small-model")
    assert guard.spent_today() == pytest.approx(CALL_COST)  # the spend is still recorded


def test_unknown_model_is_rejected_before_any_money_is_spent(clock):
    guard = make_guard(clock)
    model = FakeModel()
    with pytest.raises(UnknownModelError, match="mystery-model"):
        guard.call(model, user="u1", model="mystery-model")
    assert model.calls == []


def test_missing_model_or_user_is_an_error(clock):
    guard = make_guard(clock)
    with pytest.raises(ValueError, match="no model given"):
        guard.call(FakeModel(), user="u1")
    with pytest.raises(ValueError, match="user"):
        guard.call(FakeModel(), user="", model="small-model")


def test_exception_in_the_wrapped_call_propagates_and_records_nothing(clock):
    guard = make_guard(clock)

    def broken():
        raise TimeoutError("upstream timed out")

    with pytest.raises(TimeoutError):
        guard.call(broken, user="u1", model="small-model")
    assert guard.store.summary("user") == []


def test_record_and_check_for_calls_made_elsewhere(clock):
    guard = make_guard(clock, budget_usd_per_user_day=0.25)
    guard.check(user="u1", model="small-model")
    cost = guard.record(user="u1", model="small-model", input_tokens=300_000)
    assert cost == pytest.approx(0.30)
    with pytest.raises(BudgetExceeded):
        guard.check(user="u1", model="small-model")
    # An explicit cost bypasses the price table.
    assert guard.record(user="u2", model="unpriced", cost_usd=0.07) == 0.07
    assert guard.spent_today(user="u2") == pytest.approx(0.07)


def test_wrap_fixes_feature_and_model(clock):
    guard = make_guard(clock)
    model = FakeModel()
    summarize = guard.wrap(model, feature="summarize", model="large-model")
    summarize("long text", user="u1")
    assert model.calls == [(("long text",), {})]
    assert guard.spent_today(feature="summarize") == pytest.approx(2.0)


def test_spend_persists_across_guards_on_the_same_file(tmp_path, clock):
    db = tmp_path / "costs.db"
    first = CostGuard(PRICES, db_path=db, clock=clock, budget_usd_per_user_day=0.10)
    first.call(FakeModel(), user="u1", model="small-model")
    first.close()
    second = CostGuard(PRICES, db_path=db, clock=clock, budget_usd_per_user_day=0.10)
    with pytest.raises(BudgetExceeded):
        second.call(FakeModel(), user="u1", model="small-model")


def test_concurrent_calls_are_all_recorded(tmp_path, clock):
    guard = CostGuard(PRICES, db_path=tmp_path / "costs.db", clock=clock)
    model = FakeModel(1000, 0)

    def worker():
        for _ in range(25):
            guard.call(model, user="u1", model="small-model")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert guard.store.summary("user")[0]["calls"] == 200


@pytest.mark.parametrize(
    "kwargs",
    [
        {"scope": "team", "limit_usd": 1},
        {"scope": "user", "limit_usd": -1},
        {"scope": "user", "limit_usd": 1, "action": "degrade"},
        {"scope": "total", "limit_usd": 1, "key": "x"},
    ],
)
def test_invalid_budgets_are_rejected(kwargs):
    with pytest.raises(ValueError):
        Budget(**kwargs)


def test_price_table_validation_default_and_file(tmp_path):
    with pytest.raises(ValueError):
        PriceTable({"m": {"input": 1}})
    with pytest.raises(ValueError):
        PriceTable({"m": {"input": -1, "output": 1}})
    table = PriceTable({"m": {"input": 1, "output": 2}}, default={"input": 3, "output": 3})
    assert table.cost("m", 1_000_000, 500_000) == pytest.approx(2.0)
    assert table.cost("anything-else", 1_000_000, 0) == pytest.approx(3.0)

    path = tmp_path / "prices.json"
    path.write_text('{"models": {"m": {"input": 1, "output": 2}}, "default": {"input": 9, "output": 9}}')
    assert PriceTable.from_file(path).cost("other", 1_000_000, 0) == pytest.approx(9.0)
    path.write_text('{"m": {"input": 1, "output": 2}}')
    assert PriceTable.from_file(path).cost("m", 0, 1_000_000) == pytest.approx(2.0)


def test_docstring_examples():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        for module in (llm_cost_guard.guard, llm_cost_guard.prices):
            assert doctest.testmod(module).failed == 0
