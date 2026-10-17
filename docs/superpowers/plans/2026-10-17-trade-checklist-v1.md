# Trade Checklist V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有币圈页面提供可保存草稿、能阻止错误通过的开仓前检查。

**Architecture:** 规则和草稿接口放在独立模块，Streamlit 页面只负责输入与显示。币圈入口增加“行情提醒 / 交易前检查”选择，默认保持现有行情提醒。草稿使用运行目录下独立 JSON 文件，不能写入或覆盖行情缓存。

**Tech Stack:** Python、Decimal、dataclasses、JSON、Streamlit、unittest、Streamlit AppTest；沿用现有依赖。

**Spec:** `docs/superpowers/specs/2026-10-17-trade-checklist-v1-design.md`（用户已确认）。

## Global Constraints

- 每笔交易前，交易账户资金固定为 500U；平台 OKX，保证金模式全仓。
- 将 OKX 显示的预估强平价作为“执行退出价”填写和核对。
- 做多：执行退出价 ≤ 原计划退出价 < 开仓价 < 止盈价；做空方向相反。
- 计划盈亏比必须严格 > 1.5，按价格距离计算，未扣手续费及资金费。
- 账户、计划、实际下单参数均须手动确认；修改关键参数后相关确认失效。
- 保存当前草稿，不保存交易历史；新一笔清空输入和确认，保留固定规则。
- 不接交易账户、不下单、不追踪持仓、不提供行情建议。
- 当前分支 `main` 实现和提交，不创建新 worktree、不推送。
- 部署保持复制模式；源代码同步到 Application Support，保护运行时 data。

## Test Seams — 用户已确认

1. 规则公开接口：输入检查数据，验证通过状态、原因及盈亏比。
2. 草稿公开接口：真实临时目录中保存、加载、更新、重置及损坏恢复。
3. 页面交互接口：使用 AppTest 操作字段与勾选，观察通过状态、参数变更失效及刷新恢复。

## Review Focus

- NaN、Infinity、零、负数、空白输入必须不能通过；在规则测试覆盖。
- 盈亏比恰好 1.5 和其上下边界不能因显示舍入误判；在规则测试覆盖。
- 编辑后再改回原值也必须重新确认；在页面交互测试覆盖。
- 损坏或部分草稿不能恢复出默认通过状态；在保存接口测试覆盖。
- 草稿写入失败须在页面明确报错，不能提示已保存；在页面边界测试覆盖。

## Task 1: 规则与计算

**Files:** Create `src/trend_radar/trade_checklist.py`; Create `tests/test_trade_checklist.py`.

**Interfaces:**
- `evaluate_checklist(draft: dict[str, Any]) -> ChecklistResult`：返回 `passed: bool`、`errors: tuple[str, ...]`、`reward_distance: Decimal | None`、`risk_distance: Decimal | None`、`reward_risk_ratio: Decimal | None`、`exit_deviation: Decimal | None`。
- 输入字段：`account_balance`, `symbol`, `direction`, `trade_type`, `timeframe`, `exit_reason`, `entry_price`, `planned_exit_price`, `take_profit_price`, `leverage`, `quantity`, `liquidation_price`。
- 确认字段：`previous_position_closed`, `orders_cleared`, `funds_checked`, `okx_cross_checked`, `order_parameters_checked`；仅布尔值 true 算已确认。
- 文本数字用 Decimal 解析；方向和类型使用有限枚举；不静默接受缺失数据。

- [ ] 写一个有效做多案例测试：开仓 100、原退出 90、执行退出 88、止盈 120，盈亏比 5/3、退出偏差 -2；全确认且余额 500 时通过。
- [ ] 运行单文件确认失败，实现最小规则后确认通过。
- [ ] 逐个添加并实现：余额 700、提前退出、做空镜像、价格顺序错误、必填缺失、未确认、非法数字、非正杠杆和数量。
- [ ] 添加边界：开仓 100、退出 90、止盈 115 时失败；115.01 通过；114.99 失败。
- [ ] 运行 `.venv/bin/python -m unittest discover -s tests -p test_trade_checklist.py`；保持类型标注并运行可用静态类型检查工具。环境没有类型工具时记录限制，编译检查不能冒充类型检查。

## Task 2: 草稿持久化

**Files:** Modify `src/trend_radar/trade_checklist.py`; Extend `tests/test_trade_checklist.py`.

**Interfaces:**
- `load_checklist_draft(root: Path) -> dict[str, Any]`：不存在返回空草稿；损坏数据报可显示的 ValueError，不静默标记通过。
- `save_checklist_draft(root: Path, draft: dict[str, Any]) -> None`：白名单字段、版本标记、UTF-8 JSON，临时文件后原子替换。
- 草稿路径：`<root>/data/crypto/trade_checklist/draft.json`。保留字符串数字精度；不序列化“已通过”结果，加载后重新计算。
- 重置通过保存空草稿完成；页面同时清空会话输入和勾选。

- [ ] 在真实临时目录中先写保存后重新加载的失败测试，再实现接口并通过。
- [ ] 逐个添加并实现：替换原草稿、空草稿重置、损坏 JSON、错误字段类型和旧数据不能绕过确认。
- [ ] 再跑规则及草稿单文件测试，编译检查新增模块。

## Task 3: 页面与入口

**Files:** Create `src/trend_radar/trade_checklist_ui.py`; Modify `app.py`; Create `tests/test_trade_checklist_ui.py`; Update affected `tests/test_crypto_dashboard_ui.py` test through public navigation behavior.

**Interfaces:**
- `render_trade_checklist(root: Path) -> None`：四段输入（账户、计划、参数、结果），独立 Streamlit key 前缀。
- `_render_crypto_turnover_page()` 增加页面选择，行情提醒继续调用原 dashboard，检查页调用新 render。
- AppTest 可使用临时目录的测试入口调用 render；不接真实行情或账户，不修改 live 草稿。

- [ ] 用 AppTest 写失败测试：输入有效计划并完成确认，页面出现“检查通过”；再实现输入与结果显示。
- [ ] 余额初值及所有价格、杠杆、数量保持空白，防止默认输入冒充确认；500U 作为规则文字显示。
- [ ] 展示原退出价、执行退出价、偏差、盈利距离、亏损距离及盈亏比；严格门槛用未舍入值计算。说明预估价及未扣费用口径。
- [ ] 逐个添加并实现交互测试：700U 阻止通过；关键字段任意变更清空确认并重算；改回原值仍须确认；新一笔清空；新会话恢复草稿。
- [ ] 字段变更自动保存当前草稿，明确显示保存成功或失败；失败保留会话输入供重试，不能显示已保存。
- [ ] 跑页面单文件与受影响的币圈页面单文件测试；运行模块和 app.py 编译检查，以及可用类型检查工具。

## Task 4: 审查、部署与交付

- [ ] 以 `53f36ef` 为基准，用 code-review 技能分别审查 Standards 与 Spec。对尚未提交的实现检查工作区 diff；生成 checkpoint 后再核对最终提交差异。
- [ ] 审查者读取 spec、AGENTS.md 和本计划；修复真实问题，重复对应单文件测试。
- [ ] 最后通过 checkpoint 运行一次完整测试套件并在当前分支提交。使用项目 `.venv/bin/python -m unittest discover tests`，避免系统 Python 缺依赖导致跳过或导入失败；报告真实运行及跳过数量。
- [ ] 执行 `./scripts/update_local_runtime.sh`，需要时申请 sandbox escalation；不得复制源 data 覆盖 live data。
- [ ] 校验运行副本的 update-source-root.txt 与当前目录一致、cmp app.py、diff -qr src；区分 Python 生成的 __pycache__ 与源文件差异。
- [ ] 在实际运行页面验证入口、空白状态、填写阻断与草稿保存；不要留下伪造的 live 交易草稿，验证前备份并在结束后恢复。
- [ ] 报告页面入口、测试与审查结论、部署状态、提交哈希、会话日志路径。

## Plan Self-Review

覆盖 spec 四步流程、严格数值规则、当前草稿保存/重置及七项验收条件。新增模块不改行情数据管线；所有测试通过公开规则/保存/页面接口，不依赖内部调用次数。用户已确认本计划及上述三处测试边界，使用 implement 流程在当前会话执行。

## Implementation Verification

- 规则与草稿接口：8 项测试通过，覆盖无效数值、方向、严格盈亏比、恢复和重置。
- 页面接口：9 项 AppTest 通过，覆盖参数变更撤销确认、页面切换、保存失败和损坏恢复。
- 原币圈页面：16 项测试通过。
- 新增两个源模块通过 mypy 检查；检查器仅安装在临时目录，不新增项目依赖。
- Standards 审查无发现；Spec 审查发现一次重置失败后的保存重试缺陷，已用失败测试复现并修复，回归通过。
- 数值输入限定 64 字符和 1e-18..1e18，计算使用 128 位精度，避免异常指数和阈值舍入误判。
