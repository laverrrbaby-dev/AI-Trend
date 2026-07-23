# Refresh 跳过中间缺失日期 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `refresh_latest_data` 改为只处理最新一个交易日，移除对中间缺失工作日的自动拉取与评分，同时保留覆盖率检测、信号处理、checkpoint 等单日行为。

**Architecture:** 唯一代码改动是 `src/trend_radar/refresh.py` 里 `refresh_latest_data` 的 `dates_to_refresh` 计算；测试调整以删掉断言旧补齐行为的两个用例并新增两个断言新行为的用例；文档只微调 `refresh` 命令的注释描述。

**Tech Stack:** Python 3, `unittest`, pandas, 现有 AkShare 拉取链路（本次不触碰）。

---

## File Structure

- `src/trend_radar/refresh.py` — 唯一代码改动点（`refresh_latest_data` 第 141 行附近）。
- `tests/test_refresh.py` — 移除两个旧用例，新增两个新用例。
- `CLAUDE.md` — `refresh` 命令的注释从"Auto-fetch+score newest complete trading day"改为更精确描述。
- `README.md` — 在"启动 Streamlit 页面"那段补一句关于"中间缺失日期不再自动拉取"的说明。
- 不动：`fetcher.py`、`__main__.py`、`app.py`、`backfill.py`、`audit.py`、`validation.py`、`scoring.py`、其他测试与文档。

---

## Task 1: 调整 test_refresh.py 用例

**Files:**
- Modify: `tests/test_refresh.py:62-117`（删除两个旧 gap 用例）
- Modify: `tests/test_refresh.py`（新增两个新用例）

- [ ] **Step 1: 删除两个旧用例**

打开 `tests/test_refresh.py`，删除以下两个整段函数定义：

- `test_missing_weekday_gap_is_backfilled_before_target`（大约 62–92 行）
- `test_existing_target_still_backfills_missing_weekday_gap`（大约 94–117 行）

每个用例的删除范围都包含 `def` 起始和下一个顶层 `def` 之前的空行。

- [ ] **Step 2: 在 RefreshTests 类内追加两个新用例**

在 `RefreshTests` 类内、紧接现有 `test_existing_target_radar_skips_without_fetching` 用例之后，插入以下两块代码（按顺序）：

```python
    def test_refresh_does_not_backfill_intermediate_weekday_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._write_radar(root, "2026-07-15")
            calls: list[tuple[str, str, str | None]] = []

            def fake_fetcher(data_date: str, root: Path, **kwargs: object) -> object:
                calls.append(("fetch", data_date, None))
                self._write_history(root, data_date, data_date)
                return SimpleNamespace(data_date=data_date)

            def fake_scorer(data_date: str, root: Path, source_date: str | None = None) -> object:
                calls.append(("score", data_date, source_date))
                return self._write_radar(root, data_date, source_date=source_date)

            now = datetime(2026, 7, 22, 16, 0, tzinfo=ASIA_SHANGHAI)
            result = refresh_latest_data(root=root, now=now, fetcher=fake_fetcher, scorer=fake_scorer)

            self.assertEqual(result.status, REFRESH_SUCCEEDED)
            self.assertEqual(result.actual_date, "2026-07-22")
            self.assertEqual(
                calls,
                [("fetch", "2026-07-22", None), ("score", "2026-07-22", None)],
            )
            self.assertEqual(result.message, "已生成 2026-07-22 数据")
            for missing_date in ("2026-07-16", "2026-07-17", "2026-07-20", "2026-07-21"):
                self.assertFalse(
                    (root / "data" / "normalized" / missing_date).exists(),
                    f"intermediate snapshot {missing_date} should not be created",
                )

    def test_existing_target_radar_skips_when_intermediate_dates_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._write_radar(root, "2026-07-15")
            self._write_radar(root, "2026-07-22")

            def fail_fetcher(**kwargs: object) -> None:
                raise AssertionError("fetcher should not be called")

            now = datetime(2026, 7, 22, 16, 0, tzinfo=ASIA_SHANGHAI)
            result = refresh_latest_data(root=root, now=now, fetcher=fail_fetcher)

            self.assertEqual(result.status, REFRESH_SKIPPED)
            self.assertEqual(result.message, "当前已是最新 2026-07-22 数据")
```

- [ ] **Step 3: 检查 import 充足**

确认 `tests/test_refresh.py` 顶部仍然 import 了以下符号（修改前就应已存在；新代码不引入新依赖）：

- `tempfile`、`Path`、`datetime`、`SimpleNamespace`（已在文件内）
- `REFRESH_SKIPPED`、`REFRESH_SUCCEEDED`、`refresh_latest_data`（已在 import 列表）
- `ASIA_SHANGHAI`（已 import）

不需要新增 import。

- [ ] **Step 4: Commit 测试调整**

```bash
git add tests/test_refresh.py
git commit -m "test: refresh 不再补齐中间工作日"
```

---

## Task 2: 确认新测试在旧代码下失败

**Files:** 无（只跑测试）

- [ ] **Step 1: 跑新测试，预期失败**

```bash
.venv/bin/python -m unittest tests.test_refresh.RefreshTests.test_refresh_does_not_backfill_intermediate_weekday_gap tests.test_refresh.RefreshTests.test_existing_target_radar_skips_when_intermediate_dates_missing -v
```

预期：两个测试都 FAIL（`test_refresh_does_not_backfill_intermediate_weekday_gap` 应当看到 `calls` 长度超过 2 / 多个 fetch 调用；`test_existing_target_radar_skips_when_intermediate_dates_missing` 应当看到 `AssertionError: fetcher should not be called`）。

- [ ] **Step 2: 不要修代码，继续 Task 3**

如果两个测试都按预期失败，Task 1 完成。如果有一个意外通过，先停下来 review 它是不是真的断言了"不补齐"——可能漏写了断言。

---

## Task 3: 修改 refresh.py

**Files:**
- Modify: `src/trend_radar/refresh.py:141`（一行替换）

- [ ] **Step 1: 替换 `dates_to_refresh` 计算**

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

注意缩进：当前是函数体内 12 空格，替换后保持 12 空格的悬挂缩进（`[` 顶到 12 空格，`if` 行内容接其后）。

- [ ] **Step 2: 不动其他行**

不要删除 `_refresh_date_sequence` / `_missing_weekday_radar_dates` / `_previous_weekday` / `_refresh_success_message` 这些辅助函数；它们仍被模块保留，未来如需恢复补齐能力可直接重新挂上。也不动 `refresh_latest_data` 函数体内 `existing_radar = ...` 那行、以及 for 循环 / `_remove_incomplete_target_outputs` / `_promote_latest_from_radar` 等下游逻辑。

- [ ] **Step 3: Commit 代码改动**

```bash
git add src/trend_radar/refresh.py
git commit -m "feat(refresh): 不再自动补齐中间缺失日期"
```

---

## Task 4: 跑新测试，预期通过

**Files:** 无

- [ ] **Step 1: 跑新测试**

```bash
.venv/bin/python -m unittest tests.test_refresh.RefreshTests.test_refresh_does_not_backfill_intermediate_weekday_gap tests.test_refresh.RefreshTests.test_existing_target_radar_skips_when_intermediate_dates_missing -v
```

预期：两个测试都 PASS。

- [ ] **Step 2: 跑整套 refresh 测试**

```bash
.venv/bin/python -m unittest tests.test_refresh -v
```

预期：所有 `RefreshTests` 用例 PASS（特别是 `test_existing_target_radar_skips_without_fetching`、`test_missing_target_fetches_then_scores_and_writes_success`、`test_stale_source_scores_actual_latest_history_date`、`test_partial_target_coverage_scores_recent_complete_history_date`、`test_fetch_progress_is_written_to_status`、`test_refresh_passes_resume_and_worker_options_to_fetcher`、lock / retry / scan_problem 等仍通过）。

---

## Task 5: 跑全量离线测试套件

**Files:** 无

- [ ] **Step 1: 跑 offline 测试**

```bash
.venv/bin/python scripts/run_tests.py --mode offline
```

预期：所有用例 PASS。

- [ ] **Step 2: 如有失败，先修再继续**

任何新失败都属于本次改动引入的回归，回到 Task 3 检查代码或 Task 1 检查测试。不要带着失败继续 Task 6。

---

## Task 6: 更新文档

**Files:**
- Modify: `CLAUDE.md:32-33`
- Modify: `README.md:54` 附近

- [ ] **Step 1: 改 `CLAUDE.md` 的 refresh 注释**

把：

```markdown
python -m trend_radar refresh                         # Auto-fetch+score newest complete trading day (if needed)
python -m trend_radar refresh --force                 # Force refresh even if target radar exists
```

改为：

```markdown
python -m trend_radar refresh                         # Auto-fetch+score newest complete trading day only; intermediate missing dates are NOT auto-filled
python -m trend_radar refresh --force                 # Force refresh even if target radar exists
```

- [ ] **Step 2: 改 `README.md` 的启动行为段**

把第 54 行：

```markdown
启动 Streamlit 页面时会先显示本地已有缓存，同时在后台检查最新完整交易日数据。若目标日期缺失，会自动执行拉取和评分；完成后页面顶部显示刷新按钮，由用户决定是否切到新数据。
```

改为：

```markdown
启动 Streamlit 页面时会先显示本地已有缓存，同时在后台检查最新完整交易日数据。若目标日期缺失，会自动执行拉取和评分；完成后页面顶部显示刷新按钮，由用户决定是否切到新数据。中间缺失的交易日不会被自动拉取，如需补齐请使用 `python -m trend_radar backfill`。
```

- [ ] **Step 3: 跑一次 offline 测试再 commit**

```bash
.venv/bin/python scripts/run_tests.py --mode offline
```

预期：PASS（文档改动不影响测试，但走一遍以防误改测试文件）。

- [ ] **Step 4: Commit 文档改动**

```bash
git add CLAUDE.md README.md
git commit -m "docs: refresh 行为说明（不再补齐中间日期）"
```

---

## Task 7: 最终 checkpoint commit

**Files:** 无（脚本会自动写 session log）

- [ ] **Step 1: 跑 checkpoint 脚本**

```bash
python3 scripts/checkpoint_commit.py --summary "refresh 不再自动补齐中间缺失日期" --details "把 refresh_latest_data 的 dates_to_refresh 改为只包含 target_date 一天；调整 test_refresh.py：删除两个旧 gap 用例，新增两个断言新行为的用例；CLAUDE.md/README.md 同步行为描述。"
```

预期：脚本写入 `.codex/session-log/<timestamp>.md`、跑 `python3 -m unittest discover tests`、把所有本次会话相关文件 add 进 staging 并生成 `checkpoint: refresh 不再自动补齐中间缺失日期` 提交。

- [ ] **Step 2: 汇报 commit hash、tests、log path**

在最终回复里给出 commit hash、test status、session log 路径（脚本会打印 commit hash 和 session log）。

---

## Self-Review

- **Spec 覆盖**：
  - 行为变化（Task 3）→ 对应 `refresh_latest_data` 替换。
  - 状态消息（隐含在 Task 4 通过 `assertEqual(result.message, "已生成 2026-07-22 数据")` 覆盖；跳过消息在 Task 4 第二用例覆盖；失败/中断消息由既有测试覆盖）。
  - 覆盖率与不完整产物（既有 `test_partial_target_coverage_scores_recent_complete_history_date` 仍覆盖）。
  - 测试调整（Task 1）→ 删旧 + 加新。
  - 文档（Task 6）→ CLAUDE.md + README.md。
  - 风险与回滚（Task 3 Step 2 + Task 7）→ 辅助函数不删，回滚只需恢复那一行。
- **占位符扫描**：每个 step 都有具体代码或命令；没有 "TODO" / "类似 Task N" / "适当处理" 等。
- **类型/方法一致性**：`REFRESH_SKIPPED` / `REFRESH_SUCCEEDED` 在 import 列表与新测试内一致；`_write_radar` / `_write_history` 在新测试中的调用与既有定义一致；`refresh_latest_data` 签名 `root`, `now`, `fetcher`, `scorer` 与既有测试一致。
