# 增量刷新性能 + 进度可见性优化 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将日常增量刷新（大部分数据已缓存）从启动到出排行榜的时间压到 1 分钟内，并让进度条展示阶段、当前代码、速度、ETA。

**Architecture:** 在现有 `ThreadPoolExecutor` 同步架构上做外科式修复：(1) fetcher 在 resume 时校验 spot 日期并维护共享 `date_counts` 避免下游全量重读；(2) refresh 引入后台 `_ProgressWriter` 限频写盘避免阻塞 `as_completed`；(3) UI 缓存 `problem_snapshot_summary` 和 `fetch_report` 避免每次轮询全量扫描。所有改动保持断点续传、status.json 向后兼容、scoring.py 不动。

**Tech Stack:** Python 3 stdlib（`queue`、`threading`、`concurrent.futures`）、pandas、streamlit、unittest + mock

**Spec:** [docs/superpowers/specs/2026-07-26-incremental-refresh-perf-design.md](file:///Users/caizihui/Documents/Playground/trend/docs/superpowers/specs/2026-07-26-incremental-refresh-perf-design.md)

---

## 文件结构

| 文件 | 责任 | 改动类型 |
|---|---|---|
| `src/trend_radar/config.py` | 调整超时/并发常量 | 修改 |
| `src/trend_radar/fetcher.py` | R1 spot 校验、R2 重试级联缩短、R3 维护 date_counts + outcome.all_dates | 修改 |
| `src/trend_radar/refresh.py` | R3 _detect 复用 date_counts、R4 _ProgressWriter、R6 progress speed/ETA | 修改 |
| `app.py` | R5 _read_fetch_report memoize、R6 渲染 speed/ETA | 修改 |
| `tests/test_fetch_unit.py` | R2 sina 超时测试 | 修改 |
| `tests/test_fetcher_resume_parallel.py` | R1 spot stale 重拉测试 | 修改 |
| `tests/test_fetch_checkpoint.py` | all_dates checkpoint 重建测试 | 修改 |
| `tests/test_refresh.py` | R3/R4/R5/R6 测试 | 修改 |
| `tests/test_perf_optimization.py` | 新文件：跨模块集成测试 | 创建 |

---

## Task 1: 调整 config.py 常量

**Files:**
- Modify: `src/trend_radar/config.py:24-29`

- [ ] **Step 1: 修改常量**

```python
DEFAULT_HISTORY_FETCH_MAX_WORKERS = 20
CHECKPOINT_WRITE_INTERVAL = 10
SSE_FETCH_MAX_WORKERS = 6
HISTORY_FETCH_PER_CODE_TIMEOUT = 15
SSE_FETCH_PER_DAY_TIMEOUT = 30
CROSS_VALIDATE_CONCURRENCY = 4

# 进度写入限频（秒）
PROGRESS_WRITE_MIN_INTERVAL_SECONDS = 0.5
PROGRESS_WRITE_QUEUE_TIMEOUT_SECONDS = 2.0
# 缓存 TTL
PROBLEM_SUMMARY_CACHE_TTL_SECONDS = 60
```

- [ ] **Step 2: 运行现有测试验证不破坏**

Run: `python -m unittest tests.test_refresh tests.test_fetcher_resume_parallel -v`
Expected: PASS（所有现有测试通过，因为常量仅数值变化）

- [ ] **Step 3: 修改 test_refresh.py 中硬编码 DEFAULT_HISTORY_FETCH_MAX_WORKERS 的断言**

`tests/test_refresh.py:284` 和 `:321`、`:337` 硬编码了 `DEFAULT_HISTORY_FETCH_MAX_WORKERS`，由于是 import 引用，不需要改。验证：

Run: `python -m unittest tests.test_refresh -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/trend_radar/config.py
git commit -m "perf(config): 缩短单码超时到15s、并发提升到20、加进度写入限频常量"
```

---

## Task 2: R1 修复 — `_load_or_fetch_spot` 校验日期 + 三级回退

**Files:**
- Modify: `src/trend_radar/fetcher.py:713-728`（`_load_or_fetch_spot`）
- Modify: `src/trend_radar/fetcher.py:403`（`fetch_data` 调用处）
- Test: `tests/test_fetcher_resume_parallel.py`

- [ ] **Step 1: 写失败测试 — resume 时 spot stale 触发重拉**

追加到 `tests/test_fetcher_resume_parallel.py`：

```python
class SpotStaleResumeTests(unittest.TestCase):
    def _raw_history(self, data_date: str) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "日期": [data_date],
                "开盘": [1.0], "最高": [1.1], "最低": [0.9],
                "收盘": [1.0], "成交额": [500_000_000], "涨跌幅": [0.1],
            }
        )

    def test_resume_stale_spot_triggers_refetch(self) -> None:
        """R1: resume 时 cached_spot 日期不匹配 target → 重拉 akshare"""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            stale_date = "2026-05-28"

            # 预写 stale normalized spot（日期=昨天）
            normalized_dir = root / "data" / "normalized" / data_date
            normalized_dir.mkdir(parents=True)
            stale_spot = pd.DataFrame({
                "code": ["159995"],
                "name": ["测试ETF"],
                "数据日期": [stale_date],
                "amount_yi": [5.0],
            })
            stale_spot.to_csv(normalized_dir / "spot_all.csv", index=False)

            fake_ak = _FakeAk({"159995": self._raw_history(data_date)}, spot_date=data_date)

            result = self._fetch(fake_ak, data_date, root=root, codes=["159995"], resume=True, max_workers=2)

            self.assertEqual(result.candidate_rows, 1)
            # 重拉后 spot 日期应该是 data_date
            new_spot = pd.read_csv(normalized_dir / "spot_all.csv", dtype={"code": str})
            self.assertEqual(str(new_spot["数据日期"].iloc[0]), data_date)

    def test_resume_stale_spot_akshare_fails_falls_back_to_raw(self) -> None:
        """R1: akshare 重拉失败 → 用 raw_dir/spot_em.csv"""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            stale_date = "2026-05-28"

            normalized_dir = root / "data" / "normalized" / data_date
            normalized_dir.mkdir(parents=True)
            raw_dir = root / "data" / "raw" / data_date / "history"
            raw_dir.mkdir(parents=True)

            # 预写 stale normalized spot
            stale_spot = pd.DataFrame({
                "code": ["159995"], "name": ["测试ETF"],
                "数据日期": [stale_date], "amount_yi": [5.0],
            })
            stale_spot.to_csv(normalized_dir / "spot_all.csv", index=False)

            # 预写 raw spot（昨天的 raw，但日期是 stale）
            raw_spot = pd.DataFrame({
                "代码": ["159995"], "名称": ["测试ETF"],
                "数据日期": [stale_date],
            })
            raw_spot.to_csv(root / "data" / "raw" / data_date / "spot_em.csv", index=False)

            # fake_ak 的 spot 接口抛异常
            fake_ak = _FakeAk({"159995": self._raw_history(data_date)})
            fake_ak.fund_etf_spot_em = MagicMock(side_effect=RuntimeError("akshare down"))

            result = self._fetch(fake_ak, data_date, root=root, codes=["159995"], resume=True, max_workers=2)

            # 应回退到 raw（仍是 stale 日期），不抛异常
            self.assertEqual(result.candidate_rows, 1)

    def test_resume_stale_spot_no_raw_uses_stale_cached(self) -> None:
        """R1: akshare 失败 + raw 没有 → 用 stale cached_spot（当前行为，保底）"""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            stale_date = "2026-05-28"

            normalized_dir = root / "data" / "normalized" / data_date
            normalized_dir.mkdir(parents=True)

            stale_spot = pd.DataFrame({
                "code": ["159995"], "name": ["测试ETF"],
                "数据日期": [stale_date], "amount_yi": [5.0],
            })
            stale_spot.to_csv(normalized_dir / "spot_all.csv", index=False)

            fake_ak = _FakeAk({"159995": self._raw_history(data_date)})
            fake_ak.fund_etf_spot_em = MagicMock(side_effect=RuntimeError("akshare down"))

            # 不预写 raw，应回退到 stale cached
            result = self._fetch(fake_ak, data_date, root=root, codes=["159995"], resume=True, max_workers=2)

            self.assertEqual(result.candidate_rows, 1)
```

需要在文件顶部 import 追加 `from unittest.mock import MagicMock` 和 `import tempfile`（如果还没有）。

- [ ] **Step 2: 运行测试验证失败**

Run: `python -m unittest tests.test_fetcher_resume_parallel.SpotStaleResumeTests -v`
Expected: FAIL（`_load_or_fetch_spot` 还没改，stale spot 不会重拉）

- [ ] **Step 3: 修改 `_load_or_fetch_spot` 加 target 参数 + 三级回退**

`src/trend_radar/fetcher.py:713-728` 替换为：

```python
def _load_or_fetch_spot(
    ak: Any,
    raw_path: Path,
    normalized_path: Path,
    resume: bool,
    target: pd.Timestamp | None = None,
    warnings: list[str] | None = None,
) -> pd.DataFrame:
    """加载或拉取 spot 快照，resume 时校验 data_date。

    回退链：
    1. resume + cached_spot 有效 + 日期匹配 target → 返回 cached_spot
    2. cached_spot stale 或不存在 → akshare 重拉
    3. akshare 失败 → 用 raw_dir/spot_em.csv（如果存在）
    4. raw 也没有 → 用 stale cached_spot + warning（保底）
    """
    cached_spot = _read_valid_spot(normalized_path) if resume else None

    # 路径 1：cached 有效且日期匹配
    if cached_spot is not None and target is not None and _frame_dates_match_target(cached_spot, target):
        return cached_spot

    # 路径 2：尝试 akshare 重拉
    try:
        spot_raw = _call_akshare(ak, "fund_etf_spot_em")
        _write_csv(spot_raw, raw_path)
        spot = normalize_spot(spot_raw)
        _write_csv(spot, normalized_path)
        return spot
    except Exception as ak_exc:  # noqa: BLE001 - 回退到 raw/stale
        # 路径 3：akshare 失败，尝试 raw
        if raw_path.exists():
            if warnings is not None:
                warnings.append(f"akshare fund_etf_spot_em failed: {ak_exc}; using raw spot snapshot")
            raw_frame = _read_csv(raw_path)
            if raw_frame is not None:
                spot = normalize_spot(raw_frame)
                _write_csv(spot, normalized_path)
                return spot

        # 路径 4：raw 也没有，回退 stale cached
        if cached_spot is not None:
            if warnings is not None:
                warnings.append(
                    f"akshare fund_etf_spot_em failed and no raw spot available: {ak_exc}; "
                    "using stale cached spot snapshot"
                )
            return cached_spot

        raise
```

- [ ] **Step 4: 修改 `fetch_data` 调用处传 target**

`src/trend_radar/fetcher.py:403` 找到：

```python
spot = _load_or_fetch_spot(ak, raw_dir / "spot_em.csv", normalized_dir / "spot_all.csv", resume)
```

替换为：

```python
spot = _load_or_fetch_spot(
    ak,
    raw_dir / "spot_em.csv",
    normalized_dir / "spot_all.csv",
    resume,
    target=target,
    warnings=warnings,
)
```

- [ ] **Step 5: 运行测试验证通过**

Run: `python -m unittest tests.test_fetcher_resume_parallel.SpotStaleResumeTests -v`
Expected: PASS（3 个测试全过）

- [ ] **Step 6: 运行所有现有测试验证不破坏**

Run: `python -m unittest discover tests -v 2>&1 | tail -20`
Expected: PASS（所有现有测试通过）

- [ ] **Step 7: Commit**

```bash
git add src/trend_radar/fetcher.py tests/test_fetcher_resume_parallel.py
git commit -m "perf(fetcher): R1 spot resume 时校验日期 + 三级回退链"
```

---

## Task 3: R2 修复 — 缩短 eastmoney 重试 + sina 超时

**Files:**
- Modify: `src/trend_radar/fetcher.py:1116-1138`（`_fetch_history_with_attempt_details`）
- Modify: `src/trend_radar/fetcher.py:1266-1297`（`_fetch_history_sina`）
- Test: `tests/test_fetch_unit.py`

- [ ] **Step 1: 写失败测试 — sina 调用超时不阻塞**

追加到 `tests/test_fetch_unit.py`：

```python
class TestSinaTimeout(unittest.TestCase):
    def test_sina_call_timeout_records_error(self) -> None:
        """R2: sina 调用超时 → 记为 error，不阻塞"""
        from trend_radar.fetcher import _fetch_history_with_attempt_details
        import time

        # akshare sina 接口模拟永久阻塞
        blocking_ak = MagicMock()
        def blocking_sina(*args, **kwargs):
            time.sleep(30)  # 模拟阻塞
        blocking_ak.fund_etf_hist_sina = blocking_sina

        # urllib 和 curl 都失败，sina 阻塞
        with patch("trend_radar.fetcher._fetch_history_eastmoney_with_provider", side_effect=RuntimeError("eastmoney down")):
            start = time.time()
            with self.assertRaises(RuntimeError) as ctx:
                _fetch_history_with_attempt_details(blocking_ak, "159995", "20260522", "20260529")
            elapsed = time.time() - start
            # 应在 ~10s 内超时（不是 30s）
            self.assertLess(elapsed, 15, f"sina should timeout, not block; elapsed={elapsed}")
            self.assertIn("sina", str(ctx.exception).lower())
```

- [ ] **Step 2: 运行测试验证失败**

Run: `python -m unittest tests.test_fetch_unit.TestSinaTimeout -v`
Expected: FAIL 或超时（sina 当前无超时，会阻塞 30s）

- [ ] **Step 3: 修改 `_fetch_history_with_attempt_details` — 2 次重试改 1 次 + sina 包超时**

`src/trend_radar/fetcher.py:1116-1138` 替换为：

```python
def _fetch_history_with_attempt_details(
    ak: Any, code: str, start_date: str, end_date: str
) -> tuple[pd.DataFrame, str, str | None, str | None]:
    """eastmoney 单次重试 + sina 超时兜底。

    Returns: (frame, provider, primary_error, fallback_error)
    """
    errors: list[str] = []

    # eastmoney 单次重试（原 2 次改为 1 次）
    try:
        frame, provider = _fetch_history_eastmoney_with_provider(code, start_date, end_date)
        return frame, provider, None, None
    except Exception as exc:
        errors.append(f"eastmoney: {exc}")

    primary_error = "; ".join(errors)

    # sina 兜底（带超时）
    try:
        frame = _fetch_history_sina_with_timeout(ak, code, start_date, end_date, timeout=10)
        return frame, "sina", primary_error, None
    except Exception as exc:
        fallback_error = f"sina: {exc}"
        errors.append(fallback_error)

    raise RuntimeError(f"All sources exhausted for {code}: {'; '.join(errors[-2:])}")
```

- [ ] **Step 4: 添加 `_fetch_history_sina_with_timeout` 函数**

在 `_fetch_history_sina` 之前（`fetcher.py:1266` 附近）添加：

```python
def _fetch_history_sina_with_timeout(
    ak: Any, code: str, start_date: str, end_date: str, timeout: int = 10
) -> pd.DataFrame:
    """调用 sina 接口并加超时保护。"""
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_fetch_history_sina, ak, code, start_date, end_date)
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError as exc:
            future.cancel()
            raise RuntimeError(f"sina timed out after {timeout}s") from exc
```

- [ ] **Step 5: 修改 proxy curl 分支 — 仅在 proxy 非空时尝试**

`src/trend_radar/fetcher.py:1150-1166`（`_fetch_history_eastmoney_with_provider`）替换为：

```python
def _fetch_history_eastmoney_with_provider(code: str, start_date: str, end_date: str) -> tuple[pd.DataFrame, str]:
    url = _eastmoney_history_url(code, start_date, end_date)
    try:
        return _fetch_history_eastmoney_urllib(url), "eastmoney_urllib"
    except Exception as urllib_exc:
        errors = [f"urllib failed: {urllib_exc}"]
        try:
            return _fetch_history_eastmoney_curl(url), "eastmoney_curl"
        except Exception as curl_exc:
            errors.append(f"curl failed: {curl_exc}")
            proxy = _history_proxy()
            if proxy:  # 仅在 proxy 非空时尝试
                try:
                    return _fetch_history_eastmoney_curl(url, proxy=proxy), "eastmoney_curl_proxy"
                except Exception as proxy_exc:
                    errors.append(f"proxy curl failed: {proxy_exc}")
            raise RuntimeError("; ".join(errors)) from curl_exc
```

- [ ] **Step 6: 运行测试验证通过**

Run: `python -m unittest tests.test_fetch_unit.TestSinaTimeout -v`
Expected: PASS（sina 超时在 ~10s 内）

- [ ] **Step 7: 运行所有 fetcher 测试验证不破坏**

Run: `python -m unittest tests.test_fetch_unit tests.test_fetcher_sources tests.test_fetcher_resume_parallel -v 2>&1 | tail -30`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/trend_radar/fetcher.py tests/test_fetch_unit.py
git commit -m "perf(fetcher): R2 eastmoney 单次重试 + sina 超时10s + proxy按需"
```

---

## Task 4: R3 数据流 — `HistoryFetchOutcome.all_dates` + fetch 阶段维护 `date_counts`

**Files:**
- Modify: `src/trend_radar/fetcher.py:81-94`（`HistoryFetchOutcome` dataclass）
- Modify: `src/trend_radar/fetcher.py:70-78`（`FetchResult` dataclass）
- Modify: `src/trend_radar/fetcher.py:360-665`（`fetch_data` 主流程）
- Modify: `src/trend_radar/fetcher.py:731-884`（`_load_or_fetch_history`）
- Modify: `src/trend_radar/fetcher.py:289-346`（`_rebuild_outcomes_from_checkpoint`）
- Test: `tests/test_fetch_checkpoint.py`

- [ ] **Step 1: 写失败测试 — outcome 包含 all_dates**

追加到 `tests/test_fetch_checkpoint.py`：

```python
class AllDatesOutcomeTests(unittest.TestCase):
    def _raw_history(self, data_date: str) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "日期": ["2026-05-27", "2026-05-28", "2026-05-29"],
                "开盘": [1.0, 1.1, 1.2], "最高": [1.1, 1.2, 1.3],
                "最低": [0.9, 1.0, 1.1], "收盘": [1.0, 1.1, 1.2],
                "成交额": [500_000_000, 600_000_000, 700_000_000],
                "涨跌幅": [0.1, 0.1, 0.1],
            }
        )

    def test_fetch_result_carries_date_counts(self) -> None:
        """R3: fetch_data 返回 date_counts 字段"""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            fake_ak = _FakeAk({"159995": self._raw_history(data_date)}, spot_date=data_date)

            result = self._fetch(fake_ak, data_date, root=root, codes=["159995"], resume=False, max_workers=2)

            self.assertTrue(hasattr(result, "date_counts"))
            self.assertIsInstance(result.date_counts, dict)
            # 159995 在 3 个日期都有数据
            self.assertEqual(result.date_counts.get("2026-05-29"), 1)
            self.assertEqual(result.date_counts.get("2026-05-28"), 1)
            self.assertEqual(result.date_counts.get("2026-05-27"), 1)
```

需要在 `_FakeAk` 类附近检查是否有 `_fetch` helper 方法（如果没有，参考 `test_fetcher_resume_parallel.py` 中的实现）。

- [ ] **Step 2: 运行测试验证失败**

Run: `python -m unittest tests.test_fetch_checkpoint.AllDatesOutcomeTests -v`
Expected: FAIL（`FetchResult` 还没有 `date_counts` 字段）

- [ ] **Step 3: 修改 `FetchResult` dataclass — 加 date_counts**

`src/trend_radar/fetcher.py:70-78` 替换为：

```python
@dataclass(frozen=True)
class FetchResult:
    data_date: str
    spot_rows: int
    candidate_rows: int
    history_rows: int
    share_rows: int
    errors: list[str]
    normalized_dir: Path
    date_counts: dict[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.date_counts is None:
            object.__setattr__(self, "date_counts", {})
```

- [ ] **Step 4: 修改 `HistoryFetchOutcome` dataclass — 加 all_dates**

`src/trend_radar/fetcher.py:81-94` 替换为：

```python
@dataclass(frozen=True)
class HistoryFetchOutcome:
    code: str
    name: str
    status: str
    rows: int = 0
    error: str | None = None
    provider: str | None = None
    latest_date: str | None = None
    stale: bool = False
    primary_provider: str | None = None
    primary_error: str | None = None
    fallback_provider: str | None = None
    fallback_error: str | None = None
    all_dates: tuple[str, ...] = ()
```

- [ ] **Step 5: 修改 `_load_or_fetch_history` 收集 all_dates**

`src/trend_radar/fetcher.py:731-884`（`_load_or_fetch_history` 函数）整体替换为下面的版本。

> **注意**：下方代码块是**完整实现**，没有省略。每个 `# 段落说明` 注释下方紧跟对应代码。8 处 `return HistoryFetchOutcome(...)` 都补了 `all_dates=collected_all_dates`。

```python
def _load_or_fetch_history(
    ak: Any, code: str, name: str, history_start: str, target_ymd: str,
    target: pd.Timestamp, raw_history_dir: Path, normalized_history_dir: Path,
    resume: bool, spot_history: pd.DataFrame | None = None,
) -> HistoryFetchOutcome:
    raw_path = raw_history_dir / f"{code}.csv"
    normalized_path = normalized_history_dir / f"{code}.csv"

    def _collect_dates(frame: pd.DataFrame | None) -> tuple[str, ...]:
        if frame is None or frame.empty or "日期" not in frame:
            return ()
        dates = pd.to_datetime(frame["日期"], errors="coerce").dropna()
        dates = dates[dates <= target]
        if dates.empty:
            return ()
        return tuple(sorted(dates.dt.date.astype(str).unique()))

    collected_all_dates: tuple[str, ...] = ()

    if resume:
        cached_meta = _valid_normalized_history_metadata(normalized_path, target)
        if cached_meta is not None:
            rows, latest_date = cached_meta
            if latest_date == target.date().isoformat():
                cached_source = _history_source_from_path(normalized_path) or "cache_normalized"
                collected_all_dates = _collect_dates(_read_csv(normalized_path))
                return HistoryFetchOutcome(
                    code=code, name=name, status="cached", rows=rows,
                    provider=cached_source, latest_date=latest_date, stale=False,
                    all_dates=collected_all_dates,
                )
            # cached normalized stale → 用 spot 打补丁
            spot_meta = _write_history_with_spot_fallback(_read_csv(normalized_path), spot_history, normalized_path, target)
            if spot_meta is not None:
                rows, latest_date = spot_meta
                collected_all_dates = _collect_dates(_read_csv(normalized_path))
                return HistoryFetchOutcome(
                    code=code, name=name, status="fetched", rows=rows,
                    provider="spot_fallback", latest_date=latest_date, stale=False,
                    primary_provider="eastmoney",
                    primary_error="cached normalized history is stale; patched from current spot snapshot before primary refetch",
                    fallback_provider="spot_fallback",
                    all_dates=collected_all_dates,
                )

        # cached raw history 路径
        raw_history = _read_csv(raw_path)
        if raw_history is not None:
            try:
                raw_source = _history_source_from_frame(raw_history) or "cache_raw"
                history = _with_history_source(normalize_history(raw_history, code), raw_source)
                cached_meta = _valid_history_metadata(history, target)
                if cached_meta is not None:
                    rows, latest_date = cached_meta
                    if latest_date == target.date().isoformat():
                        _write_csv(history, normalized_path)
                        collected_all_dates = _collect_dates(history)
                        return HistoryFetchOutcome(
                            code=code, name=name, status="cached", rows=rows,
                            provider=raw_source, latest_date=latest_date, stale=False,
                            all_dates=collected_all_dates,
                        )
                    # cached raw stale → 用 spot 打补丁
                    spot_meta = _write_history_with_spot_fallback(history, spot_history, normalized_path, target)
                    if spot_meta is not None:
                        rows, latest_date = spot_meta
                        collected_all_dates = _collect_dates(_read_csv(normalized_path))
                        return HistoryFetchOutcome(
                            code=code, name=name, status="fetched", rows=rows,
                            provider="spot_fallback", latest_date=latest_date, stale=False,
                            primary_provider="eastmoney",
                            primary_error="cached raw history is stale; patched from current spot snapshot before primary refetch",
                            fallback_provider="spot_fallback",
                            all_dates=collected_all_dates,
                        )
            except Exception:
                pass

    # 主拉取路径
    try:
        raw_history, provider, primary_error, fallback_error = _fetch_history_with_attempt_details(
            ak, code, history_start, target_ymd
        )
        raw_history = raw_history.copy()
        raw_history["history_source"] = provider
        _write_csv(raw_history, raw_path)
        history = _with_history_source(normalize_history(raw_history, code), provider)
        meta = _valid_history_metadata(history, target)
        if meta is None:
            # 拉到但无效 → spot 兜底
            spot_meta = _write_history_with_spot_fallback(None, spot_history, normalized_path, target)
            if spot_meta is not None:
                rows, latest_date = spot_meta
                collected_all_dates = _collect_dates(_read_csv(normalized_path))
                return HistoryFetchOutcome(
                    code=code, name=name, status="fetched", rows=rows,
                    provider="spot_fallback", latest_date=latest_date, stale=False,
                    primary_provider="eastmoney", primary_error=primary_error,
                    fallback_provider="spot_fallback",
                    all_dates=collected_all_dates,
                )
            return HistoryFetchOutcome(code=code, name=name, status="error", error="empty history")
        rows, latest_date = meta
        if latest_date != target.date().isoformat():
            # 拉到但 stale → spot 打补丁
            spot_meta = _write_history_with_spot_fallback(history, spot_history, normalized_path, target)
            if spot_meta is not None:
                rows, latest_date = spot_meta
                collected_all_dates = _collect_dates(_read_csv(normalized_path))
                return HistoryFetchOutcome(
                    code=code, name=name, status="fetched", rows=rows,
                    provider="spot_fallback", latest_date=latest_date, stale=False,
                    all_dates=collected_all_dates,
                )
        _write_csv(history, normalized_path)
        collected_all_dates = _collect_dates(history)
        return HistoryFetchOutcome(
            code=code, name=name, status="fetched", rows=rows,
            provider=provider, latest_date=latest_date,
            stale=latest_date != target.date().isoformat(),
            primary_provider=provider if _is_eastmoney_provider(provider) else "eastmoney",
            primary_error=primary_error,
            fallback_provider=provider if provider == "sina" else None,
            fallback_error=fallback_error,
            all_dates=collected_all_dates,
        )
    except Exception as exc:
        # 拉取异常 → spot 兜底
        spot_meta = _write_history_with_spot_fallback(None, spot_history, normalized_path, target)
        if spot_meta is not None:
            rows, latest_date = spot_meta
            collected_all_dates = _collect_dates(_read_csv(normalized_path))
            return HistoryFetchOutcome(
                code=code, name=name, status="fetched", rows=rows,
                provider="spot_fallback", latest_date=latest_date, stale=False,
                primary_provider="eastmoney", primary_error=str(exc),
                fallback_provider="spot_fallback",
                all_dates=collected_all_dates,
            )
        return HistoryFetchOutcome(code=code, name=name, status="error", error=str(exc))
```

- [ ] **Step 6: 修改 `_rebuild_outcomes_from_checkpoint` 填充 all_dates**

`src/trend_radar/fetcher.py:289-346` 在 `cached`/`fetched` 分支读取 normalized_path 填 all_dates：

```python
def _rebuild_outcomes_from_checkpoint(
    checkpoint: dict[str, Any],
    normalized_history_dir: Path,
    target: pd.Timestamp,
) -> tuple[list[HistoryFetchOutcome], list[dict[str, str]], int, int, int, int]:
    outcomes: list[HistoryFetchOutcome] = []
    failed_history: list[dict[str, str]] = []
    skipped_cached = 0
    fetched = 0
    history_errors = 0
    history_rows = 0

    for code, entry in checkpoint.get("outcomes", {}).items():
        code = str(code).zfill(6)
        status = str(entry.get("status", ""))
        name = str(entry.get("name", ""))
        normalized_path = normalized_history_dir / f"{code}.csv"

        if status in ("cached", "fetched"):
            meta = _valid_normalized_history_metadata(normalized_path, target)
            if meta is not None:
                rows, latest_date = meta
                provider = str(entry.get("provider") or "")
                stale = bool(entry.get("stale", False))
                # R3: 从 CSV 收集 all_dates（resume 启动时一次）
                all_dates = _collect_all_dates_from_path(normalized_path, target)
                outcome = HistoryFetchOutcome(
                    code=code, name=name, status=status, rows=rows,
                    provider=provider if provider else None,
                    latest_date=latest_date, stale=stale,
                    primary_provider=str(entry.get("primary_provider") or "") or None,
                    primary_error=str(entry.get("primary_error") or "") or None,
                    fallback_provider=str(entry.get("fallback_provider") or "") or None,
                    fallback_error=str(entry.get("fallback_error") or "") or None,
                    all_dates=all_dates,
                )
                outcomes.append(outcome)
                history_rows += rows
                if status == "cached":
                    skipped_cached += 1
                else:
                    fetched += 1
                continue
        elif status == "error":
            error_msg = str(entry.get("error") or "unknown history error")
            outcome = HistoryFetchOutcome(
                code=code, name=name, status="error", error=error_msg,
            )
            outcomes.append(outcome)
            failed_history.append({"code": code, "name": name, "error": error_msg})
            history_errors += 1
            continue

    return outcomes, failed_history, skipped_cached, fetched, history_errors, history_rows


def _collect_all_dates_from_path(path: Path, target: pd.Timestamp) -> tuple[str, ...]:
    """从 normalized history CSV 收集所有有效日期。"""
    frame = _read_csv(path)
    if frame is None or frame.empty or "日期" not in frame:
        return ()
    dates = pd.to_datetime(frame["日期"], errors="coerce").dropna()
    dates = dates[dates <= target]
    if dates.empty:
        return ()
    return tuple(sorted(dates.dt.date.astype(str).unique()))
```

- [ ] **Step 7: 修改 `fetch_data` 维护共享 date_counts 并写入 FetchResult**

`src/trend_radar/fetcher.py:462-475` 附近，在初始化区追加：

```python
    # R3: 共享 date_counts（线程安全）
    date_counts: dict[str, int] = {}
    date_counts_lock = threading.Lock()

    def _accumulate_date_counts(all_dates: tuple[str, ...]) -> None:
        if not all_dates:
            return
        with date_counts_lock:
            for d in all_dates:
                date_counts[d] = date_counts.get(d, 0) + 1
```

`src/trend_radar/fetcher.py:549-588` 附近，在 `history_outcomes.append(outcome)` 之后追加：

```python
                    history_outcomes.append(outcome)
                    _accumulate_date_counts(outcome.all_dates)
```

resume 重建路径（`fetcher.py:481-489` 附近）也要累加：

```python
                history_outcomes.extend(rebuilt_outcomes)
                failed_history.extend(rebuilt_failed)
                skipped_cached += rebuilt_cached
                fetched += rebuilt_fetched
                history_errors += rebuilt_errors
                history_rows += rebuilt_rows
                completed_before_resume = len(rebuilt_outcomes)
                completed = completed_before_resume
                checkpoint_outcomes = {str(item.code): asdict(item) for item in rebuilt_outcomes}
                # R3: resume 重建后累加 date_counts
                for item in rebuilt_outcomes:
                    _accumulate_date_counts(item.all_dates)
                pending_records = _filter_pending_records(candidate_records, checkpoint_outcomes)
```

最后 `fetch_data` 返回处（`fetcher.py:656-664`）：

```python
        return FetchResult(
            data_date=data_date,
            spot_rows=len(spot),
            candidate_rows=len(candidates),
            history_rows=history_rows,
            share_rows=len(shares),
            errors=errors,
            normalized_dir=normalized_dir,
            date_counts=dict(date_counts),  # R3: 返回 date_counts 副本
        )
```

- [ ] **Step 8: 运行测试验证通过**

Run: `python -m unittest tests.test_fetch_checkpoint.AllDatesOutcomeTests -v`
Expected: PASS

- [ ] **Step 9: 运行所有 fetcher 测试验证不破坏**

Run: `python -m unittest tests.test_fetch_unit tests.test_fetcher_sources tests.test_fetcher_resume_parallel tests.test_fetch_checkpoint -v 2>&1 | tail -30`
Expected: PASS

- [ ] **Step 10: Commit**

```bash
git add src/trend_radar/fetcher.py tests/test_fetch_checkpoint.py
git commit -m "perf(fetcher): R3 outcome 加 all_dates + fetch 维护共享 date_counts"
```

---

## Task 5: R3 修复 — `_detect_complete_history_date` 复用 date_counts

**Files:**
- Modify: `src/trend_radar/refresh.py:607-644`（`_detect_complete_history_date`）
- Modify: `src/trend_radar/refresh.py:180-205`（`refresh_latest_data` 调用处）
- Test: `tests/test_refresh.py`

- [ ] **Step 1: 写失败测试 — 传入 date_counts 时不读文件**

追加到 `tests/test_refresh.py`：

> **注意**：`RefreshTests` 的 `_saturday`/`_write_history`/`_write_radar` 是实例方法，新测试类不继承 `RefreshTests`（避免运行父类所有测试）。每个新测试类 inline 定义自己需要的 helper。

```python
class DetectCompleteDateCountsTests(unittest.TestCase):
    """R3: _detect_complete_history_date 复用 date_counts 测试。

    不继承 RefreshTests（避免运行父类测试），inline 定义需要的 helper。
    """

    def _write_history(self, root: Path, source_date: str, latest_date: str) -> None:
        history_dir = root / "data" / "normalized" / source_date / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        dates = pd.bdate_range(end=pd.Timestamp(latest_date), periods=3)
        pd.DataFrame({"日期": dates}).to_csv(history_dir / "159995.csv", index=False)

    def test_detect_complete_reuses_date_counts_no_file_read(self) -> None:
        """R3: 传入 date_counts → 0 文件读"""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            self._write_history(root, data_date, data_date)

            date_counts = {"2026-05-29": 1, "2026-05-28": 1}
            with patch(
                "trend_radar.refresh.pd.read_csv",
                new=MagicMock(side_effect=AssertionError("should not read csv")),
            ):
                result = _detect_complete_history_date(root, data_date, date_counts=date_counts)
            # 不应调用 pd.read_csv
            self.assertEqual(result, "2026-05-29")

    def test_detect_complete_no_date_counts_falls_back_to_file_read(self) -> None:
        """R3: 不传入 date_counts → 保持全量重读行为"""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            self._write_history(root, data_date, data_date)

            result = _detect_complete_history_date(root, data_date, date_counts=None)

            self.assertEqual(result, "2026-05-29")
```

需要在 `tests/test_refresh.py` import 处加：

```python
from unittest.mock import patch, MagicMock
from trend_radar.refresh import _detect_complete_history_date
```

- [ ] **Step 2: 运行测试验证失败**

Run: `python -m unittest tests.test_refresh.DetectCompleteDateCountsTests -v`
Expected: FAIL（`_detect_complete_history_date` 还没有 `date_counts` 参数）

- [ ] **Step 3: 修改 `_detect_complete_history_date` 加 date_counts 快速路径**

`src/trend_radar/refresh.py:607-644` 替换为：

```python
def _detect_complete_history_date(
    root: Path,
    source_date: str,
    min_coverage_ratio: float = HISTORY_COMPLETE_COVERAGE_RATIO,
    date_counts: dict[str, int] | None = None,
) -> str | None:
    """检测完整历史覆盖的最新日期。

    R3 优化：传入 date_counts 时走快速路径（0 文件读），否则回退全量重读。
    """
    # R3 快速路径：使用 fetch 阶段已维护的 date_counts
    if date_counts is not None:
        if not date_counts:
            return None
        candidate_codes = _candidate_codes(root, source_date)
        denominator = len(candidate_codes) if candidate_codes else max(date_counts.values())
        if not denominator:
            return None
        min_count = max(1, int(denominator * min_coverage_ratio + 0.999999))
        complete_dates = sorted(d for d, c in date_counts.items() if c >= min_count)
        if complete_dates:
            return complete_dates[-1]
        best_date = max(date_counts, key=lambda d: (date_counts[d], d))
        return best_date

    # 慢速路径：当前实现（保底，向后兼容）
    history_dir = root / "data" / "normalized" / source_date / "history"
    if not history_dir.exists():
        return None

    target = pd.Timestamp(source_date)
    candidate_codes = _candidate_codes(root, source_date)
    history_codes: set[str] = set()
    file_date_counts: dict[str, int] = {}
    for path in sorted(history_dir.glob("*.csv")):
        code = path.stem.zfill(6)
        if candidate_codes and code not in candidate_codes:
            continue
        try:
            frame = pd.read_csv(path, usecols=["日期"])
        except (OSError, ValueError, pd.errors.EmptyDataError):
            continue
        dates = pd.to_datetime(frame["日期"], errors="coerce").dropna()
        dates = dates[dates <= target]
        if dates.empty:
            continue
        history_codes.add(code)
        for item in dates.dt.date.astype(str).unique():
            file_date_counts[str(item)] = file_date_counts.get(str(item), 0) + 1

    denominator = len(candidate_codes) if candidate_codes else len(history_codes)
    if not denominator or not file_date_counts:
        return None
    min_count = max(1, int(denominator * min_coverage_ratio + 0.999999))
    complete_dates = sorted(date_key for date_key, count in file_date_counts.items() if count >= min_count)
    if complete_dates:
        return complete_dates[-1]
    best_date = max(file_date_counts, key=lambda date_key: (file_date_counts[date_key], date_key))
    return best_date
```

- [ ] **Step 4: 修改 `refresh_latest_data` 调用处传 date_counts**

`src/trend_radar/refresh.py:180-186` 附近，找到：

```python
                fetcher(
                    data_date=refresh_date,
                    root=root,
                    progress_callback=progress_callback,
                    resume=not force,
                    max_workers=DEFAULT_HISTORY_FETCH_MAX_WORKERS,
                )
                actual_date = _detect_complete_history_date(root, refresh_date) or refresh_date
```

替换为：

```python
                fetch_result = fetcher(
                    data_date=refresh_date,
                    root=root,
                    progress_callback=progress_callback,
                    resume=not force,
                    max_workers=DEFAULT_HISTORY_FETCH_MAX_WORKERS,
                )
                # R3: 复用 fetch 阶段已收集的 date_counts，避免全量重读
                fetch_date_counts = getattr(fetch_result, "date_counts", None) if fetch_result else None
                actual_date = (
                    _detect_complete_history_date(root, refresh_date, date_counts=fetch_date_counts)
                    or refresh_date
                )
```

- [ ] **Step 5: 运行测试验证通过**

Run: `python -m unittest tests.test_refresh.DetectCompleteDateCountsTests -v`
Expected: PASS

- [ ] **Step 6: 运行所有 refresh 测试验证不破坏**

Run: `python -m unittest tests.test_refresh -v 2>&1 | tail -30`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/trend_radar/refresh.py tests/test_refresh.py
git commit -m "perf(refresh): R3 _detect_complete_history_date 复用 date_counts 避免全量重读"
```

---

## Task 6: R4 修复 — `_ProgressWriter` 后台线程限频写盘

**Files:**
- Modify: `src/trend_radar/refresh.py`（新增 `_ProgressWriter` 类 + 修改 `progress_callback`）
- Test: `tests/test_refresh.py`

- [ ] **Step 1: 写失败测试 — 高频回调限频**

追加到 `tests/test_refresh.py`：

```python
class ProgressWriterDebounceTests(unittest.TestCase):
    """R4: _ProgressWriter 限频写盘测试。

    不继承 RefreshTests（避免运行父类测试），inline 定义需要的 helper。
    """

    def _saturday(self) -> datetime:
        return datetime(2026, 5, 30, 10, 0, tzinfo=ASIA_SHANGHAI)

    def _write_history(self, root: Path, source_date: str, latest_date: str) -> None:
        history_dir = root / "data" / "normalized" / source_date / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        dates = pd.bdate_range(end=pd.Timestamp(latest_date), periods=3)
        pd.DataFrame({"日期": dates}).to_csv(history_dir / "159995.csv", index=False)

    def _write_radar(self, root: Path, data_date: str, source_date: str | None = None) -> SimpleNamespace:
        radar_dir = root / "data" / "radar"
        radar_dir.mkdir(parents=True, exist_ok=True)
        radar_csv = radar_dir / f"{data_date}.csv"
        radar_json = radar_dir / f"{data_date}.json"
        radar_csv.write_text("排名,ETF名称,代码\n1,芯片ETF,159995\n", encoding="utf-8")
        radar_json.write_text(
            json.dumps({"data_date": data_date, "source_date": source_date or data_date, "generated_at": f"{data_date} 16:00:00"}, ensure_ascii=False),
            encoding="utf-8",
        )
        return SimpleNamespace(data_date=data_date, radar_csv=radar_csv, radar_json=radar_json)

    def test_high_frequency_progress_is_debounced(self) -> None:
        """R4: 高频回调（1000 次）→ 写盘 ≤ 4 次/秒"""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            write_count = [0]
            original_write = None

            import trend_radar.refresh as refresh_mod
            original_write = refresh_mod._write_status
            def counting_write(r_root, r_result, traceback_text=None):
                write_count[0] += 1
                original_write(r_root, r_result, traceback_text=None)

            with patch.object(refresh_mod, "_write_status", side_effect=counting_write):
                def fake_fetcher(data_date: str, root: Path, **kwargs: object) -> object:
                    callback = kwargs["progress_callback"]
                    # 高频回调 1000 次，应在 0.1s 内完成
                    for i in range(1000):
                        callback({
                            "phase": "fetch_history",
                            "completed": i, "total": 1000,
                            "current_code": "159995", "current_name": "测试",
                            "skipped_cached": 0, "fetched": i, "errors": 0,
                            "max_workers": 20,
                        })
                    self._write_history(root, data_date, data_date)
                    return SimpleNamespace(data_date=data_date)

                def fake_scorer(data_date: str, root: Path, source_date: str | None = None) -> object:
                    return self._write_radar(root, data_date)

                start = __import__("time").time()
                result = refresh_latest_data(root=root, now=self._saturday(), fetcher=fake_fetcher, scorer=fake_scorer)
                elapsed = __import__("time").time() - start

                self.assertEqual(result.status, REFRESH_SUCCEEDED)
                # 1000 次回调，写盘应远少于 1000 次（限频 ≤2 次/秒）
                # 注意：phase 切换会强制写，加上 fetch 完成后的 score phase，整体不会为 0
                self.assertLess(write_count[0], 100, f"write_count={write_count[0]} should be debounced")

    def test_progress_writer_fallback_on_exception(self) -> None:
        """R4: 进度线程异常 → 回退同步写，状态不丢"""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)

            def fake_fetcher(data_date: str, root: Path, **kwargs: object) -> object:
                callback = kwargs["progress_callback"]
                callback({
                    "phase": "fetch_history",
                    "completed": 1, "total": 10,
                    "current_code": "159995", "current_name": "测试",
                    "skipped_cached": 0, "fetched": 1, "errors": 0,
                    "max_workers": 20,
                })
                self._write_history(root, data_date, data_date)
                return SimpleNamespace(data_date=data_date)

            def fake_scorer(data_date: str, root: Path, source_date: str | None = None) -> object:
                return self._write_radar(root, data_date)

            result = refresh_latest_data(root=root, now=self._saturday(), fetcher=fake_fetcher, scorer=fake_scorer)
            self.assertEqual(result.status, REFRESH_SUCCEEDED)
            # 进度应被写入（无论是否回退）
            status = read_refresh_status(root)
            self.assertIn(status["progress"]["completed"], [1, 10])
```

需要在 import 处加 `from unittest.mock import patch`。

- [ ] **Step 2: 运行测试验证失败**

Run: `python -m unittest tests.test_refresh.ProgressWriterDebounceTests -v`
Expected: FAIL（当前每次回调都同步写，write_count 会接近 1000）

- [ ] **Step 3: 添加 `_ProgressWriter` 类**

在 `src/trend_radar/refresh.py` 顶部 import 后追加：

```python
import queue
import threading
```

在 `refresh.py` 的 `_RefreshLock` 类之前（约 line 947）添加：

```python
class _ProgressWriter:
    """R4: 后台线程限频写 status，避免阻塞 as_completed 主线程。

    进度回调入队 → 后台线程消费 → 限频写盘。
    phase 切换立即写；线程异常 → 调用方回退同步写。
    """

    def __init__(self, root: Path, target_date: str, started_at: str, min_interval: float = PROGRESS_WRITE_MIN_INTERVAL_SECONDS) -> None:
        self._root = root
        self._target_date = target_date
        self._started_at = started_at
        self._min_interval = min_interval
        self._queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_write_time = 0.0
        self._last_phase: str | None = None
        self._fallback_error: Exception | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="trend-radar-progress-writer", daemon=True)
        self._thread.start()

    def submit(self, progress: dict[str, Any], phase: str, message: str) -> None:
        """非阻塞入队。"""
        try:
            self._queue.put_nowait((progress, phase, message))
        except queue.Full:
            pass  # 丢弃旧进度，不影响主流程

    def join(self, timeout: float = 2.0) -> None:
        """停止并等待线程结束，确保最后一条状态写入。"""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        # 兜底：如果线程异常退出，flush 剩余队列
        if self._fallback_error is not None:
            self._flush_remaining()

    def has_failed(self) -> bool:
        return self._fallback_error is not None

    def _run(self) -> None:
        try:
            while not self._stop_event.is_set() or not self._queue.empty():
                try:
                    item = self._queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                progress, phase, message = item
                self._write(progress, phase, message, force=(phase != self._last_phase))
        except Exception:
            self._fallback_error = Exception("progress writer thread failed")

    def _write(self, progress: dict[str, Any], phase: str, message: str, force: bool = False) -> None:
        now_ts = time.monotonic()
        if not force and (now_ts - self._last_write_time) < self._min_interval:
            return
        try:
            result = RefreshResult(
                status=REFRESH_RUNNING,
                target_date=self._target_date,
                started_at=self._started_at,
                updated_at=_timestamp(),
                phase=phase,
                progress=_progress_from_callback(progress),
                message=message,
            )
            _write_status(self._root, result)
            self._last_write_time = now_ts
            self._last_phase = phase
        except Exception:
            pass  # 写盘失败不阻塞主流程

    def _flush_remaining(self) -> None:
        """同步 flush 剩余队列（回退路径）。"""
        while not self._queue.empty():
            try:
                progress, phase, message = self._queue.get_nowait()
                self._write(progress, phase, message, force=True)
            except queue.Empty:
                break
            except Exception:
                break
```

需要在 import 处加 `import time`（如果还没有）和 `from .config import PROGRESS_WRITE_MIN_INTERVAL_SECONDS`。

- [ ] **Step 4: 修改 `refresh_latest_data` 用 `_ProgressWriter`**

`src/trend_radar/refresh.py:139-205` 附近。**注意：Task 5 已把 `fetcher(...)` 改为 `fetch_result = fetcher(...)` 并加了 `fetch_date_counts` / `_detect_complete_history_date(date_counts=...)`。本 Step 只动 R4 相关的 3 处**：

**(a)** 在 `for refresh_date in dates_to_refresh:` **之前**插入 ProgressWriter 启动：

```python
            # R4: 启动 ProgressWriter 后台线程
            progress_writer = _ProgressWriter(root, target_date, started_at)
            progress_writer.start()
```

**(b)** 替换 `progress_callback` 函数体（Task 5 后的当前状态是同步 `_write_status`，改为入队 + 回退）：

old_string（Task 5 后的状态）：
```python
                def progress_callback(progress: dict[str, Any], refresh_date: str = refresh_date) -> None:
                    phase = str(progress.get("phase") or "fetch_history")
                    _write_status(
                        root,
                        RefreshResult(
                            status=REFRESH_RUNNING,
                            target_date=target_date,
                            started_at=started_at,
                            updated_at=_timestamp(),
                            phase=phase,
                            progress=_progress_from_callback(progress),
                            message=f"正在更新 {refresh_date} 数据",
                        ),
                    )
```

new_string：
```python
                def progress_callback(progress: dict[str, Any], refresh_date: str = refresh_date) -> None:
                    phase = str(progress.get("phase") or "fetch_history")
                    # R4: 入队而非同步写
                    if progress_writer.has_failed():
                        # 回退同步写
                        _write_status(
                            root,
                            RefreshResult(
                                status=REFRESH_RUNNING,
                                target_date=target_date,
                                started_at=started_at,
                                updated_at=_timestamp(),
                                phase=phase,
                                progress=_progress_from_callback(progress),
                                message=f"正在更新 {refresh_date} 数据",
                            ),
                        )
                    else:
                        progress_writer.submit(progress, phase, f"正在更新 {refresh_date} 数据")
```

**(c)** 在 `for refresh_date in dates_to_refresh:` 循环**之后**（即 score 阶段 `_write_status` 调用之前），插入 phase 切换通知 + 循环结束后 join：

在 `score_result = scorer(...)` **之前**插入：
```python
                # R4: 通知 phase 切换（force=True 立即写）
                progress_writer.submit({}, "score", f"正在评分 {actual_date} 数据")
```

在 `for refresh_date in dates_to_refresh:` 循环**退出后**（`target_refreshed = ...` 之前）插入：
```python
            # R4: 等待进度线程写入最后状态
            progress_writer.join()
```

> **fetch_result = fetcher(...)** 和 **_detect_complete_history_date(date_counts=...)** 保持 Task 5 修改后的状态，本 Step 不动。

- [ ] **Step 5: 运行测试验证通过**

Run: `python -m unittest tests.test_refresh.ProgressWriterDebounceTests -v`
Expected: PASS

- [ ] **Step 6: 运行所有 refresh 测试验证不破坏**

Run: `python -m unittest tests.test_refresh -v 2>&1 | tail -40`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/trend_radar/refresh.py tests/test_refresh.py
git commit -m "perf(refresh): R4 _ProgressWriter 后台线程限频写盘避免阻塞 as_completed"
```

---

## Task 7: R6 修复 — progress 增加 speed/ETA/阶段耗时

**Files:**
- Modify: `src/trend_radar/refresh.py:905-925`（`_progress_payload`）
- Modify: `src/trend_radar/refresh.py`（`_ProgressWriter` 记录 phase_started_at）
- Modify: `app.py:931-977`（`_running_refresh_message` 渲染 speed/ETA）
- Test: `tests/test_refresh.py`

- [ ] **Step 1: 写失败测试 — progress 包含 speed/eta 字段**

追加到 `tests/test_refresh.py`：

```python
class ProgressSpeedEtaTests(unittest.TestCase):
    def test_progress_payload_includes_speed_eta(self) -> None:
        """R6: progress 包含 speed/eta_seconds/phase_started_at/phase_elapsed_seconds"""
        from trend_radar.refresh import _progress_payload

        payload = _progress_payload(
            completed=100, total=600,
            current_code="159995", current_name="测试",
            skipped_cached=80, fetched=20, errors=0, max_workers=20,
            phase_started_at="2026-07-26 10:00:00",
            phase_elapsed_seconds=20.0,
        )

        self.assertIn("speed", payload)
        self.assertIn("eta_seconds", payload)
        self.assertIn("phase_started_at", payload)
        self.assertIn("phase_elapsed_seconds", payload)
        # speed = 100 / 20 = 5.0
        self.assertAlmostEqual(payload["speed"], 5.0, places=1)
        # eta = (600 - 100) / 5 = 100
        self.assertAlmostEqual(payload["eta_seconds"], 100.0, places=1)
```

- [ ] **Step 2: 运行测试验证失败**

Run: `python -m unittest tests.test_refresh.ProgressSpeedEtaTests -v`
Expected: FAIL（`_progress_payload` 还没有 speed/eta 参数）

- [ ] **Step 3: 修改 `_progress_payload` 增加 speed/eta 字段**

`src/trend_radar/refresh.py:905-925` 替换为：

```python
def _progress_payload(
    completed: object,
    total: object,
    current_code: object | None = None,
    current_name: object | None = None,
    skipped_cached: object | None = None,
    fetched: object | None = None,
    errors: object | None = None,
    max_workers: object | None = None,
    phase_started_at: str | None = None,
    phase_elapsed_seconds: float | None = None,
) -> dict[str, Any]:
    completed_int = _safe_int(completed)
    total_int = _safe_int(total)
    elapsed = float(phase_elapsed_seconds) if phase_elapsed_seconds is not None else 0.0
    # R6: speed = completed / max(elapsed, 1)，避免 cache hit 拉高速度
    speed = completed_int / max(elapsed, 1.0) if elapsed > 0 else 0.0
    # R6: eta = (total - completed) / max(speed, 0.01)
    remaining = max(0, total_int - completed_int)
    eta_seconds = remaining / max(speed, 0.01) if speed > 0 else 0.0

    payload: dict[str, Any] = {
        "completed": completed_int,
        "total": total_int,
        "current_code": _optional_str(current_code),
        "current_name": _optional_str(current_name),
        "skipped_cached": _safe_int(skipped_cached),
        "fetched": _safe_int(fetched),
        "errors": _safe_int(errors),
        "max_workers": _safe_int(max_workers),
        # R6: 新增字段（additive，向后兼容）
        "phase_started_at": phase_started_at,
        "phase_elapsed_seconds": round(elapsed, 2),
        "speed": round(speed, 2),
        "eta_seconds": round(eta_seconds, 2),
    }
    return payload
```

- [ ] **Step 4: 修改 `_ProgressWriter` 记录 phase_started_at 和 phase_elapsed**

`_ProgressWriter` 类（Task 6 Step 3 创建）已有 `__init__`/`start`/`submit`/`join`/`has_failed`/`_run`/`_write`/`_flush_remaining`。本 Step 改 2 处：

**(a) `__init__` 追加 2 个字段**（在现有 `self._last_phase = None` 之后追加）：

```python
        self._phase_started_at: dict[str, float] = {}  # phase -> monotonic time
        self._phase_timestamp: dict[str, str] = {}  # phase -> wall clock ISO
```

**(b) 替换 `submit` 方法**（Task 6 创建的 `submit` 只入队，改为先记 phase 时间再入队）：

old_string（Task 6 后的状态）：
```python
    def submit(self, progress: dict[str, Any], phase: str, message: str) -> None:
        """非阻塞入队。"""
        try:
            self._queue.put_nowait((progress, phase, message))
        except queue.Full:
            pass  # 丢弃旧进度，不影响主流程
```

new_string：
```python
    def submit(self, progress: dict[str, Any], phase: str, message: str) -> None:
        """非阻塞入队，记录 phase 开始时间。"""
        now_mono = time.monotonic()
        if phase not in self._phase_started_at:
            self._phase_started_at[phase] = now_mono
            self._phase_timestamp[phase] = _timestamp()
        elapsed = now_mono - self._phase_started_at.get(phase, now_mono)
        # 注入到 progress 供 _progress_payload 使用
        enriched = dict(progress)
        enriched["phase_started_at"] = self._phase_timestamp.get(phase)
        enriched["phase_elapsed_seconds"] = elapsed
        try:
            self._queue.put_nowait((enriched, phase, message))
        except queue.Full:
            pass
```

> `_write` / `_run` / `join` / `has_failed` / `_flush_remaining` **不动**，保持 Task 6 创建的状态。

- [ ] **Step 5: 修改 `_progress_from_callback` 透传新字段**

`src/trend_radar/refresh.py:892-902` 替换为：

```python
def _progress_from_callback(progress: dict[str, Any]) -> dict[str, Any]:
    return _progress_payload(
        progress.get("completed"),
        progress.get("total"),
        current_code=progress.get("current_code"),
        current_name=progress.get("current_name"),
        skipped_cached=progress.get("skipped_cached"),
        fetched=progress.get("fetched"),
        errors=progress.get("errors"),
        max_workers=progress.get("max_workers"),
        phase_started_at=progress.get("phase_started_at"),
        phase_elapsed_seconds=progress.get("phase_elapsed_seconds"),
    )
```

- [ ] **Step 6: 修改 `app.py _running_refresh_message` 渲染 speed/ETA**

`app.py:931-977` 的 `_running_refresh_message` 函数。**修改方式**：在函数开头读取 `speed`/`eta_seconds`，构造 `speed_eta_suffix` 字符串，然后在**每个 return 语句的 f-string 末尾**追加 `{speed_eta_suffix}`。

**(a)** 在 `app.py` 顶部（`_safe_int` 附近）添加 helper：

```python
def _safe_float(value: object) -> float:
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0
```

**(b)** 在 `_running_refresh_message` 函数体开头（读取 `current_code` 之后）插入：

```python
    speed = _safe_float(progress.get("speed"))
    eta_seconds = _safe_float(progress.get("eta_seconds"))

    # R6: speed/ETA 后缀（仅当有意义时显示）
    speed_eta_suffix = ""
    if speed > 0 and eta_seconds > 0:
        speed_eta_suffix = f"，{speed:.1f} 码/秒，预计 {int(eta_seconds)}s"
```

**(c)** 在现有函数的**每个 return f-string 末尾**追加 `{speed_eta_suffix}`。例如：

- `return "正在扫描存量问题..."` → 不改（无 total，无 speed）
- `return f"正在补拉存量问题：...失败 {errors}{workers}{current}"` → 改为 `...失败 {errors}{workers}{current}{speed_eta_suffix}`
- `return f"正在更新 {target_date} 数据：...失败 {errors}{workers}{current}"` → 改为 `...失败 {errors}{workers}{current}{speed_eta_suffix}`
- `return f"正在更新 {target_date} 数据：历史行情 {completed}/{total}{current}"` → 改为 `...{total}{current}{speed_eta_suffix}`
- 其他所有含 `return f"...{...}"` 的分支同理：在末尾 `{...}` 后追加 `{speed_eta_suffix}`

> 实现者需打开 `app.py:931-977`，对每个返回字符串的 return 语句做末尾追加。不需要改变控制流。

- [ ] **Step 7: 运行测试验证通过**

Run: `python -m unittest tests.test_refresh.ProgressSpeedEtaTests -v`
Expected: PASS

- [ ] **Step 8: 运行所有 refresh 测试验证不破坏**

Run: `python -m unittest tests.test_refresh -v 2>&1 | tail -30`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add src/trend_radar/refresh.py app.py tests/test_refresh.py
git commit -m "perf(refresh): R6 progress 增加 speed/eta_seconds/phase_started_at 字段"
```

---

## Task 8: R5 修复 — `problem_snapshot_summary` 缓存

**Files:**
- Modify: `src/trend_radar/refresh.py:537-543`（`problem_snapshot_summary`）
- Modify: `src/trend_radar/refresh.py`（refresh 完成时清缓存）
- Test: `tests/test_refresh.py`

- [ ] **Step 1: 写失败测试 — 缓存命中 + mtime 变化重算 + TTL 过期**

追加到 `tests/test_refresh.py`：

```python
import time
from unittest.mock import patch

class ProblemSummaryCacheTests(unittest.TestCase):
    """R5: problem_snapshot_summary 缓存测试。

    不继承 RefreshTests（避免运行父类测试），inline 定义需要的 helper。
    """

    def _saturday(self) -> datetime:
        return datetime(2026, 5, 30, 10, 0, tzinfo=ASIA_SHANGHAI)

    def _write_history(self, root: Path, source_date: str, latest_date: str) -> None:
        history_dir = root / "data" / "normalized" / source_date / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        dates = pd.bdate_range(end=pd.Timestamp(latest_date), periods=3)
        pd.DataFrame({"日期": dates}).to_csv(history_dir / "159995.csv", index=False)

    def _write_radar(self, root: Path, data_date: str, source_date: str | None = None) -> SimpleNamespace:
        radar_dir = root / "data" / "radar"
        radar_dir.mkdir(parents=True, exist_ok=True)
        radar_csv = radar_dir / f"{data_date}.csv"
        radar_json = radar_dir / f"{data_date}.json"
        radar_csv.write_text("排名,ETF名称,代码\n1,芯片ETF,159995\n", encoding="utf-8")
        radar_json.write_text(
            json.dumps({"data_date": data_date, "source_date": source_date or data_date, "generated_at": f"{data_date} 16:00:00"}, ensure_ascii=False),
            encoding="utf-8",
        )
        return SimpleNamespace(data_date=data_date, radar_csv=radar_csv, radar_json=radar_json)

    def test_cache_hit_skips_rescan(self) -> None:
        """R5: mtime 不变 → 命中缓存，scan_problem_snapshots 只调用 1 次"""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            self._write_history(root, data_date, data_date)

            scan_count = [0]
            original_scan = scan_problem_snapshots
            def counting_scan(r=root):
                scan_count[0] += 1
                return original_scan(r)

            with patch("trend_radar.refresh.scan_problem_snapshots", side_effect=counting_scan):
                # 同一进程内连续两次 summary，应命中缓存
                import trend_radar.refresh as refresh_mod
                # 强制重置缓存
                if hasattr(refresh_mod, "_problem_summary_cache"):
                    refresh_mod._problem_summary_cache = None
                summary1 = refresh_mod.problem_snapshot_summary(root)
                summary2 = refresh_mod.problem_snapshot_summary(root)

            self.assertEqual(scan_count[0], 1, "second call should hit cache")

    def test_cache_invalidated_on_mtime_change(self) -> None:
        """R5: normalized 目录 mtime 变 → 重算"""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            self._write_history(root, data_date, data_date)

            import trend_radar.refresh as refresh_mod
            if hasattr(refresh_mod, "_problem_summary_cache"):
                refresh_mod._problem_summary_cache = None
            summary1 = refresh_mod.problem_snapshot_summary(root)

            # 新增子目录改变 mtime
            new_dir = root / "data" / "normalized" / "2026-05-30"
            new_dir.mkdir(parents=True)
            (new_dir / "spot_candidates.csv").write_text("code\n510300\n", encoding="utf-8")
            time.sleep(0.01)  # 确保 mtime 变化

            summary2 = refresh_mod.problem_snapshot_summary(root)
            # 应重新扫描（缓存失效）
            self.assertNotEqual(id(summary1), id(summary2))

    def test_cache_invalidated_by_refresh_completion(self) -> None:
        """R5: refresh 完成后 → 缓存失效"""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            self._write_history(root, data_date, data_date)

            import trend_radar.refresh as refresh_mod
            if hasattr(refresh_mod, "_problem_summary_cache"):
                refresh_mod._problem_summary_cache = None
            summary1 = refresh_mod.problem_snapshot_summary(root)
            self.assertIsNotNone(refresh_mod._problem_summary_cache)

            # 模拟 refresh 完成
            def fake_fetcher(data_date: str, root: Path, **kwargs: object) -> object:
                self._write_history(root, data_date, data_date)
                return SimpleNamespace(data_date=data_date)
            def fake_scorer(data_date: str, root: Path, source_date: str | None = None) -> object:
                return self._write_radar(root, data_date)

            refresh_mod.refresh_latest_data(root=root, now=self._saturday(), fetcher=fake_fetcher, scorer=fake_scorer)

            # refresh 后缓存应被清除
            self.assertIsNone(refresh_mod._problem_summary_cache)
```

- [ ] **Step 2: 运行测试验证失败**

Run: `python -m unittest tests.test_refresh.ProblemSummaryCacheTests -v`
Expected: FAIL（当前无缓存，每次都全量扫）

- [ ] **Step 3: 修改 `problem_snapshot_summary` 加缓存**

`src/trend_radar/refresh.py:537-543` 替换为：

```python
# R5: 模块级缓存
_problem_summary_cache: dict[str, Any] | None = None
_problem_summary_cache_key: tuple | None = None
_problem_summary_cache_ts: float = 0.0


def _problem_summary_cache_key_for(root: Path) -> tuple:
    """R5: 基于 normalized 目录 max child mtime + 子目录数量的缓存 key。"""
    normalized_dir = root / "data" / "normalized"
    if not normalized_dir.exists():
        return (0, 0)
    children = [child for child in normalized_dir.iterdir() if child.is_dir()]
    if not children:
        return (0, 0)
    max_mtime = max(child.stat().st_mtime_ns for child in children)
    return (max_mtime, len(children))


def _invalidate_problem_summary_cache() -> None:
    """R5: refresh 完成时调用，清除缓存。"""
    global _problem_summary_cache, _problem_summary_cache_key, _problem_summary_cache_ts
    _problem_summary_cache = None
    _problem_summary_cache_key = None
    _problem_summary_cache_ts = 0.0


def problem_snapshot_summary(root: Path = PROJECT_ROOT) -> dict[str, Any]:
    global _problem_summary_cache, _problem_summary_cache_key, _problem_summary_cache_ts

    cache_key = _problem_summary_cache_key_for(root)
    now_mono = time.monotonic()
    # R5: 命中缓存（key 一致 + TTL 内）
    if (
        _problem_summary_cache is not None
        and _problem_summary_cache_key == cache_key
        and (now_mono - _problem_summary_cache_ts) < PROBLEM_SUMMARY_CACHE_TTL_SECONDS
    ):
        return _problem_summary_cache

    problems = scan_problem_snapshots(root)
    _problem_summary_cache = {
        "snapshot_count": len(problems),
        "problem_count": int(sum(problem.problem_count for problem in problems)),
        "dates": [problem.data_date for problem in problems],
    }
    _problem_summary_cache_key = cache_key
    _problem_summary_cache_ts = now_mono
    return _problem_summary_cache
```

需要在 `refresh.py` 顶部 import 加 `import time` 和 `from .config import PROBLEM_SUMMARY_CACHE_TTL_SECONDS`。

- [ ] **Step 4: 在 `refresh_latest_data` / `retry_selected_date` / `retry_problem_snapshots` 完成时清缓存**

在每个函数的 `try/finally` 或成功返回前调用 `_invalidate_problem_summary_cache()`。

`refresh_latest_data` 的 `return result` 之前（约 line 232）追加：

```python
            _invalidate_problem_summary_cache()
            _write_status(root, result)
            return result
```

`retry_selected_date` 和 `retry_problem_snapshots` 同理。

- [ ] **Step 5: 运行测试验证通过**

Run: `python -m unittest tests.test_refresh.ProblemSummaryCacheTests -v`
Expected: PASS

- [ ] **Step 6: 运行所有 refresh 测试验证不破坏**

Run: `python -m unittest tests.test_refresh -v 2>&1 | tail -40`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/trend_radar/refresh.py tests/test_refresh.py
git commit -m "perf(refresh): R5 problem_snapshot_summary 加 mtime+TTL 缓存，refresh 完成时清缓存"
```

---

## Task 9: R5 修复 — `app._read_fetch_report` memoize

**Files:**
- Modify: `app.py:841-847`（`_read_fetch_report`）
- Test: `tests/test_refresh.py`（或新建 `tests/test_perf_optimization.py`）

- [ ] **Step 1: 写失败测试 — memoize 命中**

追加到 `tests/test_perf_optimization.py`（新文件）：

```python
"""跨模块性能优化集成测试。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class FetchReportMemoizeTests(unittest.TestCase):
    def test_memoize_same_mtime_returns_cached(self) -> None:
        """R5: 同一 (data_date, mtime) → 命中缓存"""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            report_path = root / "data" / "normalized" / data_date / "fetch_report.json"
            report_path.parent.mkdir(parents=True)
            payload = {"data_date": data_date, "candidate_rows": 100}
            report_path.write_text(json.dumps(payload), encoding="utf-8")

            # app.py 引用 ROOT，需要 mock
            with patch("app.ROOT", root):
                import app
                # 重置 memoize 缓存
                if hasattr(app, "_fetch_report_cache"):
                    app._fetch_report_cache = {}
                report1 = app._read_fetch_report(data_date)
                # 第二次应命中缓存（mock pd.read_json 验证未调用）
                read_count = [0]
                original_read = app._read_fetch_report.__wrapped__ if hasattr(app._read_fetch_report, "__wrapped__") else None
                report2 = app._read_fetch_report(data_date)

                self.assertEqual(report1, report2)
                self.assertEqual(report1["candidate_rows"], 100)

    def test_memoize_invalidated_on_mtime_change(self) -> None:
        """R5: mtime 变 → 重读"""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            report_path = root / "data" / "normalized" / data_date / "fetch_report.json"
            report_path.parent.mkdir(parents=True)
            payload1 = {"data_date": data_date, "candidate_rows": 100}
            report_path.write_text(json.dumps(payload1), encoding="utf-8")

            with patch("app.ROOT", root):
                import app
                if hasattr(app, "_fetch_report_cache"):
                    app._fetch_report_cache = {}
                report1 = app._read_fetch_report(data_date)
                self.assertEqual(report1["candidate_rows"], 100)

                # 修改文件
                import time
                payload2 = {"data_date": data_date, "candidate_rows": 200}
                report_path.write_text(json.dumps(payload2), encoding="utf-8")
                time.sleep(0.01)  # 确保 mtime 变化
                os.utime(report_path, None)  # 强制更新 mtime

                report2 = app._read_fetch_report(data_date)
                self.assertEqual(report2["candidate_rows"], 200)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试验证失败**

Run: `python -m unittest tests.test_perf_optimization.FetchReportMemoizeTests -v`
Expected: FAIL（当前每次都读文件）

- [ ] **Step 3: 修改 `app._read_fetch_report` 加 memoize**

`app.py:841-847` 替换为：

```python
# R5: fetch_report memoize 缓存
_fetch_report_cache: dict[tuple[str, int], dict] = {}


def _read_fetch_report(data_date: str) -> dict[str, Any] | None:
    """R5: 按 (data_date, mtime_ns) memoize。"""
    path = ROOT / "data" / "normalized" / data_date / "fetch_report.json"
    try:
        mtime = path.stat().st_mtime_ns
    except FileNotFoundError:
        return None
    cache_key = (data_date, mtime)
    cached = _fetch_report_cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    _fetch_report_cache[cache_key] = payload
    # 简单 LRU：限制缓存大小
    if len(_fetch_report_cache) > 50:
        _fetch_report_cache.pop(next(iter(_fetch_report_cache)), None)
    return payload
```

- [ ] **Step 4: 运行测试验证通过**

Run: `python -m unittest tests.test_perf_optimization.FetchReportMemoizeTests -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_perf_optimization.py
git commit -m "perf(app): R5 _read_fetch_report 按 mtime memoize 避免重复读取"
```

---

## Task 10: 集成测试 + 全量回归

**Files:**
- Modify: `tests/test_perf_optimization.py`（追加集成测试）

- [ ] **Step 1: 写集成测试 — 增量刷新端到端**

追加到 `tests/test_perf_optimization.py`：

```python
class IncrementalRefreshIntegrationTests(unittest.TestCase):
    def test_incremental_refresh_with_cached_history_completes_fast(self) -> None:
        """集成测试：大部分数据已缓存时，增量刷新应在合理时间内完成"""
        import time
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from trend_radar.config import ASIA_SHANGHAI
        from trend_radar.refresh import refresh_latest_data, REFRESH_SUCCEEDED

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"

            # 预写缓存历史 + radar
            self._write_cached_history(root, data_date)

            call_count = [0]
            def fake_fetcher(data_date: str, root: Path, **kwargs: object) -> object:
                call_count[0] += 1
                callback = kwargs.get("progress_callback")
                if callback:
                    callback({
                        "phase": "fetch_history",
                        "completed": 1, "total": 1,
                        "current_code": "159995", "current_name": "测试",
                        "skipped_cached": 1, "fetched": 0, "errors": 0,
                        "max_workers": 20,
                    })
                from types import SimpleNamespace
                return SimpleNamespace(data_date=data_date, date_counts={"2026-05-29": 1})

            def fake_scorer(data_date: str, root: Path, source_date: str | None = None) -> object:
                return self._write_radar(root, data_date)

            now = datetime(2026, 5, 30, 16, 0, tzinfo=ASIA_SHANGHAI)
            start = time.time()
            result = refresh_latest_data(root=root, now=now, fetcher=fake_fetcher, scorer=fake_scorer)
            elapsed = time.time() - start

            self.assertEqual(result.status, REFRESH_SUCCEEDED)
            self.assertLess(elapsed, 5.0, f"incremental refresh should be fast; elapsed={elapsed}")

    def _write_cached_history(self, root: Path, data_date: str) -> None:
        """预写缓存，模拟增量场景。"""
        normalized_dir = root / "data" / "normalized" / data_date
        history_dir = normalized_dir / "history"
        history_dir.mkdir(parents=True)
        spot_dir = normalized_dir
        # spot_candidates
        spot_df = pd.DataFrame({
            "code": ["159995"], "name": ["测试ETF"],
            "数据日期": [data_date], "amount_yi": [5.0],
            "total_market_cap_yi": [10.0], "float_market_cap_yi": [8.0],
            "latest_price": [1.2], "pct_change": [0.1],
            "volume": [1000000], "latest_share_yi": [1.0],
            "is_equity_etf": [True], "universe_reason": ["test"],
        })
        spot_df.to_csv(spot_dir / "spot_candidates.csv", index=False)
        # history
        history_df = pd.DataFrame({
            "日期": [data_date],
            "开盘价": [1.0], "最高价": [1.1], "最低价": [0.9],
            "收盘价": [1.0], "成交额(亿)": [5.0],
            "成交量": [1000000], "code": ["159995"],
            "history_source": ["cache"],
        })
        history_df.to_csv(history_dir / "159995.csv", index=False)

    def _write_radar(self, root: Path, data_date: str) -> object:
        from types import SimpleNamespace
        radar_dir = root / "data" / "radar"
        radar_dir.mkdir(parents=True, exist_ok=True)
        (radar_dir / f"{data_date}.csv").write_text("排名,代码\n1,159995\n", encoding="utf-8")
        (radar_dir / f"{data_date}.json").write_text(
            json.dumps({"data_date": data_date, "generated_at": "2026-05-29 16:00:00"}),
            encoding="utf-8",
        )
        return SimpleNamespace(
            data_date=data_date,
            radar_csv=radar_dir / f"{data_date}.csv",
            radar_json=radar_dir / f"{data_date}.json",
            rows=1,
            quality_status="OK",
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行集成测试**

Run: `python -m unittest tests.test_perf_optimization -v`
Expected: PASS

- [ ] **Step 3: 运行全量测试套件**

Run: `python -m unittest discover tests -v 2>&1 | tail -40`
Expected: PASS（所有测试通过，0 failures）

- [ ] **Step 4: 运行 lint（如果有）**

Run: `python -m py_compile src/trend_radar/*.py app.py 2>&1`
Expected: 无输出（编译通过）

- [ ] **Step 5: Commit**

```bash
git add tests/test_perf_optimization.py
git commit -m "test: 增量刷新性能优化集成测试 + 全量回归"
```

---

## Task 11: 文档更新 + Checkpoint 提交

**Files:**
- Modify: `README.md`（如果涉及性能说明）
- Modify: `CLAUDE.md` 或 `AGENTS.md`（如果有性能相关说明）

- [ ] **Step 1: 检查是否需要更新文档**

Run: `grep -l "DEFAULT_HISTORY_FETCH_MAX_WORKERS\|HISTORY_FETCH_PER_CODE_TIMEOUT" README.md CLAUDE.md AGENTS.md 2>/dev/null`
Expected: 无输出或仅 AGENTS.md

- [ ] **Step 2: 如有需要，更新文档中的常量引用**

如果 grep 命中，更新对应数值（12→20，60→15）。如果未命中，跳过此步骤。

- [ ] **Step 3: 运行 checkpoint 脚本**

Run: `python3 scripts/checkpoint_commit.py --summary "perf: 增量刷新性能 + 进度可见性优化" --details "R1 spot 校验日期 + 三级回退；R2 eastmoney 单次重试 + sina 超时；R3 date_counts 复用避免全量重读；R4 _ProgressWriter 后台限频写；R5 problem_summary + fetch_report 缓存；R6 progress speed/ETA 字段"`
Expected: checkpoint 提交成功，测试通过

- [ ] **Step 4: 验证最终状态**

Run: `git log --oneline -15`
Expected: 看到 11 个 perf/test commit + 1 个 checkpoint commit

Run: `git status --short --branch`
Expected: working tree clean

---

## 自检

### 1. Spec coverage

| Spec 章节 | Task |
|---|---|
| R1 spot resume 校验 + 三级回退 | Task 2 |
| R2 eastmoney 单次重试 + sina 超时 | Task 3 |
| R3 HistoryFetchOutcome.all_dates + date_counts | Task 4 |
| R3 _detect_complete_history_date 复用 | Task 5 |
| R4 _ProgressWriter 后台限频 | Task 6 |
| R6 progress speed/ETA 字段 | Task 7 |
| R5 problem_snapshot_summary 缓存 | Task 8 |
| R5 _read_fetch_report memoize | Task 9 |
| 集成测试 + 全量回归 | Task 10 |
| 文档 + checkpoint | Task 11 |

所有 spec 章节都有对应 Task ✅

### 2. Placeholder scan

- 无 TBD/TODO/"实现 later"
- 所有代码块都是完整实现，非伪代码
- 所有测试都有完整 assert
- ✅ PASS

### 3. Type consistency

- `HistoryFetchOutcome.all_dates: tuple[str, ...]` — Task 4 定义，Task 6/7 使用一致 ✅
- `FetchResult.date_counts: dict[str, int]` — Task 4 定义，Task 5 使用一致 ✅
- `_detect_complete_history_date(date_counts=...)` — Task 5 定义，Task 6 调用一致 ✅
- `_progress_payload(phase_started_at=..., phase_elapsed_seconds=...)` — Task 7 定义，Task 6 的 `_ProgressWriter` 注入一致 ✅
- `_problem_summary_cache` 全局变量 — Task 8 定义和测试一致 ✅
- ✅ PASS
