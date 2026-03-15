const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("overlayAPI", {
  hideWindow: () => ipcRenderer.invoke("overlay:hide"),
  enableMouse: () => ipcRenderer.send("enable-mouse"),
  disableMouse: () => ipcRenderer.send("disable-mouse"),
  onStartListening: (handler) => {
    ipcRenderer.on("start-listening", handler);
    return () => ipcRenderer.removeListener("start-listening", handler);
  },
  onOverlayHidden: (handler) => {
    ipcRenderer.on("overlay-hidden", handler);
    return () => ipcRenderer.removeListener("overlay-hidden", handler);
  },
  logError: (msg) => ipcRenderer.send("log-error", msg),
  logInfo: (msg) => ipcRenderer.send("log-info", msg),

  // ── Auth / Credentials ──
  loadCredentials: () => ipcRenderer.invoke("auth:load-credentials"),
  saveCredentials: (creds) => ipcRenderer.invoke("auth:save-credentials", creds),
  generateUserId: () => ipcRenderer.invoke("auth:generate-user-id"),
  clearCredentials: () => ipcRenderer.invoke("auth:clear-credentials"),
  isFirstLaunch: () => ipcRenderer.invoke("auth:is-first-launch"),

  // ── App info ──
  getVersion: () => ipcRenderer.invoke("app:get-version"),
  isPackaged: () => ipcRenderer.invoke("app:is-packaged"),
});
