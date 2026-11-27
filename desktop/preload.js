const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("agentPanel", {
  platform: process.platform,
  getSettings: () => ipcRenderer.invoke("panel:settings-get"),
  setSettings: (values) => ipcRenderer.invoke("panel:settings-set", values),
  notify: (payload) => ipcRenderer.invoke("panel:notify", payload),
  openExternal: (url) => ipcRenderer.invoke("panel:open-external", url),
  windowAction: (action) => ipcRenderer.invoke("panel:window", action),
});
