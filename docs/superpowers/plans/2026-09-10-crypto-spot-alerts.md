# Crypto Spot Attention Alerts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add startup-triggered, UTC+8 daily attention alerts for BTC, ETH, SOL, XAUT, and BNB without producing trading instructions.

**Architecture:** Keep the existing multi-exchange turnover collector intact for compatibility, and add focused modules for fixed-universe OHLCV acquisition, pure signal calculation, durable event/state storage, and background refresh coordination. Binance supplies UTC+8 daily candles for BTC/ETH/SOL/BNB; OKX supplies XAUT history. Streamlit starts one process-wide background refresh before rendering the default US-stock page, then reads atomically published snapshots.

**Tech Stack:** Python 3.11+, pandas, requests, Plotly, Streamlit, unittest; no new runtime dependency.

**Spec:** `docs/crypto-spot-alert-design.md`

## Global Constraints

- Work in an isolated worktree on branch `codex/crypto-spot-alerts`; never implement directly on `main`.
- The main checkout contains unrelated user changes in `src/trend_radar/us_stocks.py` and `tests/test_us_stocks.py`; do not copy, stage, overwrite, or commit them.
- Monitor and display exactly `BTC`, `ETH`, `SOL`, `XAUT`, `BNB`, in that order.
- Use complete UTC+8 daily candles for every calculation; an in-progress candle may be drawn but never affects state or alerts.
- Use Binance `1d` klines with `timeZone=8` for BTC/ETH/SOL/BNB and OKX `1D` candles for XAUT. Do not splice OHLCV histories from different venues.
- Fetch and retain at least 500 complete candles per symbol. Replay the latest 180 complete candles only as `historical_backfill`; never mark replayed events unread.
- Produce attention/change notices only. Never emit buy, sell, entry, exit, position sizing, stop-loss, or profit-target language.
- Preserve existing crypto turnover public interfaces and offline tests unless a task explicitly extends their output.
- Store runtime market data, state, presets, and events under ignored `data/crypto/alerts/`; commit only the read-only default configuration under `config/`.
- Historical events are append-only and carry `algorithm_version`, `preset_id`, source, candle date, confirmation date, and evidence.
- A source-switch or failed quality check keeps the last good state, marks the symbol for review, and emits no new strategy event.
- Follow strict RED-GREEN-REFACTOR for every production behavior below.

---

### Task 1: Fixed Universe and Versioned Configuration

**Files:**
- Create: `config/crypto_alerts.default.json`
- Create: `src/trend_radar/crypto_alert_config.py`
- Create: `tests/test_crypto_alert_config.py`

**Interfaces:**
- Produces: `CRYPTO_ALERT_SYMBOLS`, `ALGORITHM_VERSION`, `CryptoAlertConfig`, `load_crypto_alert_config(root, preset_id=None)`, `save_crypto_alert_preset(root, preset_id, payload)`, `list_crypto_alert_presets(root)`.
- Persists user presets in `data/crypto/alerts/presets/<preset_id>.json` and active selection in `data/crypto/alerts/config.json`.

- [ ] **Step 1: Write failing configuration tests**

```python
class CryptoAlertConfigTests(unittest.TestCase):
    def test_default_config_has_fixed_order_and_sources(self):
        config = load_crypto_alert_config(root=PROJECT_ROOT)
        self.assertEqual(list(config.symbols), ["BTC", "ETH", "SOL", "XAUT", "BNB"])
        self.assertEqual(config.sources["BTC"].primary, "binance")
        self.assertEqual(config.sources["XAUT"].primary, "okx")
        self.assertEqual(config.history_bars, 500)
        self.assertEqual(config.timezone, "Asia/Shanghai")

    def test_named_preset_round_trips_without_mutating_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            saved = save_crypto_alert_preset(root, "quiet", {"enabled_alerts": ["td"]})
            self.assertEqual(saved.preset_id, "quiet")
            self.assertEqual(load_crypto_alert_config(root, "quiet").enabled_alerts, ("td",))
            self.assertIn("default", list_crypto_alert_presets(root))
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `.venv/bin/python -m unittest tests.test_crypto_alert_config -v`

Expected: import failure for `trend_radar.crypto_alert_config`.

- [ ] **Step 3: Implement immutable defaults and validated presets**

Use frozen dataclasses. Reject preset IDs outside `[a-zA-Z0-9_-]{1,40}`, unknown alert names, symbol changes outside the fixed universe, non-positive periods, or malformed JSON. The committed default must encode:

```json
{
  "preset_id": "default",
  "algorithm_version": "crypto-alerts-v1",
  "timezone": "Asia/Shanghai",
  "symbols": ["BTC", "ETH", "SOL", "XAUT", "BNB"],
  "history_bars": 500,
  "replay_bars": 180,
  "startup_refresh": true,
  "sources": {
    "BTC": {"primary": "binance", "pair": "BTCUSDT", "backup": "okx", "backup_pair": "BTC-USDT"},
    "ETH": {"primary": "binance", "pair": "ETHUSDT", "backup": "okx", "backup_pair": "ETH-USDT"},
    "SOL": {"primary": "binance", "pair": "SOLUSDT", "backup": "okx", "backup_pair": "SOL-USDT"},
    "XAUT": {"primary": "okx", "pair": "XAUT-USDT", "backup": "binance", "backup_pair": "XAUTUSDT"},
    "BNB": {"primary": "binance", "pair": "BNBUSDT", "backup": "okx", "backup_pair": "BNB-USDT"}
  },
  "enabled_alerts": ["dow", "price_action", "divergence", "td", "ma120", "vegas"],
  "parameters": {
    "short_pivot": 3,
    "main_pivot": 5,
    "rsi_period": 14,
    "atr_period": 14,
    "sma_period": 120,
    "vegas_fast": 144,
    "vegas_slow": 169,
    "divergence_min_gap": 5,
    "divergence_max_gap": 60,
    "divergence_match_radius": 2,
    "touch_atr": 0.5,
    "engulfing_body_ratio": 1.2,
    "pin_wick_body_ratio": 2.0,
    "pin_other_wick_body_max": 0.5,
    "delayed_false_break_bars": 3,
    "close_deviation_pct": 0.5,
    "xaut_close_deviation_pct": 1.0
  }
}
```

- [ ] **Step 4: Run config tests and the existing offline suite**

Run: `.venv/bin/python -m unittest tests.test_crypto_alert_config -v`

Run: `.venv/bin/python scripts/run_tests.py --mode offline`

- [ ] **Step 5: Commit the task**

```bash
git add config/crypto_alerts.default.json src/trend_radar/crypto_alert_config.py tests/test_crypto_alert_config.py
git commit -m "feat: add crypto alert configuration"
```

### Task 2: UTC+8 OHLCV Acquisition and Cache

**Files:**
- Create: `src/trend_radar/crypto_market_data.py`
- Create: `tests/test_crypto_market_data.py`
- Modify: `src/trend_radar/config.py`

**Interfaces:**
- Consumes: `CryptoAlertConfig` and its per-symbol source specs.
- Produces: `CANDLE_COLUMNS`, `CryptoHistoryResult`, `fetch_crypto_history(symbol, config, root, now=None, fetch_json=None)`, `fetch_latest_backup_candle(symbol, config, now=None, fetch_json=None)`, `load_crypto_history(root, symbol)`, `latest_complete_crypto_date(now=None)`.
- `CryptoHistoryResult.frame` contains complete candles only; `CryptoHistoryResult.in_progress` optionally contains the current forming candle for chart display and is never fed into indicators.
- Cache schema: `timestamp,日期,开盘价,最高价,最低价,收盘价,成交量,成交额,source,pair,is_complete` in `data/crypto/alerts/history/<symbol>.csv`.

- [ ] **Step 1: Write failing Binance and OKX parser tests**

```python
def test_binance_request_uses_china_daily_boundary_and_drops_open_candle(self):
    result = fetch_crypto_history("BTC", config, root, now=NOW, fetch_json=fake_binance)
    self.assertIn("interval=1d", seen_url)
    self.assertIn("timeZone=8", seen_url)
    self.assertIn("limit=500", seen_url)
    self.assertEqual(result.frame["日期"].tolist(), ["2026-09-08", "2026-09-09"])
    self.assertTrue(result.frame["is_complete"].all())
    self.assertEqual(result.in_progress["日期"], "2026-09-10")

def test_xaut_uses_okx_utc8_daily_candles_and_confirm_flag(self):
    result = fetch_crypto_history("XAUT", config, root, now=NOW, fetch_json=fake_okx)
    self.assertIn("bar=1D", seen_url)
    self.assertNotIn("1Dutc", seen_url)
    self.assertEqual(result.source, "okx")
    self.assertEqual(result.frame["收盘价"].tolist(), [2500.0, 2510.0])
```

Also test malformed rows, duplicate timestamps, 500-row pagination, atomic cache replacement, incremental merge, and preserving the old cache when the fetch fails.

- [ ] **Step 2: Run market-data tests and verify RED**

Run: `.venv/bin/python -m unittest tests.test_crypto_market_data -v`

Expected: import failure for `trend_radar.crypto_market_data`.

- [ ] **Step 3: Implement source adapters and normalization**

For Binance parse `[open_time, open, high, low, close, volume, close_time, quote_volume, ...]`; request `timeZone=8`, page backward when needed, and define completeness as `close_time < now_utc_ms`. For OKX parse `[ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]`; request `bar=1D`, `limit=300`, page with `after`, and require `confirm == "1"` for the persisted frame. Return the newest `confirm == "0"`/not-yet-closed row separately as `in_progress`. Validate monotonic unique timestamps, finite positive OHLC, `low <= min(open, close)`, `high >= max(open, close)`, and non-negative volume.

- [ ] **Step 4: Implement atomic cache writes**

Write to a sibling `.tmp` file, parse it back, then replace the final CSV. On a failed refresh, leave the previous CSV byte-for-byte unchanged. Add `CRYPTO_ALERT_DIR` creation to `ensure_data_dirs()`.

- [ ] **Step 5: Run focused and offline tests**

Run: `.venv/bin/python -m unittest tests.test_crypto_market_data tests.test_crypto_turnover -v`

Run: `.venv/bin/python scripts/run_tests.py --mode offline`

- [ ] **Step 6: Commit the task**

```bash
git add src/trend_radar/config.py src/trend_radar/crypto_market_data.py tests/test_crypto_market_data.py
git commit -m "feat: cache UTC+8 crypto OHLCV history"
```

### Task 3: Pure Indicators, Pivots, and TD Counts

**Files:**
- Create: `src/trend_radar/crypto_indicators.py`
- Create: `tests/test_crypto_indicators.py`

**Interfaces:**
- Produces: `IndicatorFrame`, `calculate_indicators(frame, config)`, `confirmed_pivots(frame, left, right)`, `td_continuous_counts(close)`.
- Adds columns: `sma120`, `ema144`, `ema169`, `macd`, `macd_signal`, `macd_hist`, `rsi14`, `obv`, `atr14`, `swing_high_3`, `swing_low_3`, `swing_high_5`, `swing_low_5`, `td_up`, `td_down`.

- [ ] **Step 1: Write failing literal-fixture tests**

Create hand-derived fixtures that catch wrong rolling windows, look-ahead pivots, and TD resets:

```python
def test_td_counts_compare_close_four_bars_back_and_lock_after_13(self):
    close = pd.Series([10, 10, 10, 10] + list(range(11, 26)) + [25, 24])
    counts = td_continuous_counts(close)
    self.assertEqual(counts.loc[12, "td_up"], 9)
    self.assertEqual(counts.loc[16, "td_up"], 13)
    self.assertEqual(counts.loc[17, "td_up"], 13)
    self.assertEqual(counts.loc[18, "td_up"], 0)

def test_pivot_is_visible_only_after_right_hand_confirmation(self):
    frame = candle_frame(high=[1, 2, 5, 2, 1], low=[0, 0, 0, 0, 0])
    pivots = confirmed_pivots(frame, left=2, right=2)
    self.assertEqual(pivots.iloc[2]["confirmed_at_index"], 4)
```

Use known literal vectors for SMA, EMA, Wilder RSI, Wilder ATR, MACD histogram, and OBV. Do not compute expected values with the production helpers.

- [ ] **Step 2: Run indicator tests and verify RED**

Run: `.venv/bin/python -m unittest tests.test_crypto_indicators -v`

- [ ] **Step 3: Implement indicators without a new TA dependency**

Use `Series.ewm(adjust=False)` for MACD and Vegas EMAs. Implement Wilder RMA with an SMA seed followed by the recursive `(previous * (period - 1) + current) / period` formula for RSI and ATR. Treat equal TD closes as a reset; after 13 keep the count locked at 13 until interruption or reversal. Mark pivots at the pivot candle but attach a separate confirmation index/date to prevent look-ahead alerts.

- [ ] **Step 4: Run focused tests and refactor duplicate rolling logic**

Run: `.venv/bin/python -m unittest tests.test_crypto_indicators -v`

- [ ] **Step 5: Commit the task**

```bash
git add src/trend_radar/crypto_indicators.py tests/test_crypto_indicators.py
git commit -m "feat: calculate crypto technical indicators"
```

### Task 4: Deterministic Attention Event Engine

**Files:**
- Create: `src/trend_radar/crypto_signals.py`
- Create: `tests/test_crypto_signals.py`

**Interfaces:**
- Consumes: normalized candles plus indicator columns.
- Produces: `CryptoEvent`, `CryptoCurrentState`, `evaluate_crypto_signals(symbol, frame, config) -> tuple[CryptoCurrentState, list[CryptoEvent]]`.
- Event types: `dow_change`, `breakout`, `same_day_rejection`, `delayed_false_break`, `level_reaction`, `engulfing`, `pin_bar`, `regular_divergence`, `hidden_divergence`, `td9`, `td13`, `ma120_cross`, `ma120_touch`, `ma120_retest_exit`, `vegas_break`, `vegas_touch`, `vegas_retest_exit`.

- [ ] **Step 1: Write failing tests for Dow, TD, and moving-average events**

Assert exact event dates and evidence from synthetic OHLCV sequences:

```python
def test_dow_changes_only_when_confirmed_main_pivots_change_structure(self):
    state, events = evaluate_crypto_signals("BTC", dow_fixture(), config)
    self.assertEqual(state.trend, "上升结构")
    self.assertEqual([(e.event_type, e.confirmed_date) for e in events], [("dow_change", "2026-06-20")])

def test_td_emits_only_first_9_and_13_in_same_continuous_run(self):
    _, events = evaluate_crypto_signals("BTC", td_fixture(), config)
    self.assertEqual([(e.event_type, e.direction) for e in events], [("td9", "上涨计数"), ("td13", "上涨计数")])
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m unittest tests.test_crypto_signals.CryptoTrendSignalTests -v`

- [ ] **Step 3: Implement Dow, TD, SMA120, and Vegas state transitions**

Classify 5/5 pivots as `上升结构` only when the two latest confirmed highs and lows both rise; `下降结构` only when both fall; otherwise `震荡/未确认`. Vegas upper/lower are `max(ema144, ema169)` and `min(...)`. Crosses require a complete candle close to move across the boundary. Touch uses `0.5 * ATR14`. Retest-exit requires a prior confirmed break, a later touch/entry, then a close back outside.

- [ ] **Step 4: Write failing price-action tests**

Cover main/short pivot breakout, same-day wick rejection, close-break followed by a three-bar return, ATR touch/reaction, body engulfing at 1.2x, and pin-bar ratios. Assert that Inside Bar emits no event.

- [ ] **Step 5: Verify price-action RED, implement, and rerun GREEN**

Run: `.venv/bin/python -m unittest tests.test_crypto_signals.CryptoPriceActionTests -v`

- [ ] **Step 6: Write failing divergence tests**

Cover all four definitions for MACD histogram, RSI14, and OBV:

- regular bullish: price lower low, indicator higher low;
- regular bearish: price higher high, indicator lower high;
- hidden bullish: price higher low, indicator lower low;
- hidden bearish: price lower high, indicator higher high.

Assert 5–60 bar spacing, ±2-bar indicator matching, confirmation-date emission, MACD zero-side `典型/弱` labels, and RSI 30/70 `典型/普通` labels.

- [ ] **Step 7: Verify divergence RED, implement, and rerun GREEN**

Run: `.venv/bin/python -m unittest tests.test_crypto_signals.CryptoDivergenceTests -v`

- [ ] **Step 8: Run the complete signal and offline suites**

Run: `.venv/bin/python -m unittest tests.test_crypto_indicators tests.test_crypto_signals -v`

Run: `.venv/bin/python scripts/run_tests.py --mode offline`

- [ ] **Step 9: Commit the task**

```bash
git add src/trend_radar/crypto_signals.py tests/test_crypto_signals.py
git commit -m "feat: detect crypto attention events"
```

### Task 5: Append-Only Event Store, Replay, and Unread State

**Files:**
- Create: `src/trend_radar/crypto_alert_store.py`
- Create: `tests/test_crypto_alert_store.py`

**Interfaces:**
- Produces: `publish_crypto_result(root, result)`, `load_crypto_dashboard(root)`, `load_crypto_events(root, symbol=None)`, `mark_crypto_symbol_read(root, symbol, event_ids)`, `replay_crypto_history(root, config)`.
- Files: `data/crypto/alerts/events.jsonl`, `state/<symbol>.json`, `snapshots/<date>.json`, `read_state.json`, `baseline.json`.

- [ ] **Step 1: Write failing persistence tests**

Test that identical `(symbol,event_type,direction,candle_date,algorithm_version,preset_id)` events deduplicate, events remain append-only, same-day multiple events merge only in dashboard output, replay events carry `historical_backfill=true`, replay does not increment unread count, and algorithm-version changes create new audit events without overwriting old ones.

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m unittest tests.test_crypto_alert_store -v`

- [ ] **Step 3: Implement atomic per-symbol publication**

Write state and snapshot JSON via temporary files and `Path.replace()`. Serialize each JSONL event as one complete line under a module lock. If one symbol fails, publish successful symbols and preserve the failed symbol's prior state with `quality="待复核"`, `stale=true`, and its last successful data date.

- [ ] **Step 4: Implement baseline and 180-bar replay semantics**

On first run, evaluate only the configured replay window, save events as historical, write a baseline date equal to the latest complete candle, and set unread count to zero. On later runs, only events confirmed after the baseline and not previously stored become unread.

- [ ] **Step 5: Run focused and offline tests**

Run: `.venv/bin/python -m unittest tests.test_crypto_alert_store -v`

Run: `.venv/bin/python scripts/run_tests.py --mode offline`

- [ ] **Step 6: Commit the task**

```bash
git add src/trend_radar/crypto_alert_store.py tests/test_crypto_alert_store.py
git commit -m "feat: persist crypto alert history and state"
```

### Task 6: Quality Check and Refresh Orchestration

**Files:**
- Create: `src/trend_radar/crypto_alert_refresh.py`
- Create: `tests/test_crypto_alert_refresh.py`

**Interfaces:**
- Produces: `CryptoAlertRefreshResult`, `refresh_crypto_alerts(root=PROJECT_ROOT, force=False, now=None, fetch_json=None)`, `read_crypto_alert_refresh_status(root)`, `crypto_alert_refresh_is_active()`, `start_crypto_alert_refresh_once(root=PROJECT_ROOT, force=False) -> bool`.

- [ ] **Step 1: Write failing quality and partial-failure tests**

Use complete Binance/OKX fake payloads. Assert that BTC/ETH/SOL/BNB and XAUT use their configured primary sources, backup close deviation above 0.5% (1% for XAUT) marks review, no event is published for a failed symbol, and successful symbols still publish.

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m unittest tests.test_crypto_alert_refresh.CryptoAlertRefreshTests -v`

- [ ] **Step 3: Implement the orchestrator**

For each fixed symbol: load/fetch primary history, call `fetch_latest_backup_candle()` for the latest complete date, validate date and close deviation, calculate indicators/signals from the complete frame, then publish atomically. Preserve `in_progress` only for chart rendering. Status JSON must contain per-symbol phases, completed/total counts, target date, timestamps, unread count, warnings, and errors.

- [ ] **Step 4: Write failing concurrency and cache-skip tests**

Assert that ten simultaneous `start_crypto_alert_refresh_once()` calls start exactly one daemon thread; a second call while running returns `False`; an unchanged complete target date/config hash/algorithm version skips network; `force=True` still cannot overlap an active task.

- [ ] **Step 5: Verify RED and implement process-wide coordination**

Use one module-level `threading.Lock`, one active-thread reference, and persisted status recovery for a prior process that died while marked `running`. Never rely on `st.session_state` for task ownership.

- [ ] **Step 6: Run focused and offline tests**

Run: `.venv/bin/python -m unittest tests.test_crypto_alert_refresh -v`

Run: `.venv/bin/python scripts/run_tests.py --mode offline`

- [ ] **Step 7: Commit the task**

```bash
git add src/trend_radar/crypto_alert_refresh.py tests/test_crypto_alert_refresh.py
git commit -m "feat: orchestrate crypto alert refresh"
```

### Task 7: Startup Trigger and Global Status UI

**Files:**
- Modify: `app.py`
- Create: `tests/test_crypto_startup_ui.py`
- Modify: `tests/test_functional_contracts.py`

**Interfaces:**
- Consumes: `start_crypto_alert_refresh_once`, `read_crypto_alert_refresh_status`, `load_crypto_dashboard`.
- Produces app helpers: `_start_crypto_background_work()`, `_crypto_market_label(status)`, `_crypto_global_notice(status, session_state)`.

- [ ] **Step 1: Write failing startup behavior tests**

Import `app` with the existing Streamlit stubs and assert observable helper behavior: startup delegates once to the process coordinator before market-specific rendering; running status formats as a quiet progress label; unread results format `币圈现货 · N`; unchanged success produces no toast; failure and review status produce one notice per status revision.

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m unittest tests.test_crypto_startup_ui -v`

- [ ] **Step 3: Move the trigger to `main()` before `_market_selector()`**

Call `_start_crypto_background_work()` immediately after page/style/lifecycle initialization. Remove the page-local ownership check from `_render_crypto_turnover_page()`. Keep its status polling, but point it at the new alert refresh status.

- [ ] **Step 4: Add non-blocking global feedback**

Format the segmented-control label with unread count while preserving the stable internal market value. Show a compact status fragment during active work. Toast only when a new status revision reports unread changes, failure, or review; never switch tabs or show a modal.

- [ ] **Step 5: Run UI and offline tests**

Run: `.venv/bin/python -m unittest tests.test_crypto_startup_ui tests.test_functional_contracts -v`

Run: `.venv/bin/python scripts/run_tests.py --mode offline`

- [ ] **Step 6: Commit the task**

```bash
git add app.py tests/test_crypto_startup_ui.py tests/test_functional_contracts.py
git commit -m "feat: start crypto alerts with the app"
```

### Task 8: Fixed Five-Coin Dashboard and Candlestick Detail

**Files:**
- Modify: `app.py`
- Create: `tests/test_crypto_dashboard_ui.py`

**Interfaces:**
- Produces: `_crypto_dashboard_table(dashboard)`, `_crypto_candlestick_chart(history, state, overlays)`, `_render_crypto_alert_settings(config)`.

- [ ] **Step 1: Write failing table tests**

Assert exactly five rows in fixed order even when turnover data omits XAUT; columns include `币种`, `当前状态`, `最新变化`, `变化日期`, `数据截至`, `数据质量`, plus available turnover background fields. Assert multiple same-day events render as one compact list and no text contains trading directives.

- [ ] **Step 2: Verify table RED, implement table, rerun GREEN**

Run: `.venv/bin/python -m unittest tests.test_crypto_dashboard_ui.CryptoDashboardTableTests -v`

- [ ] **Step 3: Write failing chart tests**

With real Plotly available, assert traces for candlestick, SMA120, EMA144, EMA169 and optional pivot/TD/divergence overlays. Assert an incomplete candle uses a distinct opacity/name and complete-only indicator values do not extend into it.

- [ ] **Step 4: Verify chart RED, implement detail expander, rerun GREEN**

Run: `.venv/bin/python -m unittest tests.test_crypto_dashboard_ui.CryptoChartTests -v`

- [ ] **Step 5: Write failing settings tests**

Test pure payload helpers for enabling alert families, changing numeric parameters, saving a named preset, selecting one active preset, disabling startup refresh, and marking a symbol read when its detail is opened.

- [ ] **Step 6: Implement settings UI and refresh controls**

Provide a read-only default preset, named user presets, one active preset, overlay checkboxes, `仅看有变化`, and a manual `重新检查` button. Saving or switching a preset updates config but formal alerts begin with the next complete candle.

- [ ] **Step 7: Run UI and complete offline tests**

Run: `.venv/bin/python -m unittest tests.test_crypto_dashboard_ui -v`

Run: `.venv/bin/python scripts/run_tests.py --mode offline`

- [ ] **Step 8: Commit the task**

```bash
git add app.py tests/test_crypto_dashboard_ui.py
git commit -m "feat: render crypto attention dashboard"
```

### Task 9: CLI, Documentation, and Offline Contract

**Files:**
- Modify: `src/trend_radar/__main__.py`
- Modify: `README.md`
- Modify: `docs/crypto-spot-alert-design.md`
- Create: `tests/test_crypto_alert_cli.py`

**Interfaces:**
- Adds: `python -m trend_radar crypto-alerts [--force] [--format table|json]`.

- [ ] **Step 1: Write failing CLI tests**

Patch only external fetching while exercising the real parser and formatter. Assert table output lists five symbols and latest changes; JSON output is valid and contains `generated_at`, `target_date`, `status`, `symbols`, and `events`; failed refresh exits non-zero without deleting cached output.

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m unittest tests.test_crypto_alert_cli -v`

- [ ] **Step 3: Implement CLI and update docs**

Document startup background behavior, fixed symbols, UTC+8 boundary, primary/backup sources, data locations, configuration/presets, event semantics, manual command, and the explicit non-advice boundary. Change the design document's pending sections to `已确认` and link the implementation plan.

- [ ] **Step 4: Run focused and offline tests**

Run: `.venv/bin/python -m unittest tests.test_crypto_alert_cli -v`

Run: `.venv/bin/python scripts/run_tests.py --mode offline`

- [ ] **Step 5: Commit the task**

```bash
git add src/trend_radar/__main__.py README.md docs/crypto-spot-alert-design.md tests/test_crypto_alert_cli.py
git commit -m "docs: expose crypto alert workflow"
```

### Task 10: End-to-End Verification and Checkpoint

**Files:**
- Modify only if verification exposes a defect: files already listed in Tasks 1–9 and their corresponding tests.
- Generated, ignored verification output: `output/test-reports/`, `output/live-smoke/`, `data/crypto/alerts/`.

**Interfaces:**
- Verifies the complete startup-to-dashboard story and creates the repository-required session checkpoint.

- [ ] **Step 1: Run fresh complete offline verification**

Run: `.venv/bin/python scripts/run_tests.py --mode offline`

Expected: exit code 0 with `offline_unittest: passed`.

- [ ] **Step 2: Run a bounded live crypto smoke test**

Run: `.venv/bin/python -m trend_radar crypto-alerts --force --format json`

Verify manually from the JSON:

- exactly five symbols and fixed order;
- BTC/ETH/SOL/BNB source is Binance and XAUT source is OKX;
- latest complete candle date is a UTC+8 completed day;
- every symbol has at least 500 rows except when a source explicitly reports insufficient history;
- no event is generated for a symbol marked `待复核`;
- no buy/sell/entry/exit wording appears.

If network access is unavailable, record the live smoke as `external_unavailable`; do not fabricate a pass.

- [ ] **Step 3: Launch the app and verify startup behavior**

Run: `./open_trend_radar.command`

Verify that the first visible page is still 美股, the crypto task starts without visiting its tab, the UI remains responsive, only one task runs across reruns, the crypto label/status updates, and opening 币圈现货 shows five rows plus chart/settings.

- [ ] **Step 4: Review scope and repository status**

Run: `git status --short --branch`

Run: `git diff --check HEAD~9..HEAD`

Confirm no unrelated US-stock files or user changes are present in the branch diff.

- [ ] **Step 5: Create the required checkpoint commit**

Run:

```bash
python3 scripts/checkpoint_commit.py \
  --summary "实现币圈现货关注提醒" \
  --details "新增固定五币 UTC+8 OHLCV、技术指标与变化提醒、历史去重、启动后台刷新、配置预设、页面图表及 CLI。" \
  --path config/crypto_alerts.default.json \
  --path src/trend_radar/config.py \
  --path src/trend_radar/crypto_alert_config.py \
  --path src/trend_radar/crypto_market_data.py \
  --path src/trend_radar/crypto_indicators.py \
  --path src/trend_radar/crypto_signals.py \
  --path src/trend_radar/crypto_alert_store.py \
  --path src/trend_radar/crypto_alert_refresh.py \
  --path src/trend_radar/__main__.py \
  --path app.py \
  --path README.md \
  --path docs/crypto-spot-alert-design.md \
  --path tests/test_crypto_alert_config.py \
  --path tests/test_crypto_market_data.py \
  --path tests/test_crypto_indicators.py \
  --path tests/test_crypto_signals.py \
  --path tests/test_crypto_alert_store.py \
  --path tests/test_crypto_alert_refresh.py \
  --path tests/test_crypto_startup_ui.py \
  --path tests/test_crypto_dashboard_ui.py \
  --path tests/test_crypto_alert_cli.py \
  --path tests/test_functional_contracts.py
```

- [ ] **Step 6: Report evidence**

Report the branch, commits, checkpoint hash, exact offline result, live-smoke result, manual startup observations, and session-log path. Do not push unless explicitly requested.

## Plan Self-Review

- Spec coverage: fixed universe, UTC+8 complete candles, Binance/OKX sources, 500-bar history, all requested indicators/events, non-advice wording, configuration/presets, immutable history, startup background refresh, global status, partial failure, terminal output, chart overlays, and validation are each assigned to a task.
- Placeholder scan: every implementation and verification step is concrete; no unresolved stand-in remains.
- Type consistency: configuration flows from Task 1 to market data and indicators; `CryptoEvent`/`CryptoCurrentState` flow from Task 4 to store, refresh, CLI, and UI; refresh status is the single startup/UI coordination contract.
- Isolation: execution begins in `codex/crypto-spot-alerts`; unrelated dirty main-checkout files remain outside the worktree.
