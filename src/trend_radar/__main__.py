"""Command line entry point for the ETF radar."""

from __future__ import annotations

import argparse
from datetime import timedelta

import pandas as pd

from .audit import audit_outputs
from .backfill import backfill_scores
from .config import MARKET_COMPLETE_TIME, now_shanghai
from .fetcher import fetch_data
from .refresh import REFRESH_FAILED, refresh_latest_data
from .scoring import generate_scores
from .validation import validate_outputs


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m trend_radar")
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch_parser = subparsers.add_parser("fetch", help="Fetch and cache public ETF data")
    fetch_parser.add_argument("--date", default=None, help="Target data date, YYYY-MM-DD")
    fetch_parser.add_argument("--limit", type=int, default=None, help="Limit candidate ETFs for smoke tests")
    fetch_parser.add_argument("--codes", default=None, help="Comma-separated ETF codes for targeted fetches")

    score_parser = subparsers.add_parser("score", help="Generate radar ranking from cached data")
    score_parser.add_argument("--date", default=None, help="Target data date, YYYY-MM-DD")
    score_parser.add_argument("--source-date", default=None, help="Normalized source snapshot date, YYYY-MM-DD")

    backfill_parser = subparsers.add_parser("backfill", help="Backfill daily radar outputs from cached history")
    backfill_parser.add_argument("--months", type=int, default=1, help="Number of 20-trading-day months to backfill")
    backfill_parser.add_argument("--source-date", default=None, help="Normalized source snapshot date, YYYY-MM-DD")

    validate_parser = subparsers.add_parser("validate", help="Validate radar outputs and UI-facing data")
    validate_parser.add_argument("--start", required=True, help="Start date, YYYY-MM-DD")
    validate_parser.add_argument("--end", required=True, help="End date, YYYY-MM-DD")

    audit_parser = subparsers.add_parser("audit", help="End-to-end audit of cached Top30 data")
    audit_parser.add_argument("--start", required=True, help="Start date, YYYY-MM-DD")
    audit_parser.add_argument("--end", required=True, help="End date, YYYY-MM-DD")
    audit_parser.add_argument("--external", action="store_true", help="Re-fetch external history and compare local cache")
    audit_parser.add_argument("--repair", action="store_true", help="Back up, re-fetch, and regenerate failed audit ranges")

    refresh_parser = subparsers.add_parser("refresh", help="Fetch and score the newest complete data if needed")
    refresh_parser.add_argument("--force", action="store_true", help="Fetch and score even when the target radar exists")

    args = parser.parse_args()
    if args.command in {"fetch", "score"}:
        data_date = args.date or _default_data_date()
    else:
        data_date = None

    if args.command == "fetch":
        codes = [item.strip() for item in args.codes.split(",")] if args.codes else None
        result = fetch_data(data_date=str(data_date), limit=args.limit, codes=codes)
        print(
            f"Fetched {result.candidate_rows}/{result.spot_rows} ETF candidates, "
            f"{result.history_rows} history rows, {result.share_rows} share rows for {result.data_date}."
        )
        if result.errors:
            print(f"Warnings/errors: {len(result.errors)}; see {result.normalized_dir / 'fetch_report.json'}")
    elif args.command == "score":
        result = generate_scores(data_date=str(data_date), source_date=args.source_date)
        print(
            f"Generated {result.rows} radar rows for {result.data_date}: "
            f"{result.radar_csv} ({result.quality_status})"
        )
    elif args.command == "backfill":
        result = backfill_scores(months=args.months, source_date=args.source_date)
        first_date = result.dates[0] if result.dates else "-"
        last_date = result.dates[-1] if result.dates else "-"
        total_rows = sum(item.rows for item in result.results)
        print(
            f"Backfilled {len(result.dates)} trading days from {result.source_date}: "
            f"{first_date} to {last_date}, {total_rows} total radar rows."
        )
    elif args.command == "validate":
        result = validate_outputs(start=args.start, end=args.end)
        status = "OK" if result.ok else "FAIL"
        print(
            f"Validation {status}: {len(result.checked_dates)} dates, "
            f"{len(result.errors)} errors, {len(result.warnings)} warnings. Report: {result.report_path}"
        )
        if result.errors:
            for error in result.errors[:20]:
                print(f"- {error}")
            raise SystemExit(1)
    elif args.command == "audit":
        result = audit_outputs(start=args.start, end=args.end, external=args.external, repair=args.repair)
        status = "OK" if result.ok else "FAIL"
        repaired = " repaired" if result.repaired else ""
        print(
            f"Audit {status}{repaired}: {len(result.checked_dates)} dates, "
            f"{len(result.errors)} errors, {len(result.warnings)} warnings. Report: {result.report_path}"
        )
        if result.errors:
            for error in result.errors[:20]:
                print(f"- {error}")
            raise SystemExit(1)
    elif args.command == "refresh":
        result = refresh_latest_data(force=args.force)
        actual = result.actual_date or "-"
        message = result.message or result.status
        print(f"Refresh {result.status}: target={result.target_date}, actual={actual}. {message}")
        if result.radar_csv:
            print(f"Radar: {result.radar_csv}")
        if result.error:
            print(f"Error: {result.error}")
        if result.status == REFRESH_FAILED:
            raise SystemExit(1)


def _default_data_date() -> str:
    now = now_shanghai()
    today = pd.Timestamp(now.date())
    if now.time() >= MARKET_COMPLETE_TIME:
        return today.strftime("%Y-%m-%d")
    previous = pd.bdate_range(end=today - timedelta(days=1), periods=1)[0]
    return previous.strftime("%Y-%m-%d")


if __name__ == "__main__":
    main()
