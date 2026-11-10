"""Offline demo: a fake model, three users, a per-user daily budget.

Run from the repository root:

    python examples/demo.py
    llm-cost-guard report --db examples/demo.db

The prices in prices.json are made up. Use your provider's real prices.
"""

from pathlib import Path

from llm_cost_guard import Budget, BudgetExceeded, CostGuard, PriceTable

HERE = Path(__file__).parent
DB = HERE / "demo.db"


def llm_call(prompt: str, max_words: int = 50) -> dict:
    """Stands in for a real model call. Returns text plus token usage, which
    is the shape most model clients give you in one form or another."""
    answer = " ".join(["word"] * max_words)
    return {
        "text": answer,
        "usage": {"input_tokens": len(prompt.split()) * 300, "output_tokens": max_words * 40},
    }


def alert(event) -> None:
    print(f"  ALERT  {event.describe()}")


def main() -> None:
    DB.unlink(missing_ok=True)  # start fresh so the demo prints the same thing every time
    guard = CostGuard(
        PriceTable.from_file(HERE / "prices.json"),
        db_path=DB,
        budget_usd_per_user_day=0.05,  # hard limit: block the user for the rest of the day
        budgets=[Budget("feature", 0.10, action=alert)],  # soft limit: tell someone, keep serving
    )

    traffic = [
        ("alice", "summarize", "small-model", 3),
        ("bob", "chat", "small-model", 2),
        ("mallory", "chat", "large-model", 8),  # a runaway loop
    ]
    for user, feature, model, attempts in traffic:
        for attempt in range(1, attempts + 1):
            try:
                guard.call(
                    llm_call,
                    "Summarise the following support ticket in two sentences please",
                    max_words=60,
                    user=user,
                    feature=feature,
                    model=model,
                )
            except BudgetExceeded as exc:
                print(f"  BLOCKED  {user} call {attempt}: {exc}")
                break
        print(f"{user:8} spent ${guard.spent_today(user=user):.4f} today")
    guard.close()


if __name__ == "__main__":
    main()
