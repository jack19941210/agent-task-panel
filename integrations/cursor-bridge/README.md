# Cursor IDE conversation bridge

For a chat linked to a workspace, the macOS panel sends a short-lived local request
with the workspace path and composer ID. The bridge accepts it only in the matching
trusted project. It invokes `composer.openComposer(id)` and `composer.openAsPane(id)`
and returns an acknowledgment. It never sends a prompt or writes Cursor's conversation
database. Chats without a workspace use Cursor's built-in deep link to open the
conversation in Agents and do not use the bridge.

macOS releases bundle the VSIX inside the app. On first use, the panel checks the
installed version and installs/upgrades it through Cursor's CLI without contacting
the extension marketplace. An equal or newer version is retained. Disabled extensions
and untrusted workspaces are not force-enabled. Installation does not restart Cursor.

For development only, manual installation is also available:

```sh
python3 integrations/cursor-bridge/package.py
cursor --install-extension integrations/cursor-bridge/agent-panel-bridge.vsix --force
```

New installations normally activate automatically. If no acknowledgment arrives,
check that the extension is enabled and the workspace trusted; reloading that workspace
may be needed. The panel reports a timeout rather than pretending the conversation opened.

Verified against Cursor 3.27.14 on macOS. These are internal Cursor commands and may
change. Remote workspaces and Windows/Linux panel integration are not covered.

The queue is `~/.agent-panel-cursor-bridge` (owner-only directory). Requests expire
in 30 seconds; the panel waits 20 seconds. Command execution success confirms the
commands returned, not the visible state; verify Cursor selection separately when testing.

Validation: `node integrations/cursor-bridge/test.cjs`.

Installer regression tests: `scripts/test-cursor-installer.sh`.
