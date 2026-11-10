"""The guard: wrap a call, record what it cost, enforce daily budgets."""

from __future__ import annotations

import functools
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .prices import PriceTable
from .store import SpendStore
from .usage import Extractor, Usage, UsageError, UsageWarning, coerce_usage, default_extractor

SCOPES = ("user", "feature", "model", "total")
BLOCK = "block"


@dataclass(frozen=True)
class BudgetEvent:
    """Describes a budget that has been reached. Passed to callbacks and
    attached to :class:`BudgetExceeded`."""

    scope: str  # "user", "feature", "model" or "total"
    key: str | None  # which user/feature/model; None for "total"
    limit_usd: float
    spent_usd: float
    day: str
    user: str
    feature: str
    model: str

    def describe(self) -> str:
        target = "total spend" if self.scope == "total" else f"{self.scope} '{self.key}'"
        return (
            f"daily budget reached for {target}: "
            f"${self.spent_usd:.4f} spent of ${self.limit_usd:.4f} on {self.day}"
        )


class BudgetExceeded(RuntimeError):
    """Raised instead of making the call when a blocking budget is used up."""

    def __init__(self, event: BudgetEvent) -> None:
        super().__init__(event.describe())
        self.event = event


@dataclass(frozen=True)
class Budget:
    """A daily spending limit in USD (days are UTC).

    ``scope`` says what the limit applies to. With ``key=None`` every user (or
    feature, or model) gets the same limit; with a key it applies to that one
    only and takes precedence over the general limit for the same scope.

    ``action`` is ``"block"`` (refuse further calls once the limit is reached)
    or a callable that receives a :class:`BudgetEvent` once, right after the
    call that crosses the limit, while calls keep going through.
    """

    scope: str
    limit_usd: float
    action: str | Callable[[BudgetEvent], Any] = BLOCK
    key: str | None = None

    def __post_init__(self) -> None:
        if self.scope not in SCOPES:
            raise ValueError(f"budget scope must be one of {SCOPES}, got '{self.scope}'")
        if self.limit_usd < 0:
            raise ValueError("budget limit must not be negative")
        if self.action != BLOCK and not callable(self.action):
            raise ValueError("budget action must be 'block' or a callable")
        if self.scope == "total" and self.key is not None:
            raise ValueError("a 'total' budget cannot have a key")


class CostGuard:
    """Records tokens and cost per user, feature and model, and enforces
    daily budgets.

    >>> guard = CostGuard(
    ...     db_path=":memory:",
    ...     prices={"small-model": {"input": 0.50, "output": 1.50}},
    ...     budget_usd_per_user_day=0.50,
    ... )
    >>> def llm_call(prompt):
    ...     return {"text": "ok", "usage": {"input_tokens": 1200, "output_tokens": 400}}
    >>> guard.call(llm_call, "hello", user="u_123", feature="summarize", model="small-model")["text"]
    'ok'
    >>> round(guard.spent_today(user="u_123"), 6)
    0.0012
    """

    def __init__(
        self,
        prices: PriceTable | Mapping[str, Mapping[str, float]] | None = None,
        *,
        db_path: str | Path = "llm_costs.db",
        budgets: list[Budget] | None = None,
        budget_usd_per_user_day: float | None = None,
        budget_usd_per_feature_day: float | None = None,
        budget_usd_per_day: float | None = None,
        on_exceed: str | Callable[[BudgetEvent], Any] = BLOCK,
        usage_extractor: Extractor = default_extractor,
        default_model: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.prices = prices if isinstance(prices, PriceTable) else PriceTable(prices or {})
        self.store = SpendStore(db_path)
        self.usage_extractor = usage_extractor
        self.default_model = default_model
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.budgets = list(budgets or [])
        shortcuts = (
            ("user", budget_usd_per_user_day),
            ("feature", budget_usd_per_feature_day),
            ("total", budget_usd_per_day),
        )
        for scope, limit in shortcuts:
            if limit is not None:
                self.budgets.append(Budget(scope, limit, on_exceed))

    def close(self) -> None:
        self.store.close()

    # -- time ---------------------------------------------------------------

    def _now(self) -> tuple[str, str]:
        now = self._clock().astimezone(timezone.utc)
        return now.isoformat(timespec="seconds"), now.date().isoformat()

    # -- budgets ------------------------------------------------------------

    def _applicable(self, tags: dict[str, str]) -> list[tuple[Budget, str | None]]:
        """Budgets that apply to a call, with the key each one is measured on.
        A budget for a specific key replaces the general one of that scope."""
        chosen = []
        for scope in SCOPES:
            key = None if scope == "total" else tags[scope]
            candidates = [b for b in self.budgets if b.scope == scope]
            specific = [b for b in candidates if b.key is not None and b.key == key]
            general = [b for b in candidates if b.key is None]
            chosen += [(budget, key) for budget in (specific or general)]
        return chosen

    def _spent(self, day: str, scope: str, key: str | None) -> float:
        return self.store.spent(day) if scope == "total" else self.store.spent(day, scope, key)

    def _event(self, budget: Budget, key: str | None, spent: float, day: str, tags: dict) -> BudgetEvent:
        return BudgetEvent(budget.scope, key, budget.limit_usd, spent, day, **tags)

    def check(self, *, user: str, feature: str = "default", model: str | None = None) -> None:
        """Raise :class:`BudgetExceeded` if a blocking budget is already used
        up for this user/feature/model. Records nothing."""
        tags = self._tags(user, feature, model)
        event = self._blocking_event(tags, self._now()[1])
        if event is not None:
            raise BudgetExceeded(event)

    def _blocking_event(self, tags: dict[str, str], day: str) -> BudgetEvent | None:
        for budget, key in self._applicable(tags):
            if budget.action != BLOCK:
                continue
            spent = self._spent(day, budget.scope, key)
            if spent >= budget.limit_usd:
                return self._event(budget, key, spent, day, tags)
        return None

    # -- recording ----------------------------------------------------------

    def _tags(self, user: str, feature: str, model: str | None) -> dict[str, str]:
        model = model or self.default_model
        if not model:
            raise ValueError("no model given: pass model=... or set default_model on the guard")
        if not user:
            raise ValueError("user must be a non-empty string")
        return {"user": str(user), "feature": str(feature), "model": str(model)}

    def record(
        self,
        *,
        user: str,
        feature: str = "default",
        model: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float | None = None,
    ) -> float:
        """Record usage for a call you made yourself (for example a streamed
        response) and return its cost. Pass ``cost_usd`` to bypass the price
        table. Callback budgets fire if this record crosses their limit."""
        tags = self._tags(user, feature, model)
        usage = Usage(int(input_tokens), int(output_tokens))
        if cost_usd is None:
            cost_usd = self.prices.cost(tags["model"], usage.input_tokens, usage.output_tokens)
        ts, day = self._now()

        watched = [
            (budget, key, self._spent(day, budget.scope, key))
            for budget, key in self._applicable(tags)
            if budget.action != BLOCK
        ]
        self.store.add(
            ts=ts,
            day=day,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=cost_usd,
            **tags,
        )
        for budget, key, before in watched:
            after = before + cost_usd
            if before < budget.limit_usd <= after:
                budget.action(self._event(budget, key, after, day, tags))
        return cost_usd

    def call(
        self,
        fn: Callable[..., Any],
        /,
        *args: Any,
        user: str,
        feature: str = "default",
        model: str | None = None,
        usage: Extractor | Usage | tuple[int, int] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Run ``fn(*args, **kwargs)`` under the guard and return its result.

        ``user``, ``feature``, ``model`` and ``usage`` are consumed by the
        guard and not forwarded. ``usage`` is either the numbers themselves or
        a function that extracts them from the result; without it the guard's
        ``usage_extractor`` is used.

        Before the call: raises :class:`BudgetExceeded` if a blocking budget is
        used up, and :class:`UnknownModelError` if the model has no price, so
        nothing is spent on a call that cannot be priced. If ``fn`` raises,
        the exception propagates and nothing is recorded. If usage cannot be
        read from the result, the call is recorded with zero tokens and a
        :class:`UsageWarning` is emitted.
        """
        tags = self._tags(user, feature, model)
        self.prices.rates(tags["model"])
        ts, day = self._now()
        event = self._blocking_event(tags, day)
        if event is not None:
            self.store.add(ts=ts, day=day, status="blocked", **tags)
            raise BudgetExceeded(event)

        result = fn(*args, **kwargs)

        try:
            if callable(usage):
                measured = coerce_usage(usage(result))
            elif usage is not None:
                measured = coerce_usage(usage)
            else:
                measured = coerce_usage(self.usage_extractor(result))
        except UsageError as exc:
            # The call has already been paid for, so keep the result and make
            # the gap visible: a zero-token row plus a warning.
            warnings.warn(
                f"llm-cost-guard: {exc}; recording this call with zero tokens",
                UsageWarning,
                stacklevel=2,
            )
            measured = Usage()
        self.record(
            input_tokens=measured.input_tokens, output_tokens=measured.output_tokens, **tags
        )
        return result

    def wrap(
        self,
        fn: Callable[..., Any],
        *,
        feature: str = "default",
        model: str | None = None,
        usage: Extractor | None = None,
    ) -> Callable[..., Any]:
        """Return a guarded version of ``fn`` with the feature and model fixed.
        The wrapper takes the same arguments as ``fn`` plus a required
        ``user=`` keyword."""

        @functools.wraps(fn)
        def guarded(*args: Any, user: str, **kwargs: Any) -> Any:
            return self.call(
                fn, *args, user=user, feature=feature, model=model, usage=usage, **kwargs
            )

        return guarded

    # -- reading ------------------------------------------------------------

    def spent_today(
        self, *, user: str | None = None, feature: str | None = None, model: str | None = None
    ) -> float:
        """USD spent so far today (UTC), in total or for one user, feature or model."""
        filters = [(c, k) for c, k in (("user", user), ("feature", feature), ("model", model)) if k]
        if len(filters) > 1:
            raise ValueError("filter by at most one of user, feature, model")
        day = self._now()[1]
        return self.store.spent(day, *filters[0]) if filters else self.store.spent(day)
