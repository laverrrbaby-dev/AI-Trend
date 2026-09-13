#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DOMAIN="trend.test"
USER_ID="$(id -u)"
USER_AGENT="$HOME/Library/LaunchAgents/com.trend-radar.app.plist"
SYSTEM_AGENT="/Library/LaunchDaemons/com.trend-radar.caddy.plist"
CADDY_CONFIG="/usr/local/etc/trend-radar/Caddyfile"

if [[ "$(uname)" != "Darwin" ]]; then
  echo "This installer supports macOS only." >&2
  exit 1
fi

if ! command -v brew >/dev/null; then
  echo "Homebrew is required. Install it first from https://brew.sh/." >&2
  exit 1
fi

if ! command -v caddy >/dev/null; then
  brew install caddy
fi
CADDY_BIN="$(command -v caddy)"

echo "Administrator access is required to configure $DOMAIN and the local HTTP proxy."
sudo -v

if ! sudo grep -Eq "^[[:space:]]*127\\.0\\.0\\.1[[:space:]]+$DOMAIN([[:space:]]|$)" /etc/hosts; then
  printf '\n127.0.0.1 %s\n' "$DOMAIN" | sudo tee -a /etc/hosts >/dev/null
fi
sudo dscacheutil -flushcache

mkdir -p "$HOME/Library/LaunchAgents"
sed "s|__PROJECT_ROOT__|$ROOT|g" "$ROOT/config/local/com.trend-radar.app.plist.template" > "$USER_AGENT"
plutil -lint "$USER_AGENT"

sudo install -d -m 755 /usr/local/etc/trend-radar
sudo install -m 644 "$ROOT/config/local/Caddyfile" "$CADDY_CONFIG"
sed "s|__CADDY_BIN__|$CADDY_BIN|g" "$ROOT/config/local/com.trend-radar.caddy.plist.template" | sudo tee "$SYSTEM_AGENT" >/dev/null
sudo plutil -lint "$SYSTEM_AGENT"

launchctl bootout "gui/$USER_ID" "$USER_AGENT" 2>/dev/null || true
launchctl bootstrap "gui/$USER_ID" "$USER_AGENT"
sudo launchctl bootout system "$SYSTEM_AGENT" 2>/dev/null || true
sudo launchctl bootstrap system "$SYSTEM_AGENT"

echo "Ready: http://$DOMAIN"
