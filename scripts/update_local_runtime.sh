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
listener_pids="$(lsof -nP -tiTCP:8501 -sTCP:LISTEN 2>/dev/null || true)"
for runtime_pid in $listener_pids; do
  runtime_cwd="$(lsof -a -p "$runtime_pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')"
  runtime_command="$(ps -p "$runtime_pid" -o command= 2>/dev/null || true)"
  if [[ "$runtime_cwd" == "$RUNTIME_ROOT" && "$runtime_command" == *"streamlit run app.py"* ]]; then
    kill "$runtime_pid"
    for ((attempt = 0; attempt < 30; attempt++)); do
      if ! kill -0 "$runtime_pid" 2>/dev/null; then
        break
      fi
      sleep 0.2
    done
    if kill -0 "$runtime_pid" 2>/dev/null; then
      echo "旧 Streamlit 进程未退出，已停止部署，避免继续使用旧代码。" >&2
      exit 1
    fi
  fi
done

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
echo "更新完成：请回到浏览器刷新 http://trend.test。"
