#!/usr/bin/env bash
# Install Filter Bubble as a macOS LaunchAgent.
# It starts at login, keeps the local UI alive, and refreshes at 08:00 every day.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  echo "✗ 找不到 python3，请先安装 Python 3.9+"
  exit 1
fi
AGENT="$HOME/Library/LaunchAgents/com.filterbubble.server.plist"
LOG_DIR="$DIR/data"
mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents"

if lsof -nP -iTCP:8791 -sTCP:LISTEN >/dev/null 2>&1; then
  PID="$(lsof -tiTCP:8791 -sTCP:LISTEN | head -n 1)"
  CMD="$(ps -p "$PID" -o command= 2>/dev/null || true)"
  if [[ "$CMD" == *"app.py serve"* ]]; then
    echo "检测到手动启动的 Filter Bubble（PID $PID），交给 LaunchAgent 接管…"
    kill "$PID" 2>/dev/null || true
    sleep 1
  else
    echo "⚠️  端口 8791 被其他程序占用（PID $PID）：$CMD"
    echo "   请先停止该程序，再执行本脚本。"
    exit 1
  fi
fi

cat > "$AGENT" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.filterbubble.server</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$DIR/app.py</string>
    <string>serve</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPYCACHEPREFIX</key><string>$DIR/.pycache</string>
    <key>GITPULSE_REFRESH_TIME</key><string>08:00</string>
    <key>GITPULSE_TIMEZONE</key><string>Asia/Shanghai</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>StandardOutPath</key><string>$LOG_DIR/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/launchd.err.log</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)/com.filterbubble.server" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$AGENT"
launchctl enable "gui/$(id -u)/com.filterbubble.server" 2>/dev/null || true

echo "✓ 已安装 LaunchAgent：$AGENT"
echo "  UI：      http://127.0.0.1:8791"
echo "  自动刷新：每天 08:00（Asia/Shanghai，可在 plist 里改）"
echo "  日志：    $LOG_DIR/launchd.err.log"
echo
echo "如需卸载：$DIR/scripts/uninstall-daily.sh"
