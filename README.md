# llm-cost-guard

> Per-user and per-feature token budgets, spend tracking and alerts for LLM-powered apps.

**Status:** 🚧 Early stage — design and roadmap published, implementation in progress. Stars, issues and ideas welcome.

## Why

Shipping an AI feature without spend controls means one abusive user or runaway loop can produce a surprise bill.

## Planned features

- [ ] Drop-in wrapper or lightweight proxy around LLM SDK calls
- [ ] Token and cost accounting per user, per feature, per model
- [ ] Hard and soft budgets with configurable actions (block, degrade model, alert)
- [ ] Slack / email / webhook alerts
- [ ] SQLite by default, Postgres optional
- [ ] Minimal spend dashboard

## Intended usage

```
guard = CostGuard(budget_usd_per_user_day=0.50)
resp = guard.call(llm_call, user='u_123', feature='summarize', ...)
```

_The interface above is the target design and may change._

## Roadmap

1. Core implementation of the first feature set
2. Examples and tests
3. CI and first tagged release
4. Docs and integrations

## Contributing

Contributions are welcome. Open an issue to discuss an idea or pick an unchecked item from the feature list.

## License

[MIT](LICENSE)
