#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_ROOT="$HOME/Library/Application Support/TrendRadar"
USER_ID="$(id -u)"
USER_AGENT="$HOME/Library/LaunchAgents/com.trend-radar.app.plist"

if [[ ! -f "$USER_AGENT" ]]; then
  echo "尚未完成首次安装。请先运行 ./scripts/install_local_domain.sh" >&2
  exit 1
fi

echo "正在停止当前 TrendRadar 服务..."
launchctl bootout "gui/$USER_ID" "$USER_AGENT" 2>/dev/null || true
runtime_streamlit_pids="$(pgrep -f "$RUNTIME_ROOT/.venv/bin/python.*streamlit run app.py" || true)"
if [[ -n "$runtime_streamlit_pids" ]]; then
  kill $runtime_streamlit_pids
fi

echo "正在同步最新代码..."
mkdir -p "$RUNTIME_ROOT"
rsync -a --delete \
  --exclude .codex \
  --exclude .git \
  --exclude .venv \
  --exclude __pycache__ \
  --exclude data \
  --exclude output \
  "$ROOT/" "$RUNTIME_ROOT/"
mkdir -p "$RUNTIME_ROOT/config/local"
printf '%s\n' "$ROOT" > "$RUNTIME_ROOT/config/local/update-source-root.txt"

echo "正在校验运行副本..."
if [[ "$(cat "$RUNTIME_ROOT/config/local/update-source-root.txt")" != "$ROOT" ]]; then
  echo "运行副本的更新来源记录不正确。" >&2
  exit 1
fi
cmp "$ROOT/app.py" "$RUNTIME_ROOT/app.py"
diff -qr --exclude='__pycache__' "$ROOT/src" "$RUNTIME_ROOT/src"
cmp "$ROOT/config/crypto_alerts.default.json" "$RUNTIME_ROOT/config/crypto_alerts.default.json"

echo "正在启动新版 TrendRadar 服务..."
launchctl bootstrap "gui/$USER_ID" "$USER_AGENT"
echo "更新完成：请回到浏览器刷新 http://trend.com。"
