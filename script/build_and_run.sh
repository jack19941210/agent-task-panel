#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
# Build and validate before stopping the running utility.
ARCHS="${ARCHS:-arm64}" zsh scripts/build.sh
codesign --verify --deep --strict AgentTaskPanel.app
pkill -x AgentTaskPanel || true
if [[ -d /Applications/AgentTaskPanel.app ]]; then
  backup="/private/tmp/AgentTaskPanel-backup-$(date +%Y%m%d-%H%M%S).app"
  ditto /Applications/AgentTaskPanel.app "$backup"
fi
ditto AgentTaskPanel.app /Applications/AgentTaskPanel.app
open /Applications/AgentTaskPanel.app
if [[ "${1:-}" == "--verify" ]]; then
  sleep 2
  pgrep -x AgentTaskPanel
fi
