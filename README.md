# llm-cost-guard

Per-user and per-feature token budgets, spend tracking and alerts for LLM-powered apps.

> **Project status: planning. There is no working code in this repository yet.**
> This README describes what the project will do. Nothing below can be installed or run today.
> Features are ticked off in the checklist as they are built.

## What it is

A Python library with an optional proxy.

**The problem it solves:** Shipping an AI feature without spend controls means one abusive user or runaway loop can produce a surprise bill.

## Who it is for

Anyone running an LLM feature in production who needs to cap what a single user or feature can spend.

## How it will work

1. You wrap your existing model calls with the guard and tag each call with a user and a feature name.
2. The guard records tokens and cost for every call in a local database.
3. When a user or feature goes over its budget, the guard blocks the call, switches to a cheaper model or sends an alert, depending on your settings.

## Planned features

Unticked items are not built yet.

- [ ] Drop-in wrapper or lightweight proxy around LLM SDK calls
- [ ] Token and cost accounting per user, per feature, per model
- [ ] Hard and soft budgets with configurable actions (block, degrade model, alert)
- [ ] Slack / email / webhook alerts
- [ ] SQLite by default, Postgres optional
- [ ] Minimal spend dashboard

## Planned usage

This is the intended interface, shown so you can judge the design. **It does not work yet**, and names may change before the first release.

```python
guard = CostGuard(budget_usd_per_user_day=0.50)
resp = guard.call(llm_call, user='u_123', feature='summarize', ...)
```

## Contributing

Feedback on the design is the most useful contribution right now. Open an issue to suggest a feature, question an approach or describe your use case. Code contributions are welcome once the first version lands.

## License

[MIT](LICENSE)
