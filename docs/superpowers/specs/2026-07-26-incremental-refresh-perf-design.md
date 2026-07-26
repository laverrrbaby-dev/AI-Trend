# 增量刷新性能 + 进度可见性优化设计

- 日期: 2026-07-26
- 范围: `src/trend_radar/fetcher.py`、`src/trend_radar/refresh.py`、`app.py`、`src/trend_radar/ui_data.py`、`src/trend_radar/config.py`、`tests/`
- 目标场景: 日常增量刷新（大部分数据已缓存）
- 不在范围: 冷启动全量拉取、`backfill`、`audit`、异步重构

## 目标

- 日常增量刷新从启动到出排行榜 < 1 分钟
- 进度可见：阶段、当前代码、速度（codes/sec）、ETA（秒）
- 不破坏三个硬约束：断点续传、`status.json` 向后兼容、`scoring.py` 不动

## 诊断（已通过重读源码确认）

| # | 位置 | 问题 | 证据 |
|---|---|---|---|
| R1 | `fetcher._load_or_fetch_spot` (713-728) | `resume=True` 时直接 `return cached_spot`，不校验 `data_date` → 下游 `spot_matches_target=False` → `spot_history_fallbacks={}` → 600+ 代码全走 eastmoney 全级联 | 重读源码确认 |
| R2 | `_fetch_history_with_attempt_details` (1116-1138) + `_fetch_history_sina` (1266-1297) | 2 次重试 × (urllib 15s + curl 15s + proxy curl 15s) + sina 兜底；sina 调用 `ak.fund_etf_hist_sina(...)` 无超时 | 重读源码确认 |
| R3 | `refresh._detect_complete_history_date` (607-644) | fetch 刚验证完每个文件，又全量重读 ~600 个历史 CSV 算 `date_counts`。原设计想复用 `HistoryFetchOutcome.latest_date`，但 review 发现 `latest_date` 只有最新日期，而 `_detect_complete_history_date` 需要每个代码的所有历史日期 | 重读源码确认 |
| R4 | `refresh.progress_callback` (165-178) | 在 `as_completed` 主线程同步调 `_write_status`（temp + replace + json.dumps），阻塞下一个 future 处理 | 重读源码确认 |
| R5 | `refresh.scan_problem_snapshots` (490-534) + `app._refresh_status_view` (770) | 每次 UI 5s 轮询都全量扫 `data/normalized` + 读所有历史文件 | 重读源码确认 |
| R6 | `refresh._progress_payload` (905-925) | 只有 `completed/total`，无速度/ETA/阶段耗时 | 重读源码确认 |

## 设计

### 1. 拉取层（`fetcher.py`）

#### 1.1 R1 修复：spot resume 时校验日期（主路径 + 3 级回退）

`_load_or_fetch_spot(ak, raw_path, normalized_path, resume, target)` 增加 `target` 参数：

1. resume + cached_spot 有效 + `_frame_dates_match_target(cached_spot, target)` → 返回 cached_spot（**当前路径，最优**）
2. cached_spot stale 或不存在 → 尝试 `ak.fund_etf_spot_em`（~1s，今日实时 spot）
3. akshare 调用失败/返回 stale → 用 `raw_dir/spot_em.csv`（昨日 raw）+ warning
4. raw 也没有 → 用 stale cached_spot + warning（**当前行为，保底**）

调用方 `fetch_data` 在 1.1 情况下设置 `candidate_source="target_spot_liquidity"`、`spot_matches_target=True`，下游 `spot_history_fallbacks` 对 ~600 代码生效，跳过 eastmoney。

#### 1.2 R2 修复：缩短重试级联 + sina 超时

- `_fetch_history_with_attempt_details`：2 次重试 → 1 次
- `HISTORY_FETCH_PER_CODE_TIMEOUT`：60s → 15s（`config.py`）
- `DEFAULT_HISTORY_FETCH_MAX_WORKERS`：12 → 20（`config.py`）
- proxy curl 仅在 `_history_proxy()` 非空时才尝试（当前每次都进 except 分支）
- `_fetch_history_sina` 调用 `ak.fund_etf_hist_sina(...)` 包超时：用单线程 `concurrent.futures` + `result(timeout=10)`，超时记为 error 跳过（不阻塞整体）

#### 1.3 R3 数据复用：fetch 阶段维护共享 `date_counts`

原方案想复用 `HistoryFetchOutcome.latest_date`，review 发现数据不够（只需要最新日期，但 `_detect_complete_history_date` 需要每个代码的所有历史日期）。

修正方案：

- `fetch_data` 维护共享 `date_counts: dict[str, int]`（加 `threading.Lock`）
- 每个代码 `_load_or_fetch_history` 完成后，把它的 `all_dates: tuple[str, ...]` 通过 outcome 返回（**新增字段，additive**）
- `as_completed` 主循环在拿到 outcome 后，加锁累加到 `date_counts`
- `fetch_data` 返回时把 `date_counts` 一并返回给 `refresh_latest_data`（通过新增 `FetchResult.date_counts` 字段，additive）
- `_detect_complete_history_date` 新增可选参数 `date_counts: dict[str, int] | None = None`：
  - 传入 → 直接算覆盖率，**0 文件读**
  - 不传入 → 保持当前全量重读行为（保底，向后兼容）

#### 1.4 HistoryFetchOutcome 新增字段（additive）

```python
@dataclass(frozen=True)
class HistoryFetchOutcome:
    # ... 现有字段不变 ...
    all_dates: tuple[str, ...] = ()  # 新增：该代码所有有效历史日期（YYYY-MM-DD），用于 date_counts 累加
```

`_load_or_fetch_history` 在验证 history 时顺便收集 `all_dates`，填入 outcome。checkpoint 重建路径 `_rebuild_outcomes_from_checkpoint` 读取一次 CSV 填充 `all_dates`（仅 resume 启动时一次，不阻塞主流程）。

### 2. 编排层（`refresh.py`）

#### 2.1 R4 修复：后台线程限频写 status

新增 `_ProgressWriter`：

- 进度回调入队（`queue.Queue`，非阻塞）
- 后台线程消费队列，限频写盘（≤2 次/秒，phase 切换立即写）
- 写盘用 `temp + replace`（现有原子语义不变）
- 线程异常 → 回退同步写（保底）
- `refresh_latest_data` 在 fetch/score 各阶段结束时 `join()` 确保最后一条状态写入

`progress_callback` 改为：

```python
def progress_callback(progress: dict[str, Any]) -> None:
    progress_queue.put_nowait(progress)  # 非阻塞
```

#### 2.2 R3 修复：`_detect_complete_history_date` 复用 date_counts

```python
def _detect_complete_history_date(
    root: Path,
    source_date: str,
    min_coverage_ratio: float = HISTORY_COMPLETE_COVERAGE_RATIO,
    date_counts: dict[str, int] | None = None,  # 新增
) -> str | None:
    if date_counts is not None:
        # 快速路径：0 文件读
        if not date_counts:
            return None
        candidate_codes = _candidate_codes(root, source_date)
        denominator = len(candidate_codes) if candidate_codes else max(date_counts.values())
        min_count = max(1, int(denominator * min_coverage_ratio + 0.999999))
        complete_dates = sorted(d for d, c in date_counts.items() if c >= min_count)
        if complete_dates:
            return complete_dates[-1]
        best_date = max(date_counts, key=lambda d: (date_counts[d], d))
        return best_date
    # 慢速路径：当前实现（保底）
    ...
```

`refresh_latest_data` 在 fetch 后调用 `_detect_complete_history_date(root, refresh_date, date_counts=fetch_result.date_counts)`。

#### 2.3 R6 修复：progress 增加 speed/ETA/阶段耗时（additive）

`_progress_payload` 新增字段：

- `phase_started_at`: ISO timestamp，phase 切换时记录
- `phase_elapsed_seconds`: `now - phase_started_at`
- `speed`: `completed / max(phase_elapsed_seconds, 1)`（**修正：不用最近 10 个完成时间窗口**，避免 cache hit 拉高速度导致 ETA 偏短）
- `eta_seconds`: `(total - completed) / max(speed, 0.01)`

所有字段 additive，`status.json` 现有字段不变。

### 3. UI 层（`app.py` + `ui_data.py`）

#### 3.1 R5 修复：`problem_snapshot_summary` 缓存

`refresh.problem_snapshot_summary` 加内存缓存：

- key: `(max_child_mtime_ns, subdir_count)`，其中 `max_child_mtime_ns = max(child.stat().st_mtime_ns for child in normalized_dir.iterdir())`
- TTL: 60s（保底，防 mtime 在某些文件系统精度不够）
- refresh 完成时显式 invalidate（通过模块级 `_problem_summary_cache` 变量 + 清除函数）
- 缓存 miss → 重新计算（不影响正确性）

**修正（review 发现原 key 不灵敏）**：父目录 mtime 在子目录内部文件变化时不变，因此用 `max(child.stat().st_mtime_ns)` 而非父目录 mtime。

#### 3.2 R5 修复：`_read_fetch_report` memoize

`app._read_fetch_report(data_date)` 加 memoize：

- key: `(data_date, file_mtime_ns)`
- 命中 → 返回 cached dict
- 未命中 → 读文件 + 缓存

#### 3.3 R6 渲染：`_running_refresh_message` 展示 speed/ETA

```
正在更新 2026-07-25 数据：历史行情 120/600，缓存 118，新拉取 2，失败 0，并发 20，5.2 码/秒，预计 92s
```

仅当 `speed > 0` 且 `eta_seconds > 0` 时显示 speed/ETA。

## 数据流（增量刷新场景）

1. UI 触发 refresh → 后台线程 `refresh_latest_data`
2. `_ProgressWriter` 后台线程启动
3. `fetch_data` 校验 spot 日期 → 过期则重拉（~1s）→ spot fallback 对 ~600 代码生效
4. 极少数代码（spot 没覆盖）走 eastmoney 单次重试（≤15s）+ sina 超时兜底（≤10s）
5. 每个代码完成 → outcome 带 `all_dates` → 加锁累加到 `date_counts`
6. 进度回调入队 → `_ProgressWriter` 限频写 `status.json`（不阻塞 fetch）
7. fetch 完成 → `FetchResult.date_counts` 传给 refresh
8. `_detect_complete_history_date(date_counts=...)` → 0 文件读
9. score → 完成 → `join()` 进度线程
10. UI 5s 轮询 → 读 cached `problem_summary` + cached `fetch_report` → 渲染 speed/ETA

## 错误处理（保底回退）

- spot 重拉失败 → 三级回退（akshare → raw → stale cached）+ warning
- `_ProgressWriter` 线程异常 → 回退同步写
- `date_counts` 缺失或为空 → `_detect_complete_history_date` 回退全量重读
- `all_dates` 收集失败（个别代码异常）→ 该代码不累加，不影响整体
- sina 超时 → 记为 error，跳过该代码
- 缓存失效 → 重新计算（不影响正确性）

## 测试

### 现有测试必须全过

- `tests/test_refresh.py`
- `tests/test_fetch_unit.py`
- `tests/test_fetcher_resume_parallel.py`
- `tests/test_fetch_checkpoint.py`
- `tests/test_fetcher_sources.py`
- `tests/test_data_pipeline_contracts.py`

### 新增测试

- `test_spot_stale_triggers_refetch`: resume 时 spot 过期 → 重拉 akshare；akshare 失败 → 回退 raw；raw 也没有 → 回退 stale
- `test_progress_writer_debounce`: 高频回调（1000 次/秒）→ ≤2 次/秒写盘；phase 切换立即写
- `test_progress_writer_fallback`: 进度线程异常 → 回退同步写，状态不丢
- `test_detect_complete_reuses_date_counts`: 传入 `date_counts` → 0 文件读（mock `pd.read_csv` 验证未调用）
- `test_detect_complete_fallback_no_date_counts`: 不传入 → 保持全量重读行为
- `test_history_outcome_carries_all_dates`: outcome 包含 `all_dates`，类型为 `tuple[str, ...]`
- `test_sina_call_timeout`: sina 调用超时 → 记为 error，不阻塞整体
- `test_problem_summary_cache`: mtime 不变 → 命中缓存；mtime 变 → 重算；TTL 过期 → 重算
- `test_problem_summary_invalidate_on_refresh`: refresh 完成后 → 缓存失效
- `test_fetch_report_memoize`: 同一 (data_date, mtime) → 命中；mtime 变 → 重读
- `test_progress_payload_speed_eta`: progress 包含 `speed`/`eta_seconds`/`phase_started_at`/`phase_elapsed_seconds`，且 `eta = (total - completed) / speed`

## 兼容性（满足三个硬约束）

- ✅ **断点续传**: checkpoint 读写逻辑完全不变；`all_dates` 仅作为内存累加数据，不写入 checkpoint（resume 重建时从 CSV 一次读出，不影响断点续传语义）
- ✅ **status.json 兼容**: 所有新增字段（`speed`/`eta_seconds`/`phase_started_at`/`phase_elapsed_seconds`）additive，不改/删现有字段
- ✅ **评分逻辑**: `scoring.py` 完全不动；`FetchResult` 新增 `date_counts` 字段不进入评分输入
- ✅ **无新依赖**: 纯 stdlib（`queue`、`threading`、`concurrent.futures`）+ 现有 `requirements.txt`

## 非目标

- 冷启动全量拉取优化（用户未选此场景）
- 异步重构（方案 B，若 A 不足再做）
- `backfill` / `audit` 性能
- 评分逻辑调整
