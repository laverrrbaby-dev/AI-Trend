from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .crypto_signals import CryptoCurrentState, CryptoEvent

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
        unread = sum(1 for event in all_events if not event.get("historical_backfill", False) and event.get("event_id") not in read_state.get(event.get("symbol"), []))
        state_dir = directory / "state"
        states = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(state_dir.glob("*.json"))]
        states.sort(key=lambda item: ["BTC", "ETH", "SOL", "XAUT", "BNB"].index(item["symbol"]) if item["symbol"] in {"BTC", "ETH", "SOL", "XAUT", "BNB"} else 99)
        dashboard = {"generated_at": _now_iso(), "unread_count": unread, "symbols": states, "events": all_events}
        _atomic_json(directory / "dashboard.json", dashboard)
        return dashboard


def load_crypto_events(root: Path, symbol: str | None = None) -> list[dict[str, Any]]:
    events = _read_jsonl(root / "data" / "crypto" / "alerts" / "events.jsonl")
    return [event for event in events if symbol is None or event.get("symbol") == symbol]


def load_crypto_dashboard(root: Path) -> dict[str, Any]:
    path = root / "data" / "crypto" / "alerts" / "dashboard.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"unread_count": 0, "symbols": [], "events": []}


def mark_crypto_symbol_read(root: Path, symbol: str, event_ids: list[str]) -> dict[str, Any]:
    dashboard = load_crypto_dashboard(root)
    read_path = root / "data" / "crypto" / "alerts" / "read_state.json"
    read_state = json.loads(read_path.read_text(encoding="utf-8")) if read_path.exists() else {}
    read_state.setdefault(symbol, [])
    read_state[symbol] = sorted(set(read_state[symbol]) | set(event_ids))
    _atomic_json(read_path, read_state)
    unread = sum(1 for event in dashboard.get("events", []) if not event.get("historical_backfill", False) and event.get("event_id") not in read_state.get(event.get("symbol"), []))
    dashboard["unread_count"] = unread
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
            "historical_backfill": event.historical_backfill}


def _state_payload(state: CryptoCurrentState) -> dict[str, Any]:
    return {"symbol": state.symbol, "status": state.status, "trend": state.trend, "latest_change": state.latest_change,
            "latest_change_date": state.latest_change_date, "data_date": state.data_date, "quality": state.quality, "stale": state.stale}


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
