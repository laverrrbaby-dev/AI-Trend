from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from .crypto_alert_config import CryptoAlertConfig
from .crypto_market_data import load_crypto_history
from .crypto_signals import CryptoCurrentState, CryptoEvent
from .crypto_signals import evaluate_crypto_signals

_LOCK = threading.Lock()


def publish_crypto_result(root: Path, state: CryptoCurrentState, events: list[CryptoEvent]) -> dict[str, Any]:
    directory = root / "data" / "crypto" / "alerts"
    (directory / "state").mkdir(parents=True, exist_ok=True)
    with _LOCK:
        existing = {event["event_id"] for event in _read_jsonl(directory / "events.jsonl")}
        with (directory / "events.jsonl").open("a", encoding="utf-8") as handle:
            for event in events:
                if event.event_id in existing:
                    continue
                handle.write(json.dumps(_event_payload(event), ensure_ascii=False) + "\n")
                existing.add(event.event_id)
        _atomic_json(directory / "state" / f"{state.symbol}.json", _state_payload(state))
        all_events = _read_jsonl(directory / "events.jsonl")
        read_path = directory / "read_state.json"
        read_state = json.loads(read_path.read_text(encoding="utf-8")) if read_path.exists() else {}
        state_dir = directory / "state"
        states = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(state_dir.glob("*.json"))]
        states.sort(key=lambda item: ["BTC", "ETH", "SOL", "XAUT", "BNB"].index(item["symbol"]) if item["symbol"] in {"BTC", "ETH", "SOL", "XAUT", "BNB"} else 99)
        active_events = [event for event in all_events if _event_matches_active_state(event, states)]
        for event in active_events:
            event["is_unread"] = not event.get("historical_backfill", False) and event.get("event_id") not in read_state.get(event.get("symbol"), [])
        unread = sum(1 for event in active_events if event["is_unread"])
        dashboard = {"generated_at": _now_iso(), "unread_count": unread, "symbols": states, "events": active_events}
        _atomic_json(directory / "dashboard.json", dashboard)
        if state.data_date:
            snapshot_path = directory / "snapshots" / f"{state.data_date}.json"
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_json(snapshot_path, dashboard)
        return dashboard


def load_crypto_events(root: Path, symbol: str | None = None) -> list[dict[str, Any]]:
    events = _read_jsonl(root / "data" / "crypto" / "alerts" / "events.jsonl")
    return [event for event in events if symbol is None or event.get("symbol") == symbol]


def load_crypto_dashboard(root: Path) -> dict[str, Any]:
    path = root / "data" / "crypto" / "alerts" / "dashboard.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"unread_count": 0, "symbols": [], "events": []}


def replay_crypto_history(root: Path, config: CryptoAlertConfig) -> None:
    baseline_dates: list[str] = []
    for symbol in config.symbols:
        history = load_crypto_history(root, symbol)
        if history.empty:
            continue
        window = history.tail(config.replay_bars)
        state, events = evaluate_crypto_signals(symbol, window, config)
        source = str(window.iloc[-1].get("source", "cache"))
        historical_events = [replace(event, historical_backfill=True, evidence={**event.evidence, "source": source}) for event in events]
        historical_state = replace(
            state,
            status="无新变化",
            latest_change="暂无新的策略变化",
            latest_change_date=None,
            alert_level="none",
            algorithm_version=config.algorithm_version,
            preset_id=config.preset_id,
        )
        publish_crypto_result(root, historical_state, historical_events)
        baseline_dates.append(str(window.iloc[-1]["日期"]))
    baseline = {"algorithm_version": config.algorithm_version, "preset_id": config.preset_id,
                "date": max(baseline_dates) if baseline_dates else None}
    _atomic_json(root / "data" / "crypto" / "alerts" / "baseline.json", baseline)


def mark_crypto_symbol_read(root: Path, symbol: str, event_ids: list[str]) -> dict[str, Any]:
    dashboard = load_crypto_dashboard(root)
    read_path = root / "data" / "crypto" / "alerts" / "read_state.json"
    read_state = json.loads(read_path.read_text(encoding="utf-8")) if read_path.exists() else {}
    read_state.setdefault(symbol, [])
    read_state[symbol] = sorted(set(read_state[symbol]) | set(event_ids))
    _atomic_json(read_path, read_state)
    for event in dashboard.get("events", []):
        event["is_unread"] = not event.get("historical_backfill", False) and event.get("event_id") not in read_state.get(event.get("symbol"), [])
    unread = sum(1 for event in dashboard.get("events", []) if event.get("is_unread"))
    dashboard["unread_count"] = unread
    for state in dashboard.get("symbols", []):
        if state.get("symbol") == symbol and not any(event.get("symbol") == symbol and event.get("is_unread") for event in dashboard.get("events", [])):
            state.update({"status": "无新变化", "alert_level": "none"})
            state_path = root / "data" / "crypto" / "alerts" / "state" / f"{symbol}.json"
            _atomic_json(state_path, state)
    _atomic_json(root / "data" / "crypto" / "alerts" / "dashboard.json", dashboard)
    return dashboard


def mark_crypto_symbol_review(root: Path, symbol: str, error: str) -> dict[str, Any]:
    directory = root / "data" / "crypto" / "alerts"
    state_path = directory / "state" / f"{symbol}.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {
            "symbol": symbol,
            "status": "待复核",
            "trend": "震荡/未确认",
            "latest_change": "暂无新的策略变化",
            "latest_change_date": None,
            "data_date": None,
        }
        state.update({"status": "待复核", "quality": "待复核", "stale": True, "error": error})
        _atomic_json(state_path, state)
        states = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((directory / "state").glob("*.json"))]
        dashboard = load_crypto_dashboard(root)
        dashboard["symbols"] = states
        _atomic_json(directory / "dashboard.json", dashboard)
        return dashboard


def _event_payload(event: CryptoEvent) -> dict[str, Any]:
    return {"event_id": event.event_id, "symbol": event.symbol, "event_type": event.event_type, "direction": event.direction,
            "candle_date": event.candle_date, "confirmed_date": event.confirmed_date, "message": event.message,
            "evidence": event.evidence, "algorithm_version": event.algorithm_version, "preset_id": event.preset_id,
            "historical_backfill": event.historical_backfill, "alert_level": event.alert_level}


def _state_payload(state: CryptoCurrentState) -> dict[str, Any]:
    return {"symbol": state.symbol, "status": state.status, "trend": state.trend, "latest_change": state.latest_change,
            "latest_change_date": state.latest_change_date, "data_date": state.data_date, "quality": state.quality, "stale": state.stale,
            "alert_level": state.alert_level, "algorithm_version": state.algorithm_version, "preset_id": state.preset_id}


def _event_matches_active_state(event: dict[str, Any], states: list[dict[str, Any]]) -> bool:
    state = next((item for item in states if item.get("symbol") == event.get("symbol")), None)
    if not state:
        return False
    algorithm_version = state.get("algorithm_version")
    preset_id = state.get("preset_id")
    if not algorithm_version and not preset_id:
        return True
    return event.get("algorithm_version") == algorithm_version and event.get("preset_id") == preset_id


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _now_iso() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat()
