# Refresh 跳过中间缺失日期

- 日期：2026-07-23
- 状态：设计中（等待用户审阅）
- 范围：仅 `refresh_latest_data` 自动补齐逻辑

## 背景

当前 `refresh_latest_data`（[src/trend_radar/refresh.py](../src/trend_radar/refresh.py)）启动时会通过 `_refresh_date_sequence` + `_missing_weekday_radar_dates` 扫描 `data/radar/` 已有日期与 `target_date` 之间所有缺失的工作日，把它们逐个 `fetch_data` + `generate_scores`，再统一写 `latest.json`。

用户反馈：中间缺几天数据时不再值得为它们重拉历史行情；只要能保证最近一个完整交易日的数据可用即可。本次设计把这条自动补齐行为关掉，其他命令与辅助函数保持原样。

## 目标

- `refresh_latest_data`（Streamlit 启动、UI 按钮、CLI `python -m trend_radar refresh`）只针对 `target_date` 一天做拉取与评分。
- 中间缺失的工作日不会被自动 `fetch_data`、不会创建 `data/normalized/<date>/`、不会生成 `data/radar/<date>.{csv,json}`。
- 单日行为本身（覆盖率检测、不完整产物清理、checkpoint、信号处理）保持不变。

## 非目标

- 不动 `retry_selected_date`：UI/CLI 手动补单日仍可用。
- 不动 `scan_problem_snapshots` / `retry_problem_snapshots`：独立入口，行为不变。
- 不动 `_refresh_date_sequence` / `_missing_weekday_radar_dates` / `_previous_weekday` / `_refresh_success_message` 等辅助函数本身（保留不调用，以便未来如需恢复补齐能力）。
- 不动 `backfill` / `validate` / `audit` 子命令。
- 不动 `fetcher.py`、`__main__.py`、`app.py`（Streamlit 启动只读 `refresh_status.json`，没有 gap 概念）。

## 设计

### 行为变化

`refresh_latest_data` 唯一变化发生在决定要拉哪些日期时：

- `force=True` 或 `data/radar/<target_date>.csv` 不存在 → `dates_to_refresh = [target_date]`。
- 否则 `dates_to_refresh = []`，走 `REFRESH_SKIPPED` 分支。

随后 `for refresh_date in dates_to_refresh` 循环最多执行 0 或 1 次，循环体不变。

### 状态消息

`_refresh_success_message` 内部按 `gap_dates = [d for d in refreshed_dates if d != target_date]` 判断是否有"补齐"信息。本次改动后 `gap_dates` 永远为空，因此消息会自然退化为：

- 成功 + `actual_date == target_date`：`已生成 <target_date> 数据`（来自 `_success_message`）。
- 成功 + `actual_date < target_date`：`数据源最新为 <actual_date>，已生成对应数据`。
- 跳过：`当前已是最新 <target_date> 数据`。
- 失败 / 中断：`更新失败` / `更新已中断（进度已保存）`，未变。

`已补齐 N 个缺失日期：…` 字段不再出现。

### 覆盖率与不完整产物处理（保持原状）

- `_detect_complete_history_date(root, source_date, min_coverage_ratio=HISTORY_COMPLETE_COVERAGE_RATIO=0.95)` 仍按 95% 候选 ETF 历史到该日才算完整。
- 当 `actual_date != target_date` 时，`_remove_incomplete_target_outputs` 仍删除 `data/radar/<target_date>.{csv,json}`，避免脏的"今天"被展示。
- `_promote_latest_from_radar` 只在 `target_date` 实际被刷新时覆盖 `data/state/latest.json`。

### 代码变更点

仅修改 [src/trend_radar/refresh.py](../src/trend_radar/refresh.py) 中 `refresh_latest_data` 函数体内、`existing_radar` 赋值之后、`dates_to_refresh` 计算处。

把：

```python
dates_to_refresh = _refresh_date_sequence(root, target_date, force)
```

替换为：

```python
dates_to_refresh = (
    [target_date] if force or not existing_radar.exists() else []
)
```

其余判断（`target_refreshed`、`final_actual_date`、消息组装、文件清理、`latest.json` 覆盖）无需调整，因为它们原本就只对 `dates_to_refresh` 中的元素生效。

### 行为示意

| 场景 | 现行为 | 改后行为 |
| --- | --- | --- |
| 已有 `2026-07-15.csv`（周三），`target_date=2026-07-22`（周三）且无 07-22.csv | 依次拉 07-16/17/20/21/22 共 5 个工作日 | 只拉 07-22 一天 |
| `force=True` + `2026-07-22.csv` 已存在 | 仍只拉 07-22（gap 在 07-22 之后） | 只拉 07-22 一天 |
| `2026-07-22.csv` 已存在且非 `force` | `REFRESH_SKIPPED` | `REFRESH_SKIPPED`（不变） |
| 拉取后 `actual_date = 2026-07-21`（覆盖率不足） | 删 `2026-07-22.csv\|json` | 删 `2026-07-22.csv\|json`（不变） |

## 测试

在 [tests/test_refresh.py](../tests/test_refresh.py) 调整：

1. **删除** `test_missing_weekday_gap_is_backfilled_before_target`（断言 `2026-06-05` 会被自动补齐）与 `test_existing_target_still_backfills_missing_weekday_gap`（断言 target 雷达已存在时仍会补齐 gap）—— 这两个用例直接验证本次要移除的行为，留下来必失败。
2. **新增** `test_refresh_does_not_backfill_intermediate_weekday_gap`：
   - 预写 `data/radar/2026-07-15.csv`，不写 2026-07-16/17/20/21/22 任何工作日。
   - mock `fetcher` / `scorer` 记录调用，`now` 取 `2026-07-22 16:00 Asia/Shanghai`。
   - 调 `refresh_latest_data(force=False, ...)`。
   - 断言：`fetcher` 仅被以 `data_date="2026-07-22"` 调用一次；`scorer` 也只被调一次；`data/normalized/2026-07-16/` 等中间目录不存在；`result.message` 不含 "已补齐"，等于 `已生成 2026-07-22 数据`。
3. **新增** `test_existing_target_radar_skips_when_intermediate_dates_missing`：预写 `2026-07-22.csv` 且中间日期都缺，调 `refresh_latest_data(force=False, ...)` 断言返回 `REFRESH_SKIPPED` 且 `fetcher` 未被调用（取代旧 `test_existing_target_still_backfills_missing_weekday_gap`）。
4. 其余既有用例（`test_existing_target_radar_skips_without_fetching`、`test_missing_target_fetches_then_scores_and_writes_success`、`test_stale_source_scores_actual_latest_history_date`、`test_partial_target_coverage_scores_recent_complete_history_date`、`test_fetch_progress_is_written_to_status`、`test_refresh_passes_resume_and_worker_options_to_fetcher`、lock / retry / scan_problem 等）原样通过。
5. 跑 `.venv/bin/python scripts/run_tests.py --mode offline` 全绿。

## 文档

- [CLAUDE.md](../CLAUDE.md) `## Commands` 段：`refresh` 的语义由"自动补齐最新完整交易日"改为"只处理最新一个交易日，缺失的中间日期不再自动拉取"。
- [README.md](../README.md) `## 采集与评分` 段：同步同一信息；保留 `backfill` 段不变（其目的本来就是用最近一份缓存回放历史）。
- 不动 `PLAN.md`、`docs/a-share-etf-radar-plan.md`、`docs/functional-test-step.md`。

## 风险与回滚

- **风险**：用户原本依赖 `refresh` 自动补齐的历史。回退：恢复 `_refresh_date_sequence` 那一行即可，辅助函数未删除。
- **风险**：`data/normalized/` 中可能残留旧的 gap 日期目录（之前补齐留下的）。本次不清理，用户可手工删除或后续单独处理。
- **影响面**：Streamlit 启动行为会变化；UI 顶部 `数据状态` 指标文案自动从"已生成 …"生成，无须改前端。
