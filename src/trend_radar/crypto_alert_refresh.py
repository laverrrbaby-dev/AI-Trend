from __future__ import annotations

import json
import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT
from .crypto_alert_config import load_crypto_alert_config
from .crypto_alert_store import mark_crypto_symbol_review, publish_crypto_result
from .crypto_indicators import calculate_indicators
from .crypto_market_data import fetch_crypto_history, fetch_latest_backup_candle, latest_complete_crypto_date
from .crypto_signals import CryptoCurrentState, evaluate_crypto_signals

_LOCK = threading.Lock()
_ACTIVE: threading.Thread | None = None


@dataclass(frozen=True)
class CryptoAlertRefreshResult:
    status: str
    completed: int
    total: int
    warnings: tuple[str, ...] = ()


def refresh_crypto_alerts(root: Path = PROJECT_ROOT, force: bool = False, now: datetime | None = None, fetch_json=None) -> CryptoAlertRefreshResult:
    config = load_crypto_alert_config(root)
    baseline_path = root / "data" / "crypto" / "alerts" / "baseline.json"
    target_date = latest_complete_crypto_date(now)
    if not force and _baseline_matches(baseline_path, config.algorithm_version, config.preset_id, target_date):
        status = {"status": "skipped", "completed": 0, "total": len(config.symbols), "target_date": target_date,
                  "finished_at": datetime.now(UTC).isoformat(), "message": "当前完整日线已完成计算"}
        _write_status(root, status)
        return CryptoAlertRefreshResult("skipped", 0, len(config.symbols))
    initial_replay = not baseline_path.exists()
    completed_dates: list[str] = []
    statuses: dict[str, Any] = {"status": "running", "started_at": datetime.now(UTC).isoformat(), "completed": 0, "total": len(config.symbols), "symbols": {}}
    _write_status(root, statuses)
    warnings: list[str] = []
    completed = 0
    for symbol in config.symbols:
        try:
            history = fetch_crypto_history(symbol, config, root, now=now, fetch_json=fetch_json)
            backup = fetch_latest_backup_candle(symbol, config, now=now, fetch_json=fetch_json)
            if not _backup_agrees(symbol, history.frame, backup, config):
                raise ValueError("primary and backup close prices require review")
            frame = calculate_indicators(history.frame, config)
            state, events = evaluate_crypto_signals(symbol, frame, config)
            publish_crypto_result(root, state, [replace(event, historical_backfill=True) for event in events] if initial_replay else events)
            if state.data_date:
                completed_dates.append(state.data_date)
            statuses["symbols"][symbol] = {"phase": "succeeded", "data_date": state.data_date}
        except Exception as exc:  # a single coin must not abort the other four
            warnings.append(f"{symbol}: {exc}")
            mark_crypto_symbol_review(root, symbol, str(exc))
            statuses["symbols"][symbol] = {"phase": "review", "error": str(exc)}
        completed += 1
        statuses["completed"] = completed
        _write_status(root, statuses)
    statuses.update({"status": "succeeded" if not warnings else "partial", "finished_at": datetime.now(UTC).isoformat(), "warnings": warnings})
    if initial_replay and completed_dates:
        _write_baseline(baseline_path, config.algorithm_version, config.preset_id, max(completed_dates))
    _write_status(root, statuses)
    return CryptoAlertRefreshResult(statuses["status"], completed, len(config.symbols), tuple(warnings))


def start_crypto_alert_refresh_once(root: Path = PROJECT_ROOT, force: bool = False) -> bool:
    global _ACTIVE
    with _LOCK:
        if _ACTIVE is not None and _ACTIVE.is_alive():
            return False
        _ACTIVE = threading.Thread(target=refresh_crypto_alerts, args=(root, force), daemon=True, name="crypto-alert-refresh")
        _ACTIVE.start()
        return True


def crypto_alert_refresh_is_active() -> bool:
    return _ACTIVE is not None and _ACTIVE.is_alive()


def read_crypto_alert_refresh_status(root: Path = PROJECT_ROOT) -> dict[str, Any]:
    path = root / "data" / "crypto" / "alerts" / "refresh_status.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"status": "idle", "completed": 0, "total": 5}


def _write_status(root: Path, status: dict[str, Any]) -> None:
    directory = root / "data" / "crypto" / "alerts"
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / "refresh_status.json.tmp"
    temporary.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(directory / "refresh_status.json")


def _write_baseline(path: Path, algorithm_version: str, preset_id: str, date: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"algorithm_version": algorithm_version, "preset_id": preset_id, "date": date}, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _baseline_matches(path: Path, algorithm_version: str, preset_id: str, target_date: str) -> bool:
    if not path.exists():
        return False
    try:
        baseline = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return baseline == {"algorithm_version": algorithm_version, "preset_id": preset_id, "date": target_date}


def _backup_agrees(symbol: str, primary, backup: dict[str, Any] | None, config) -> bool:
    if backup is None or primary.empty:
        return False
    latest = primary.iloc[-1]
    if str(latest["日期"]) != str(backup.get("日期")):
        return False
    primary_close = float(latest["收盘价"])
    backup_close = float(backup["收盘价"])
    if primary_close <= 0:
        return False
    limit = float(config.parameters["xaut_close_deviation_pct"] if symbol == "XAUT" else config.parameters["close_deviation_pct"])
    return abs(primary_close - backup_close) / primary_close * 100 <= limit
