#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_ROOT="$HOME/Library/Application Support/TrendRadar"
PYTHON_BIN="$(command -v python3)"
DOMAINS=("trend.com" "trend.test")
USER_ID="$(id -u)"
USER_AGENT="$HOME/Library/LaunchAgents/com.trend-radar.app.plist"
SYSTEM_AGENT="/Library/LaunchDaemons/com.trend-radar.caddy.plist"
CADDY_CONFIG="/usr/local/etc/trend-radar/Caddyfile"

if [[ "$(uname)" != "Darwin" ]]; then
  echo "This installer supports macOS only." >&2
  exit 1
fi

if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
  echo "Python 3.11 or newer is required; found: $PYTHON_BIN" >&2
  exit 1
fi

launchctl bootout "gui/$USER_ID" "$USER_AGENT" 2>/dev/null || true
runtime_streamlit_pids="$(pgrep -f "$RUNTIME_ROOT/.venv/bin/python.*streamlit run app.py" || true)"
if [[ -n "$runtime_streamlit_pids" ]]; then
  echo "Stopping the previous runtime Streamlit process."
  kill $runtime_streamlit_pids
fi

if ! command -v brew >/dev/null; then
  echo "Homebrew is required. Install it first from https://brew.sh/." >&2
  exit 1
fi

if ! command -v caddy >/dev/null; then
  brew install caddy
fi
CADDY_BIN="$(command -v caddy)"

echo "Administrator access is required to configure ${DOMAINS[*]} and the local HTTP proxy."
sudo -v

for domain in "${DOMAINS[@]}"; do
  if ! sudo grep -Eq "^[[:space:]]*127\\.0\\.0\\.1[[:space:]]+$domain([[:space:]]|$)" /etc/hosts; then
    printf '\n127.0.0.1 %s\n' "$domain" | sudo tee -a /etc/hosts >/dev/null
  fi
done
sudo dscacheutil -flushcache

mkdir -p "$RUNTIME_ROOT"
rsync -a --delete \
  --exclude .codex \
  --exclude .git \
  --exclude .venv \
  --exclude __pycache__ \
  --exclude data \
  --exclude output \
  "$ROOT/" "$RUNTIME_ROOT/"
if [[ -d "$ROOT/data" ]]; then
  rsync -a "$ROOT/data/" "$RUNTIME_ROOT/data/"
fi
if [[ -x "$RUNTIME_ROOT/.venv/bin/python" ]] && ! "$RUNTIME_ROOT/.venv/bin/python" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
  echo "Removing incompatible runtime virtual environment."
  rm -rf "$RUNTIME_ROOT/.venv"
fi

mkdir -p "$HOME/Library/LaunchAgents"
sed \
  -e "s|__PROJECT_ROOT__|$RUNTIME_ROOT|g" \
  -e "s|__PYTHON_BIN__|$PYTHON_BIN|g" \
  "$ROOT/config/local/com.trend-radar.app.plist.template" > "$USER_AGENT"
plutil -lint "$USER_AGENT"

sudo install -d -m 755 /usr/local/etc/trend-radar
sudo install -m 644 "$ROOT/config/local/Caddyfile" "$CADDY_CONFIG"
sed "s|__CADDY_BIN__|$CADDY_BIN|g" "$ROOT/config/local/com.trend-radar.caddy.plist.template" | sudo tee "$SYSTEM_AGENT" >/dev/null
sudo plutil -lint "$SYSTEM_AGENT"

launchctl bootstrap "gui/$USER_ID" "$USER_AGENT"
sudo launchctl bootout system "$SYSTEM_AGENT" 2>/dev/null || true
sudo launchctl bootstrap system "$SYSTEM_AGENT"

echo "Ready: http://trend.com and http://trend.test"
echo "Runtime copy: $RUNTIME_ROOT"
