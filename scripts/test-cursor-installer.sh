#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
task_tmp="$(mktemp -d)"
trap 'rm -rf "$task_tmp"' EXIT
mkdir -p "$task_tmp/resources"
cp tests/cursor_bridge_installer.swift "$task_tmp/main.swift"
cp integrations/cursor-bridge/package.json "$task_tmp/resources/cursor-bridge-package.json"
touch "$task_tmp/resources/agent-panel-bridge.vsix"
cat > "$task_tmp/cursor" <<'CLI'
#!/bin/bash
cd "$(dirname "$0")"
echo "$*" >> calls
if [[ "$1" == "--list-extensions" ]]; then
  if [[ -f version ]]; then printf 'agent-task-panel.agent-panel-bridge@%s\n' "$(cat version)"; fi
elif [[ "$1" == "--install-extension" ]]; then
  test -f "$2" || exit 1
  python3 -c 'import json; print(json.load(open("resources/cursor-bridge-package.json"))["version"])' > version
else exit 1
fi
CLI
chmod +x "$task_tmp/cursor"
swiftc src/CursorBridgeInstaller.swift "$task_tmp/main.swift" -o "$task_tmp/check"
"$task_tmp/check" "$task_tmp"
