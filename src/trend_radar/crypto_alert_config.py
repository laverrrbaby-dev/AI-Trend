"""Validated configuration for the fixed crypto attention-alert universe."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT

CRYPTO_ALERT_SYMBOLS = ("BTC", "ETH", "SOL", "XAUT", "BNB")
ALGORITHM_VERSION = "crypto-alerts-v1"
_ALERT_NAMES = frozenset({"dow", "price_action", "divergence", "td", "ma120", "vegas"})
_PRESET_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


@dataclass(frozen=True)
class CryptoSource:
    primary: str
    pair: str
    backup: str
    backup_pair: str


@dataclass(frozen=True)
class CryptoAlertConfig:
    preset_id: str
    algorithm_version: str
    timezone: str
    symbols: tuple[str, ...]
    history_bars: int
    replay_bars: int
    startup_refresh: bool
    sources: dict[str, CryptoSource]
    enabled_alerts: tuple[str, ...]
    parameters: dict[str, float | int]


def _default_path(root: Path) -> Path:
    return root / "config" / "crypto_alerts.default.json"


def _default_payload(root: Path) -> dict[str, Any]:
    path = _default_path(root)
    if not path.exists():
        path = _default_path(PROJECT_ROOT)
    return _read_json(path)


def _preset_path(root: Path, preset_id: str) -> Path:
    return root / "data" / "crypto" / "alerts" / "presets" / f"{preset_id}.json"


def _parse(payload: dict[str, Any], preset_id: str) -> CryptoAlertConfig:
    symbols = tuple(str(item).upper() for item in payload.get("symbols", CRYPTO_ALERT_SYMBOLS))
    if symbols != CRYPTO_ALERT_SYMBOLS:
        raise ValueError("crypto alert symbols are fixed to BTC, ETH, SOL, XAUT, BNB")
    alerts = tuple(str(item) for item in payload.get("enabled_alerts", ()))
    if not set(alerts) <= _ALERT_NAMES:
        raise ValueError("unknown crypto alert name")
    history_bars = int(payload.get("history_bars", 500))
    replay_bars = int(payload.get("replay_bars", 180))
    if history_bars < 500 or replay_bars <= 0:
        raise ValueError("invalid crypto history length")
    source_payload = payload.get("sources") or {}
    sources: dict[str, CryptoSource] = {}
    for symbol in CRYPTO_ALERT_SYMBOLS:
        raw = source_payload.get(symbol) or {}
        sources[symbol] = CryptoSource(
            primary=str(raw["primary"]),
            pair=str(raw["pair"]),
            backup=str(raw["backup"]),
            backup_pair=str(raw["backup_pair"]),
        )
    parameters = {str(key): value for key, value in (payload.get("parameters") or {}).items()}
    return CryptoAlertConfig(
        preset_id=preset_id,
        algorithm_version=str(payload.get("algorithm_version", ALGORITHM_VERSION)),
        timezone=str(payload.get("timezone", "Asia/Shanghai")),
        symbols=symbols,
        history_bars=history_bars,
        replay_bars=replay_bars,
        startup_refresh=bool(payload.get("startup_refresh", True)),
        sources=sources,
        enabled_alerts=alerts,
        parameters=parameters,
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid crypto alert config: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"crypto alert config must be an object: {path}")
    return payload


def load_crypto_alert_config(root: Path = PROJECT_ROOT, preset_id: str | None = None) -> CryptoAlertConfig:
    selected = preset_id or "default"
    payload = _default_payload(root) if selected == "default" else _read_json(_preset_path(root, selected))
    return _parse(payload, selected)


def save_crypto_alert_preset(root: Path, preset_id: str, payload: dict[str, Any]) -> CryptoAlertConfig:
    if not _PRESET_RE.fullmatch(preset_id) or preset_id == "default":
        raise ValueError("invalid preset id")
    merged = {**_default_payload(root), **payload, "preset_id": preset_id}
    config = _parse(merged, preset_id)
    path = _preset_path(root, preset_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return config


def list_crypto_alert_presets(root: Path = PROJECT_ROOT) -> tuple[str, ...]:
    directory = root / "data" / "crypto" / "alerts" / "presets"
    names = [path.stem for path in directory.glob("*.json")] if directory.exists() else []
    return tuple(sorted({"default", *names}))
