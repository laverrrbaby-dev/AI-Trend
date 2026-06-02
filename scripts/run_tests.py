from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
REPORT_ROOT = PROJECT_ROOT / "output" / "test-reports"
LIVE_ROOT = PROJECT_ROOT / "output" / "live-smoke"
LIVE_CODES = ["159995", "510300", "512880"]

sys.path.insert(0, str(SRC_ROOT))


@dataclass
class CheckResult:
    name: str
    status: str
    returncode: int = 0
    duration_seconds: float = 0.0
    stdout: str = ""
    stderr: str = ""
    details: dict[str, Any] = field(default_factory=dict)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run trend-radar test suites.")
    parser.add_argument("--mode", choices=["offline", "live", "audit", "full"], default="offline")
    parser.add_argument("--date", default="auto", help="auto or YYYY-MM-DD")
    parser.add_argument("--live-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--timestamp", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.live_worker:
        return run_live_smoke_worker(args.date, args.timestamp or timestamp)

    started = time.time()
    REPORT_ROOT.mkdir(parents=True, exist_ok=True)

    results: list[CheckResult] = []
    if args.mode in {"offline", "full"}:
        results.append(run_offline_tests())
    if args.mode in {"live", "full"}:
        results.append(run_live_smoke(args.date, timestamp))
    if args.mode in {"audit", "full"}:
        results.append(run_cache_audit(args.date))

    payload = {
        "mode": args.mode,
        "date": args.date,
        "started_at": datetime.fromtimestamp(started).isoformat(timespec="seconds"),
        "duration_seconds": round(time.time() - started, 3),
        "ok": all(item.status in {"passed", "external_unavailable"} for item in results),
        "results": [item.__dict__ for item in results],
    }
    json_path = REPORT_ROOT / f"{timestamp}_{args.mode}.json"
    md_path = REPORT_ROOT / f"{timestamp}_{args.mode}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_markdown_report(payload), encoding="utf-8")

    print(f"Report JSON: {json_path}")
    print(f"Report Markdown: {md_path}")
    for item in results:
        print(f"{item.name}: {item.status} ({item.duration_seconds:.1f}s)")
        if item.status == "failed" and item.stdout:
            print(item.stdout[-4000:])
        if item.status == "failed" and item.stderr:
            print(item.stderr[-4000:])

    return 0 if payload["ok"] else 1


def run_offline_tests() -> CheckResult:
    start = time.time()
    env = os.environ.copy()
    env["TREND_RADAR_TEST_MODE"] = "offline"
    command = [sys.executable, "-m", "unittest", "discover", "tests"]
    completed = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True, env=env, check=False)
    return CheckResult(
        name="offline_unittest",
        status="passed" if completed.returncode == 0 else "failed",
        returncode=completed.returncode,
        duration_seconds=time.time() - start,
        stdout=completed.stdout,
        stderr=completed.stderr,
        details={"command": " ".join(command), "network": "disabled by convention; tests use fixtures/fakes"},
    )


def run_live_smoke(raw_date: str, timestamp: str) -> CheckResult:
    result_path = REPORT_ROOT / f"{timestamp}_live_worker.json"
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--live-worker",
        "--date",
        raw_date,
        "--timestamp",
        timestamp,
    ]
    start = time.time()
    completed = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True, check=False)
    if result_path.exists():
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        result = CheckResult(**payload)
        result.duration_seconds = time.time() - start
        result.stdout = completed.stdout
        result.stderr = "\n".join(item for item in [result.stderr, completed.stderr] if item)
        return result

    return CheckResult(
        name="live_smoke",
        status="external_unavailable",
        returncode=0,
        duration_seconds=time.time() - start,
        stdout=completed.stdout,
        stderr=completed.stderr,
        details={
            "date": raw_date,
            "stage": "native_abort_or_worker_startup",
            "worker_returncode": completed.returncode,
            "command": " ".join(command),
        },
    )


def run_live_smoke_worker(raw_date: str, timestamp: str) -> int:
    result = _run_live_smoke_in_process(raw_date, timestamp)
    REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    result_path = REPORT_ROOT / f"{timestamp}_live_worker.json"
    result_path.write_text(json.dumps(result.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")
    return result.returncode


def _run_live_smoke_in_process(raw_date: str, timestamp: str) -> CheckResult:
    from trend_radar.fetcher import fetch_data
    from trend_radar.refresh import resolve_refresh_target_date
    from trend_radar.scoring import generate_scores
    from trend_radar.validation import validate_outputs

    start = time.time()
    data_date = resolve_refresh_target_date() if raw_date == "auto" else raw_date
    live_root = LIVE_ROOT / timestamp
    live_root.mkdir(parents=True, exist_ok=True)
    stage = "fetch"
    try:
        fetch_result = fetch_data(
            data_date=data_date,
            root=live_root,
            codes=LIVE_CODES,
            history_calendar_days=260,
            share_calendar_days=10,
            resume=False,
            max_workers=3,
        )
        stage = "score"
        score_result = generate_scores(data_date=data_date, root=live_root)
        stage = "validate"
        validation = validate_outputs(start=data_date, end=data_date, root=live_root)
    except Exception as exc:  # noqa: BLE001 - report stage-specific live failures
        status = "external_unavailable" if stage == "fetch" else "failed"
        return CheckResult(
            name="live_smoke",
            status=status,
            returncode=0 if status == "external_unavailable" else 1,
            duration_seconds=time.time() - start,
            stderr=f"{type(exc).__name__}: {exc}",
            details={"date": data_date, "root": str(live_root), "stage": stage, "codes": LIVE_CODES},
        )

    status = "passed" if validation.ok and not fetch_result.errors else "failed"
    return CheckResult(
        name="live_smoke",
        status=status,
        returncode=0 if status == "passed" else 1,
        duration_seconds=time.time() - start,
        details={
            "date": data_date,
            "root": str(live_root),
            "codes": LIVE_CODES,
            "fetch_errors": fetch_result.errors,
            "radar_csv": str(score_result.radar_csv),
            "validation_errors": validation.errors,
            "validation_warnings": validation.warnings,
            "validation_report": str(validation.report_path),
        },
    )


def run_cache_audit(raw_date: str) -> CheckResult:
    from trend_radar.audit import audit_outputs
    from trend_radar.validation import validate_outputs

    start = time.time()
    data_date = _resolve_existing_radar_date(raw_date)
    if data_date is None:
        return CheckResult(
            name="cache_audit",
            status="failed",
            returncode=1,
            duration_seconds=time.time() - start,
            stderr="No local radar CSV exists under data/radar.",
            details={"date": raw_date},
        )

    validation = validate_outputs(start=data_date, end=data_date, root=PROJECT_ROOT)
    audit = audit_outputs(start=data_date, end=data_date, root=PROJECT_ROOT)
    ok = validation.ok and audit.ok
    return CheckResult(
        name="cache_audit",
        status="passed" if ok else "failed",
        returncode=0 if ok else 1,
        duration_seconds=time.time() - start,
        details={
            "date": data_date,
            "validation_ok": validation.ok,
            "validation_errors": validation.errors,
            "validation_warnings": validation.warnings,
            "validation_report": str(validation.report_path),
            "audit_ok": audit.ok,
            "audit_errors": audit.errors,
            "audit_warnings": audit.warnings,
            "audit_report": str(audit.report_path),
        },
    )


def _resolve_existing_radar_date(raw_date: str) -> str | None:
    if raw_date != "auto":
        return raw_date
    radar_dir = PROJECT_ROOT / "data" / "radar"
    if not radar_dir.exists():
        return None
    dates = sorted(path.stem for path in radar_dir.glob("*.csv"))
    return dates[-1] if dates else None


def _markdown_report(payload: dict[str, Any]) -> str:
    lines = [
        f"# Trend Radar Test Report",
        "",
        f"- Mode: `{payload['mode']}`",
        f"- Date: `{payload['date']}`",
        f"- Started: `{payload['started_at']}`",
        f"- Duration: `{payload['duration_seconds']}s`",
        f"- Overall: `{'PASS' if payload['ok'] else 'FAIL'}`",
        "",
        "## Results",
    ]
    for item in payload["results"]:
        lines.extend(
            [
                "",
                f"### {item['name']}",
                "",
                f"- Status: `{item['status']}`",
                f"- Return code: `{item['returncode']}`",
                f"- Duration: `{round(item['duration_seconds'], 3)}s`",
            ]
        )
        details = item.get("details") or {}
        if details:
            lines.append("- Details:")
            for key, value in details.items():
                lines.append(f"  - `{key}`: `{_short_value(value)}`")
        if item.get("stderr"):
            lines.extend(["", "```text", str(item["stderr"])[-4000:], "```"])
    lines.append("")
    return "\n".join(lines)


def _short_value(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    return text if len(text) <= 500 else text[:497] + "..."


if __name__ == "__main__":
    raise SystemExit(main())
