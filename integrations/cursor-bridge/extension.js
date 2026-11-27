const vscode = require('vscode');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const inbox = path.join(os.homedir(), '.agent-panel-cursor-bridge');
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function matches(request, folders, now) {
  if (!request || !uuid.test(request.id) || !uuid.test(request.requestID)
    || typeof request.cwd !== 'string' || !Number.isFinite(request.created)
    || now - request.created >= 30 || now < request.created - 5) return false;
  return path.isAbsolute(request.cwd)
    && folders.some(folder => path.resolve(folder) === path.resolve(request.cwd));
}

function activate(context) {
  let busy = false;
  const poll = async () => {
    if (busy || !vscode.workspace.isTrusted) return;
    busy = true;
    try {
      await fs.mkdir(inbox, {recursive:true, mode:0o700});
      const folders = (vscode.workspace.workspaceFolders || []).filter(f=>f.uri.scheme==='file').map(f=>f.uri.fsPath);
      for (const name of await fs.readdir(inbox)) {
        if (!name.endsWith('.request.json')) continue;
        const file = path.join(inbox, name);
        let request;
        try { request = JSON.parse(await fs.readFile(file, 'utf8')); } catch { continue; }
        if (!request || !uuid.test(request.requestID) || name !== request.requestID+'.request.json') continue;
        if (!matches(request, folders, Date.now()/1000)) continue;
        // Atomic claim prevents two windows for the same folder consuming a request twice.
        const claimed = file+'.claimed';
        try { await fs.rename(file, claimed); } catch { continue; }
        const result = {requestID:request.requestID, id:request.id, cwd:request.cwd, ok:false};
        try {
          const commands = await vscode.commands.getCommands(true);
          if (!commands.includes('composer.openComposer') || !commands.includes('composer.openAsPane')) {
            throw new Error('当前 Cursor 版本未提供会话定位命令');
          }
          await vscode.commands.executeCommand('composer.openComposer', request.id);
          await vscode.commands.executeCommand('composer.openAsPane', request.id);
          result.ok = true;
        } catch(error) { result.error = String(error.message || error); }
        const ack = path.join(inbox, request.requestID+'.result.json');
        await fs.writeFile(ack+'.tmp', JSON.stringify(result), {mode:0o600});
        await fs.rename(ack+'.tmp', ack);
        await fs.unlink(claimed).catch(()=>{});
      }
    } finally { busy = false; }
  };
  const timer = setInterval(()=>poll().catch(()=>{}), 350);
  context.subscriptions.push({dispose:()=>clearInterval(timer)});
  poll().catch(()=>{});
}
module.exports = {activate, matches};
