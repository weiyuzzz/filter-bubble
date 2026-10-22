#!/usr/bin/env bash
set -euo pipefail
AGENT="$HOME/Library/LaunchAgents/com.filterbubble.server.plist"
launchctl bootout "gui/$(id -u)/com.filterbubble.server" >/dev/null 2>&1 || true
rm -f "$AGENT"
echo "✓ 已移除 Filter Bubble LaunchAgent（本地数据仍保留在项目 data/ 目录）"
