"""Terminal-first, research-only job workflow with explicit network execution."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path

from . import jobs, run_manager, workflow


def _print(value: object) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def _run_path(value: Path) -> Path:
    return run_manager.resolve_run(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=run_manager.ROOT / "jobs.db", help="Local master database")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="Create a structured run and daily plan")
    plan.add_argument("--date", type=date.fromisoformat, default=datetime.now(run_manager.TIMEZONE).date())
    plan.add_argument("--scope", required=True)
    plan.add_argument("--target-count", type=int, default=10)
    plan.add_argument("--focus", action="append", help="Repeat for additional role/responsibility focus")
    plan.add_argument("--locations", action="append", help="Repeat for additional preferred location")
    plan.add_argument("--freshness-days", type=int, default=30)
    plan.add_argument("--company-cap", type=int, default=2)
    plan.add_argument("--max-searches", type=int, default=20)
    plan.add_argument("--max-scrapes", type=int, default=30)
    plan.add_argument("--max-maps", type=int, default=4)
    plan.add_argument("--max-crawls", type=int, default=2)
    plan.add_argument("--max-interacts", type=int, default=2)
    plan.add_argument("--secondary-track-count", type=int, default=2)
    plan.add_argument("--include-companies", action="append")
    plan.add_argument("--exclude-companies", action="append")
    plan.add_argument("--cache-age-hours", type=int, default=24)
    plan.add_argument("--zero-yield-stop", type=int, default=4)

    discover = subparsers.add_parser("discover", help="Prepare a balanced Firecrawl search plan")
    discover.add_argument("run", type=Path)
    discover.add_argument("--execute", action="store_true", help="Explicitly authorize planned Firecrawl searches")

    triage = subparsers.add_parser("triage", help="Build a deduplicated local candidate ledger")
    triage.add_argument("run", type=Path)

    verify = subparsers.add_parser("verify", help="Prepare bounded canonical-page verification")
    verify.add_argument("run", type=Path)
    verify.add_argument("--execute", action="store_true", help="Explicitly authorize planned Firecrawl verification")

    finalize = subparsers.add_parser("finalize", help="Use the staged, hardened finalization path")
    finalize.add_argument("run", type=Path)
    finalize.add_argument("--expected-count", type=int)

    review = subparsers.add_parser("review", help="Review local records without opening job URLs")
    review_commands = review.add_subparsers(dest="review_command", required=True)
    review_list = review_commands.add_parser("list", help="List jobs awaiting review")
    review_list.add_argument("--status", action="append", choices=sorted(jobs.REVIEW_STATUSES))
    review_show = review_commands.add_parser("show", help="Show one local job and its review history")
    review_show.add_argument("--job-id", type=int, required=True)
    review_set = review_commands.add_parser("set", help="Set review status and optionally record a reason")
    review_set.add_argument("--job-id", type=int, required=True)
    review_set.add_argument("--status", required=True, choices=sorted(jobs.REVIEW_STATUSES))
    review_set.add_argument("--reason", choices=sorted(jobs.REVIEW_REASONS))
    review_set.add_argument("--note")
    review_commands.add_parser("feedback", help="Summarize explicit review feedback")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "plan":
        budgets = {
            "search": args.max_searches, "scrape": args.max_scrapes, "map": args.max_maps,
            "crawl": args.max_crawls, "interact": args.max_interacts,
        }
        run_dir = workflow.create_plan(
            args.date, args.scope, args.target_count, args.focus, args.locations,
            args.freshness_days, args.company_cap, budgets, args.secondary_track_count,
            args.include_companies, args.exclude_companies, args.cache_age_hours, args.zero_yield_stop,
        )
        _print({"run": run_dir.relative_to(run_manager.ROOT).as_posix(), "plan": "plan.json"})
    elif args.command == "discover":
        run_dir = _run_path(args.run)
        if args.execute:
            _print(workflow.execute_discovery(run_dir))
        else:
            _print(workflow.generate_discovery_plan(run_dir))
    elif args.command == "triage":
        _print(workflow.triage(_run_path(args.run), args.db))
    elif args.command == "verify":
        run_dir = _run_path(args.run)
        if args.execute:
            _print(workflow.execute_verification(run_dir))
        else:
            _print(workflow.generate_verification_plan(run_dir))
    elif args.command == "finalize":
        _print(run_manager.finalize_run(_run_path(args.run), args.expected_count))
    elif args.review_command == "list":
        _print(workflow.review_list(args.db, args.status or ["new", "review"]))
    elif args.review_command == "show":
        _print(workflow.review_show(args.db, args.job_id))
    elif args.review_command == "set":
        workflow.review_set(args.db, args.job_id, args.status, args.reason, args.note)
        _print({"job_id": args.job_id, "status": args.status, "reason": args.reason})
    else:
        _print(workflow.feedback_summary(args.db))


if __name__ == "__main__":
    main()
