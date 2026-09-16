# A股ETF主线趋势雷达

本项目当前包含 Step 1 界面原型和 Step 2 真实数据采集骨架：Streamlit 仪表盘优先读取本地真实缓存，没有缓存时自动回退到静态 mock 数据，不提供交易建议。

## 运行

最简单的方式：双击项目根目录里的 `open_trend_radar.command`。

- 打开时会自动启动本地网站并打开浏览器。
- 关闭浏览器页面不会停止服务；在启动终端按 `Ctrl-C` 可主动停止。
- 重复打开会直接唤起已运行的网站，不会启动第二份服务。
- 首次启动如果缺少 `.venv` 或依赖，会自动创建和安装。

也可以从终端启动同一个本地网站模式：

```bash
./open_trend_radar.command
```

### 本地域名访问（macOS）

运行一次以下安装器，可直接在浏览器访问 `http://trend.test`，无需输入端口。它会安装 Caddy 作为本机 `80 -> 8501` 代理，并用 `launchd` 在登录后启动项目、在意外退出时自动重启：

```bash
./scripts/install_local_domain.sh
```

安装器会请求管理员密码，用于写入 `/etc/hosts`、监听本机 80 端口，以及注册 Caddy 系统服务。它会将域名加入 Wi-Fi 的代理绕过名单，使其在 VPN/系统代理开启时仍直连本机；若网络服务名称不是 Wi-Fi，可用 `TREND_RADAR_PROXY_SERVICE="服务名称" ./scripts/install_local_domain.sh` 指定。它要求运行安装器的 `python3` 为 3.11 或更新版本，并会把该解释器路径固定给后台服务；检测到旧版虚拟环境时会自动重建。重装时会先停止运行副本的旧 Streamlit 进程，避免占用 8501。项目本身作为当前用户的服务运行；为避开 macOS 对“文稿”目录的后台访问限制，安装器会将可运行副本同步到 `~/Library/Application Support/TrendRadar`。修改项目代码后重新运行安装器即可更新运行副本；数据会随首次安装同步，之后由运行副本持续维护。日志在 `/tmp/trend-radar-launcher*.log`。

首次安装后，页面顶部会常驻“更新并重启服务”按钮。确认后它会打开终端，运行固定的本机更新脚本：同步工作区代码到运行副本并重启服务；更新完成后刷新浏览器即可。该流程不要求管理员密码，也不会从网页接收任意 shell 命令。

传统 Streamlit 运行方式仍然可用：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install -e .
streamlit run app.py
```

## 采集与评分

```bash
python -m trend_radar fetch --date 2026-05-28
python -m trend_radar score --date 2026-05-28
python -m trend_radar refresh
python -m trend_radar refresh --force
python -m trend_radar crypto-alerts --format table
python -m trend_radar crypto-alerts --force --format json
python -m trend_radar backfill --months 1
python -m trend_radar validate --start 2026-04-28 --end 2026-05-28
python -m trend_radar audit --start 2026-04-28 --end 2026-05-29 --external --repair
streamlit run app.py
```

真实数据会写入 `data/`：

- `data/raw/YYYY-MM-DD/`：AkShare 原始接口缓存
- `data/normalized/YYYY-MM-DD/`：标准化行情、历史、份额数据
- `data/radar/YYYY-MM-DD.csv`：完整排行榜
- `data/radar/YYYY-MM-DD.json`：Top10、变化提醒、数据质量
- `data/state/latest.json`：最近一次生成结果
- `data/state/refresh_status.json`：启动时自动更新状态
- `data/state/validation_START_END.json`：回填/展示数据校验报告

`backfill` 会从最近一个完整 `data/normalized/YYYY-MM-DD/` 源目录读取历史日线和份额数据，按最近 20 个交易日生成存量 radar 快照；不会重复伪造历史 raw 行情。`validate` 会校验 CSV/JSON、Top10 变化提醒、非权益 ETF 排除、明细历史和 UI 后端读取函数是否一致。`audit` 会进一步复算成交额 Top30 驻留、成交额排名、候选池和 history 覆盖率；加上 `--external` 会重拉外部日线对账，加上 `--repair` 会先备份再重拉并重算审计区间。

启动 Streamlit 页面时会先显示本地已有缓存，同时在后台检查最新完整交易日数据。若目标日期缺失，会自动执行拉取和评分；完成后页面顶部显示刷新按钮，由用户决定是否切到新数据。中间缺失的交易日不会被自动拉取，如需补齐请使用 `python -m trend_radar backfill`。

## 数据可信度

这是交易参考系统，数据质量优先级高于任何展示和评分优化。排行榜会在每行输出 `数据置信度`、`行情来源`、`份额来源`、`数据问题`：

- `完整` 只表示历史行情、份额和关键字段来源可信。
- `spot_fallback`、实时行情补点、实时行情份额或来源未记录都会标为需复核，不会再显示为完整。
- 如果 Top10 含低置信行情，页面顶部会显示警告，并提示点击“重拉当前日期”重新拉取正确历史行情。
- `validate` 和 `audit` 会检查新增质量字段，审计报告会统计低置信行数和 fallback 来源分布。

## 测试

日常回归使用离线 fixture，不访问外部网络，也不依赖本机 `data/` 缓存：

```bash
.venv/bin/python scripts/run_tests.py --mode offline
```

真实数据冒烟需要显式运行，结果写入 `output/live-smoke/`，不会污染正式缓存：

```bash
.venv/bin/python scripts/run_tests.py --mode live --date auto
```

本地缓存审计会检查当前 `data/` 下已有结果，并把报告写入 `output/test-reports/`：

```bash
.venv/bin/python scripts/run_tests.py --mode audit --date 2026-05-29
```

## 当前范围

- Top10 观察池
- 主线变化提醒
- 完整 ETF 排行榜
- 单 ETF 评分拆解、趋势图、成交额与份额变化图
- 日期切换与历史榜单对比
- 币圈现货固定五币（BTC、ETH、SOL、XAUT、BNB）日线关注提醒；应用启动后在后台执行，使用交易所标准 UTC 日线，并在北京时间次日 08:00 确认

币圈提醒数据写入 `data/crypto/alerts/`，默认配置为 `config/crypto_alerts.default.json`，可通过命名预设调整启用的提醒族和指标窗口。提醒只描述道氏结构、价格行为、MACD/RSI/OBV 背离、TD9/TD13、MA120 与 Vegas 通道的变化，不提供买卖或入场建议。

后续可继续补自动调度、更多数据源交叉校验和通知。
