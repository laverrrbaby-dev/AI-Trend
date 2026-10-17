# Trade Records V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Save real trades from a passed checklist, maintain one active position, and show UTC+8 daily trailing-stop suggestions.

**Architecture:** Separate validated record storage, public OKX market data/suggestion rules, and Streamlit UI. Preserve original checklist snapshots and keep record persistence independent of drafts. Use locked atomic JSON writes and per-record revisions to reject stale edits.

**Tech Stack:** Existing Python/Streamlit/requests, unittest/AppTest, Decimal, filesystem storage; no new runtime dependency.

**Spec:** `docs/superpowers/specs/2026-10-17-trade-records-v1-design.md`

## Global Constraints

- OKX cross margin; fixed 500U checklist; at most one active trade.
- Actual execution is manually confirmed; original checklist snapshot never changes.
- Stops only tighten, initially relative to the saved estimated liquidation price.
- UTC+08:00 daily candles close at Beijing midnight; confirmed candles only.
- Closed edits require a second confirmation; no automated execution, closing, notifications, or historical analysis.
- Follow AGENTS.md copy deployment and checkpoint requirements. Work on the current branch as authorized by the implementation request.

## Review Focus

- Duplicate submissions and concurrent pages must not create a second active trade or overwrite newer changes.
- Malformed records and failed writes must preserve the previous file and show an actionable error.
- Wrong UTC candle boundaries, stale ticker data, unfinished candles, or missing yesterday must not produce a valid suggestion.
- Widget navigation and failed saves must not lose user input or reuse a stale historical confirmation.
- Same-price and looser stops must not be persisted, including changes disguised as execution corrections.

## Test boundaries

Extend the previously agreed rule/store and Streamlit AppTest boundaries. Use actual temporary storage and public interfaces. Substitute only the external HTTP boundary and the clock for deterministic market-data cases.

### Task 1: Validated trade lifecycle

Files: create `src/trend_radar/trade_records.py`, `tests/test_trade_records.py`.

Interface: `TradeRecordStore(root)` with `list_records`, `create`, `update_execution`, `update_stop`, `close`, `update_closed`, and `delete`. Mutations accept expected record revision. Actual execution includes exact OKX instrument identity to avoid using spot prices for a derivative position; all timestamps use timezone-aware ISO format. Closed results include time, signed PnL, reason, optional notes.

- [x] Write a failing lifecycle test using a passed checklist, real temporary store, close, confirmed historical correction, and immutable snapshot assertions.
- [x] Implement the lifecycle with validation, one-active constraint, atomic locked persistence, and revision checks; run the single test file.
- [x] Add failing cases for unpassed/unconfirmed creation, invalid numbers/times, duplicate active creation, stale edits, stop direction/equality, historical confirmation, corruption and write failure; implement each behavior in vertical slices.

### Task 2: UTC+8 market data and suggestions

Files: create `src/trend_radar/trade_record_market.py`, `tests/test_trade_record_market.py`.

Interfaces: `fetch_stop_market(instrument_id, now=None, fetch_json=None)` returns validated instrument-specific latest ticker and yesterday's completed UTC+8 candle. `suggest_stop(record, market)` returns tighten/keep/crossed result and reference price.

- [x] Write a failing worked-example long suggestion test; implement strict tightening and crossed-price handling, then mirror for shorts.
- [x] Test the actual HTTP adapter with a supplied external JSON transport: request OKX `bar=1D`, reject UTC boundaries, unfinished/missing yesterday, wrong instrument, stale/future ticker, malformed values and API errors.
- [x] Verify official OKX docs and smoke-test real public endpoints without account credentials.

### Task 3: Checklist save and record pages

Files: create `src/trend_radar/trade_records_ui.py`, `tests/test_trade_records_ui.py`; modify `src/trend_radar/trade_checklist_ui.py`, `app.py`, existing checklist UI test helper if new inputs require targeting.

- [x] Write a failing AppTest for passed-checklist actual-execution confirmation and save, then implement save UI, duplicate suppression and independent record storage.
- [x] Write AppTest lifecycle flow: active correction, tightened stop, manual close confirmation, history edit second confirmation and delete; implement focused forms and snapshot display.
- [x] Cover navigation, storage failures, stale record revision handling and unavailable market data without internal mocks.
- [x] Fetch current suggestions on page entry, retain timestamped result across form reruns, and invalidate on instrument/direction changes. Never replace recorded stop from a suggestion automatically.

### Task 4: Review, verify, deploy

- [x] Typecheck changed modules regularly; run focused tests after each slice.
- [x] Apply code-review skill with independent Standards/Spec reviewers; fix material findings.
- [x] Run full real dependency test suite once via checkpoint script, record local commit and session log.
- [x] Run `scripts/update_local_runtime.sh`, verify source-root/app/src synchronization, then browser-test deployed navigation and save/close/history flow without retaining fake records or overwriting live data.

## Execution authorization

User confirmed the full spec and explicitly requested implementation. Continue in this session using existing test boundaries; do not pause again for a redundant plan approval.

## Verification record

- Record lifecycle tests: 9 passed; market/suggestion tests: 6 passed; new Streamlit flows: 12 passed.
- Existing checklist UI and alert UI focused suites passed; mypy passed on all four changed feature modules.
- Independent Standards review: no documented violations or actionable smells. Spec review: no important gaps; inner-page reentry refresh suggestion implemented and tested RED to GREEN.
- Full-suite result is recorded in the implementation checkpoint session log.
- Deployment completed with recorded source root and identical app/source files (excluding interpreter-generated bytecode caches).
- Real browser verified actual fill versus original plan, one-active save suppression, manual stop update, close preview and confirmation, and historical correction preview and confirmation. Browser console: zero errors.
- Unique browser test trade removed; runtime draft restored byte-for-byte from its pre-test backup.
