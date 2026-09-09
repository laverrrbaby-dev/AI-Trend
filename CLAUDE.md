# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A-share ETF trend radar — a local Streamlit dashboard that discovers and ranks A-share equity ETFs by multi-factor scoring. Data sourced from free public APIs (AkShare), cached locally as CSV/JSON. Scope is limited to A-share equity ETFs; bonds, REITs, commodities, and cross-border ETFs are excluded. This is a discovery/observation tool, not a trading signal system.

## Commands

### Development

```bash
# Install dependencies and package (editable)
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install -e .

# Run Streamlit app
streamlit run app.py

# Quick launch (auto-creates venv, opens browser)
./open_trend_radar.command
```

### Data Pipeline (all via `python -m trend_radar`)

```bash
python -m trend_radar fetch --date 2026-05-28        # Fetch and cache ETF data
python -m trend_radar score --date 2026-05-28        # Generate radar rankings from cached data
python -m trend_radar refresh                         # Auto-fetch+score newest complete trading day only; intermediate missing dates are NOT auto-filled
python -m trend_radar refresh --force                 # Force refresh even if target radar exists
python -m trend_radar backfill --months 1             # Backfill historical rankings from existing cache
python -m trend_radar validate --start YYYY-MM-DD --end YYYY-MM-DD  # Validate outputs
python -m trend_radar audit --start YYYY-MM-DD --end YYYY-MM-DD --external --repair  # Audit + repair
```

### Testing

```bash
# Offline unit tests (fixture-based, no network, no local data/ dependency)
.venv/bin/python scripts/run_tests.py --mode offline

# Live smoke test (hits real APIs, writes to output/live-smoke/)
.venv/bin/python scripts/run_tests.py --mode live --date auto

# Audit existing local cache (writes to output/test-reports/)
.venv/bin/python scripts/run_tests.py --mode audit --date 2026-05-29
```

### Checkpoint Commits (per AGENTS.md convention)

```bash
python3 scripts/checkpoint_commit.py --summary "<short summary>" --details "<what changed>"
```
This runs the default test command, writes a session log, and creates a local `checkpoint: ...` commit. Only do this when the user asks to commit/checkpoint. Do not push automatically.

## Architecture

### Data Pipeline

```
AkShare APIs  →  data/raw/<date>/        (raw API responses cached as CSV)
              →  data/normalized/<date>/  (normalized spot, history, shares)
              →  data/radar/<date>.csv    (final ranking, one row per ETF)
              →  data/radar/<date>.json   (Top10, changes, quality metadata)
              →  data/state/latest.json   (most recent result for UI default)
```

The pipeline has two stages: `fetch` (acquire + normalize) and `score` (rank + output). The `refresh` command orchestrates both, with automatic date resolution (Shanghai time, post-15:30 = today, otherwise = previous trading day).

### Scoring Model

Four-factor additive model (0-100):
- **成交额热度 (45%)**: Top30 residency in last 20 days + latest amount rank + 5d/20d expansion ratio
- **份额流入 (20%)**: 5d/20d share change; bonus if price up + shares up, penalty if price up + shares down
- **趋势强度 (25%)**: Weighted 20d/60d/120d returns (45/35/20), favoring strong trends
- **趋势结构 (10%)**: EMA13/EMA26/EMA144/EMA169 alignment (multi-timeframe structure)

Liquidity filters: ≥120 listing days, ≥0.30亿 latest amount, ≥0.20亿 20d avg amount, ≥2亿 market cap.

### Module Map

| Module | Role |
|---|---|
| `fetcher.py` | Acquire spot/history/share data from AkShare (Eastmoney, Sina), thread-pooled history fetches, multi-tier fallback |
| `normalization.py` | Normalize raw API responses to stable internal column names, unit conversions (to 亿) |
| `filters.py` | ETF universe classification — exclude bonds/REITs/commodities/cross-border by keyword + code prefix |
| `scoring.py` | Core ranking engine — compute scores, detect Top10 changes, cross-validate top pool against primary source |
| `config.py` | Shared constants: paths (`data/raw`, `data/normalized`, `data/radar`, `data/state`), thresholds, timezone |
| `refresh.py` | Orchestration: auto-refresh with file locks, progress reporting, problem snapshot scanning |
| `backfill.py` | Generate historical radar snapshots from existing normalized source |
| `validation.py` | Validate CSV/JSON outputs against UI+backend expectations |
| `audit.py` | End-to-end audit: re-compute Top30 residency, turnover ranks, external history reconciliation, repair |
| `ui_data.py` | UI data access layer: real cache first, mock fallback when cache missing |
| `mock_data.py` | Static mock ETF profiles/history for UI prototyping and fallback |
| `crypto.py` | Multi-exchange crypto spot turnover snapshots (Binance, OKX, etc.) |
| `us_stocks.py` | US stock turnover snapshots from public scanner data |
| `__main__.py` | CLI entry point with subcommands: fetch, score, backfill, validate, audit, refresh |
| `app.py` | Streamlit UI: Top10, full ranking, changes, single-ETF detail charts, date switching, refresh controls |

### Data Quality System

Every ETF row carries quality metadata:
- **数据置信度** (高/中/低): Based on source flags
- **行情来源**: Which provider served the history (东方财富, 新浪兜底, 实时行情补点, etc.)
- **份额来源**: Share data provenance (上交所/深交所/实时行情份额)
- **数据问题**: Human-readable issue descriptions

History fetching has a multi-tier fallback: Eastmoney (urllib → curl → curl+proxy) → Sina → spot snapshot fallback. Cross-validation during scoring re-fetches suspect Top N rankings from the primary source to detect stale/mismatched data.

### Test Conventions

- Offline tests use `TREND_RADAR_TEST_MODE=offline` env var; no network access, no `data/` dependency. Fixtures in `tests/helpers/data_factory.py`.
- Live smoke uses `--codes` to limit to 3 specific ETFs, writes to `output/live-smoke/` (never pollutes `data/`).
- Cache audit runs validate + audit on existing `data/` outputs.
- Network diagnostics: `python scripts/diagnose_network.py` tests connectivity to data sources.

## Agent skills

### Issue tracker

Issues and specs use local Markdown files under `.scratch/`. See `docs/agents/issue-tracker.md`.

### Domain docs

This repository uses a single-context domain-doc layout. See `docs/agents/domain.md`.
