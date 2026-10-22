#!/usr/bin/env bash
set -euo pipefail
AGENT="$HOME/Library/LaunchAgents/com.gitpulse.server.plist"
launchctl bootout "gui/$(id -u)/com.gitpulse.server" >/dev/null 2>&1 || true
rm -f "$AGENT"
echo "✓ 已移除 GitPulse LaunchAgent（本地数据仍保留在项目 data/ 目录）"
