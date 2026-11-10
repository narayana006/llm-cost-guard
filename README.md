# llm-cost-guard

Cap what a single user or feature can spend on LLM calls. A small Python library that wraps any model call, records tokens and cost per user, feature and model in SQLite, and enforces daily budgets.

**Status: early but usable (v0.1.0).** The call wrapper, cost accounting, a price table you configure, daily budgets that block or call back, and a command-line spend report work today. Alert integrations, model downgrading, a proxy, Postgres and a dashboard are not built.

It has no dependencies outside the standard library and does not import any model SDK. You hand it the function you already call; it does not care which model is behind it.

## Install

Python 3.10 or newer. Install from GitHub (the `llm-cost-guard` name on the package indexes belongs to unrelated projects, so do not `pip install llm-cost-guard`):

```bash
pip install git+https://github.com/narayana006/llm-cost-guard
```

## Try it offline

Clone the repository to get the demo, which uses a fake model and made-up prices:

```bash
git clone https://github.com/narayana006/llm-cost-guard
cd llm-cost-guard
pip install -e .
python examples/demo.py
```

```text
alice    spent $0.0149 today
bob      spent $0.0099 today
  ALERT  daily budget reached for feature 'chat': $0.1089 spent of $0.1000 on 2026-11-10
  BLOCKED  mallory call 3: daily budget reached for user 'mallory': $0.0990 spent of $0.0500 on 2026-11-10
mallory  spent $0.0990 today
```

(The date is the day you run it.) `mallory` is a runaway loop: the per-user budget of $0.05 stops her third call before the model is invoked. The `chat` feature has a soft budget that only raises an alert. Then look at what was recorded:

```bash
llm-cost-guard report --db examples/demo.db
```

```text
Spend by user, all time (UTC)

user     calls  blocked  input tok  output tok  cost USD
-------  -----  -------  ---------  ----------  --------
mallory      2        1      5,400       4,800    0.0990
alice        3        0      8,100       7,200    0.0149
bob          2        0      5,400       4,800    0.0099
-------  -----  -------  ---------  ----------  --------
TOTAL        7        1     18,900      16,800    0.1238
```

## Using it in your code

```python
from llm_cost_guard import BudgetExceeded, CostGuard

guard = CostGuard(
    prices={"small-model": {"input": 0.50, "output": 1.50}},  # USD per 1M tokens
    db_path="llm_costs.db",
    budget_usd_per_user_day=0.50,
)

def llm_call(prompt):
    # Your real model call goes here. This stand-in returns the usual shape:
    # some text plus the token counts the provider reports.
    return {"text": "...", "usage": {"input_tokens": 1200, "output_tokens": 400}}

try:
    resp = guard.call(llm_call, "Summarise this ticket", user="u_123", feature="summarize", model="small-model")
except BudgetExceeded as exc:
    print(exc)  # daily budget reached for user 'u_123': ...
```

`guard.call(fn, *args, **kwargs)` runs `fn(*args, **kwargs)` and returns whatever it returns. The keywords `user`, `feature`, `model` and `usage` are consumed by the guard and not forwarded; if your function needs one of those names, wrap it in a `lambda` or `functools.partial`. `user` is required, `feature` defaults to `"default"`, and `model` can be set once with `CostGuard(default_model=...)`.

### Where the token numbers come from

The guard never counts tokens itself. It uses the numbers your provider reports, found in one of three ways:

1. **Automatically.** If the result has a `usage` key or attribute holding `input_tokens`/`output_tokens` (or `prompt_tokens`/`completion_tokens`), or carries those fields directly, they are picked up.
2. **An extractor function**, per call (`usage=lambda resp: (resp.tokens_in, resp.tokens_out)`) or for the whole guard (`CostGuard(usage_extractor=...)`). It may return an `(input, output)` pair, a `Usage`, or a mapping/object with the field names above.
3. **The numbers themselves**: `usage=(1200, 400)`.

If usage cannot be read, the result is still returned, the call is recorded with zero tokens, and a `UsageWarning` is emitted so the gap is visible.

For calls the guard cannot wrap, such as a streamed response whose usage arrives at the end, check and record yourself:

```python
guard.check(user="u_123", feature="chat", model="small-model")   # raises BudgetExceeded if blocked
# ... make the call ...
guard.record(user="u_123", feature="chat", model="small-model", input_tokens=900, output_tokens=2100)
```

`record` also accepts `cost_usd=` to bypass the price table. To fix the feature and model once, `guarded = guard.wrap(llm_call, feature="summarize", model="small-model")` returns a function you call as `guarded(prompt, user="u_123")`.

### Prices

No prices are built in; they change, and the library does not know your models. Pass a dict as above or load a JSON file:

```python
from llm_cost_guard import CostGuard, PriceTable

guard = CostGuard(PriceTable.from_file("examples/prices.json"))
```

```json
{
  "models": {
    "small-model": { "input": 0.50, "output": 1.50 },
    "large-model": { "input": 5.00, "output": 15.00 }
  },
  "default": { "input": 5.00, "output": 15.00 }
}
```

Values are USD per million tokens. `default` is optional. Without it, a call naming a model that has no price raises `UnknownModelError` before the model is invoked, so nothing is spent that cannot be priced.

### Budgets

Budgets are daily and reset at midnight UTC. The shortcuts `budget_usd_per_user_day`, `budget_usd_per_feature_day` and `budget_usd_per_day` (everything combined) cover the common cases, with `on_exceed` choosing the action for all three. For anything else pass `Budget` objects:

```python
from llm_cost_guard import Budget, CostGuard

def notify(event):
    print(event.describe())   # send this wherever you send alerts

guard = CostGuard(
    prices={"small-model": {"input": 0.50, "output": 1.50}},
    budgets=[
        Budget("user", 0.50),                       # every user: block at $0.50/day
        Budget("user", 5.00, key="internal-batch"), # one user gets a higher limit
        Budget("feature", 20.00, action=notify),    # soft limit: call back, keep serving
        Budget("total", 100.00),                    # everything combined
    ],
)
```

- Scope is `"user"`, `"feature"`, `"model"` or `"total"`. A budget with a `key` applies to that one user/feature/model and replaces the general budget of the same scope for it.
- `action="block"` (the default) raises `BudgetExceeded` before the model is called, once the day's spend has reached the limit. The blocked attempt is stored and shown in the report's `blocked` column. `exc.event` carries the scope, key, limit and amount spent.
- A callable action is invoked once, right after the call that crosses the limit, with a `BudgetEvent`. Calls keep going through. If the callback raises, the exception reaches the caller of `guard.call`; the spend is already recorded.

`guard.spent_today()`, `guard.spent_today(user="u_123")`, `feature=` and `model=` return today's spend in USD.

## Spend report

```text
llm-cost-guard report [--db llm_costs.db] [--by user|feature|model|day]
                      [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--today]
                      [--top N] [--json]
```

Rows are sorted by cost, highest first (`--by day` is in date order). `calls` counts calls that went through, `blocked` counts attempts refused by a budget. `--json` prints the same rows as a JSON array, for example `llm-cost-guard report --db examples/demo.db --by feature --json`.

The data is one table, `calls`, in a plain SQLite file, so you can also query it directly.

## Feature checklist

- [x] Drop-in wrapper around any model call (`guard.call`, `guard.wrap`, or `check` + `record`)
- [x] Token and cost accounting per user, per feature, per model
- [x] User-configurable price table (dict or JSON file)
- [x] Daily budgets per user, feature, model or in total, with block or callback actions
- [x] SQLite storage, no external services
- [x] Command-line spend report
- [ ] Lightweight proxy mode
- [ ] Degrade-to-cheaper-model action
- [ ] Slack / email / webhook alerts (today: write a callback)
- [ ] Postgres storage
- [ ] Spend dashboard

## Limitations

- **A budget can be overshot by one call per concurrent caller.** The check happens before the call and the cost is only known after it, so the call that crosses the limit goes through and the next one is blocked. Parallel calls for the same user can each pass the check before any of them is recorded.
- Costs are only as accurate as the usage numbers and prices you supply. There is one input and one output rate per model; cached-token discounts, per-request fees and tiered pricing are not modelled (use `record(cost_usd=...)` if you compute the cost yourself).
- Budgets are per UTC day only. No hourly, monthly or rolling windows.
- Synchronous only. `guard.call` does not await coroutines; for async code use `check` and `record` around your own `await`.
- A call that raises is not recorded, even if the provider billed part of it.
- SQLite suits one host. Several processes can share a file, but this is not meant for a large fleet writing to one database.
- User and feature names are stored as given. Use opaque ids, not email addresses, if the database should hold no personal data.

## Contributing

Issues and pull requests are welcome. To run the tests:

```bash
pip install -e ".[dev]"
pytest
```

Built and maintained by [Narayana Vemburaj](https://github.com/narayana006).

## License

[MIT](LICENSE)
