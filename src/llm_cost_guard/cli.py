"""``llm-cost-guard report``: a spend summary from the SQLite file."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from . import __version__
from .store import GROUP_COLUMNS, SpendStore


def _date(text: str) -> str:
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{text}' is not a date like 2025-01-31") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm-cost-guard", description="Spend tracking and daily budgets for LLM calls."
    )
    parser.add_argument("--version", action="version", version=f"llm-cost-guard {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report", help="summarise recorded spend")
    report.add_argument("--db", default="llm_costs.db", help="SQLite file (default: llm_costs.db)")
    report.add_argument(
        "--by", choices=GROUP_COLUMNS, default="user", help="grouping (default: user)"
    )
    report.add_argument("--since", type=_date, metavar="YYYY-MM-DD", help="first UTC day to include")
    report.add_argument("--until", type=_date, metavar="YYYY-MM-DD", help="last UTC day to include")
    report.add_argument("--today", action="store_true", help="only the current UTC day")
    report.add_argument("--top", type=int, metavar="N", help="show only the N largest rows")
    report.add_argument("--json", action="store_true", help="print JSON instead of a table")
    return parser


def format_table(rows: list[dict], by: str) -> str:
    headers = [by, "calls", "blocked", "input tok", "output tok", "cost USD"]
    body = [
        [
            str(r["key"]),
            str(r["calls"]),
            str(r["blocked"]),
            f"{r['input_tokens']:,}",
            f"{r['output_tokens']:,}",
            f"{r['cost_usd']:.4f}",
        ]
        for r in rows
    ]
    total = [
        "TOTAL",
        str(sum(r["calls"] for r in rows)),
        str(sum(r["blocked"] for r in rows)),
        f"{sum(r['input_tokens'] for r in rows):,}",
        f"{sum(r['output_tokens'] for r in rows):,}",
        f"{sum(r['cost_usd'] for r in rows):.4f}",
    ]
    widths = [max(len(row[i]) for row in [headers, total, *body]) for i in range(len(headers))]

    def line(cells: list[str]) -> str:
        first = cells[0].ljust(widths[0])
        rest = [cell.rjust(width) for cell, width in zip(cells[1:], widths[1:])]
        return "  ".join([first, *rest]).rstrip()

    rule = "  ".join("-" * width for width in widths)
    return "\n".join([line(headers), rule, *map(line, body), rule, line(total)])


def cmd_report(args: argparse.Namespace) -> int:
    if not Path(args.db).exists():
        print(f"llm-cost-guard: error: no database at {args.db}", file=sys.stderr)
        return 2
    since, until = args.since, args.until
    if args.today:
        since = until = datetime.now(timezone.utc).date().isoformat()
    store = SpendStore(args.db)
    try:
        all_rows = store.summary(args.by, since, until)
    finally:
        store.close()
    rows = all_rows[: args.top] if args.top else all_rows

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not all_rows:
        print("No calls recorded for this period.")
        return 0
    period = "all time"
    if since and until:
        period = since if since == until else f"{since} to {until}"
    elif since:
        period = f"since {since}"
    elif until:
        period = f"until {until}"
    print(f"Spend by {args.by}, {period} (UTC)\n")
    print(format_table(rows, args.by))
    if len(rows) < len(all_rows):
        print(f"\nShowing {len(rows)} of {len(all_rows)} rows; TOTAL covers the rows shown.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return cmd_report(args)


if __name__ == "__main__":
    sys.exit(main())
