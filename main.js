const path = require("node:path");
const fs = require("node:fs");
const net = require("node:net");
const crypto = require("node:crypto");
const { spawn } = require("node:child_process");
const {
  app,
  BrowserWindow,
  globalShortcut,
  ipcMain,
  screen,
  session,
  systemPreferences,
  safeStorage,
} = require("electron");

const HOTKEYS = (process.env.LIQUID_HOTKEY || "CommandOrControl+Shift+Space,Alt+Space")
  .split(",")
  .map((value) => value.trim())
  .filter(Boolean);

const WINDOW_LEVEL = "screen-saver";

// ── Path resolution (dev vs packaged) ──
const IS_PACKAGED = app.isPackaged;
const APP_ROOT = IS_PACKAGED
  ? path.join(process.resourcesPath)
  : __dirname;
const BACKEND_ROOT = IS_PACKAGED
  ? path.join(APP_ROOT, "backend")
  : path.join(__dirname, "backend");

let mainWindow;
let lastWakeAt = 0;
let pythonProcess = null;
let ownsPythonProcess = false;

const BACKEND_WS_URL = process.env.MOONWALK_BACKEND_WS_URL || "ws://127.0.0.1:8000/ws";
const BACKEND_HOST = process.env.MOONWALK_BACKEND_HOST || "127.0.0.1";
const BACKEND_PORT = Number(process.env.MOONWALK_BACKEND_PORT || "8000");
const BRIDGE_PORT = Number(process.env.MOONWALK_BROWSER_BRIDGE_PORT || "8765");
const BACKEND_READY_SENTINEL = "[Backend] READY";

// ── Credential storage ──
const CRED_FILE = path.join(app.getPath("userData"), "credentials.enc");

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function canReachPort(port, timeoutMs = 1500) {
  return new Promise((resolve) => {
    let settled = false;
    const socket = net.createConnection({ host: BACKEND_HOST, port });

    const finish = (result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try {
        socket.destroy();
      } catch {
      }
      resolve(result);
    };

    const timer = setTimeout(() => finish(false), timeoutMs);

    socket.once("connect", () => finish(true));
    socket.once("error", () => finish(false));
  });
}

async function canReachBackend(timeoutMs = 1500) {
  const [backendReady, bridgeReady] = await Promise.all([
    canReachPort(BACKEND_PORT, timeoutMs),
    canReachPort(BRIDGE_PORT, timeoutMs)
  ]);
  return backendReady && bridgeReady;
}

async function startPythonBackend() {
  // In packaged mode, venv lives next to the app; in dev mode, in project root
  const venvPythonPath = IS_PACKAGED
    ? path.join(APP_ROOT, "venv", "bin", "python3")
    : path.join(__dirname, "venv", "bin", "python3");
  const scriptPath = path.join(BACKEND_ROOT, "servers", "local_server.py");
  const cwd = IS_PACKAGED ? APP_ROOT : __dirname;

  if (!fs.existsSync(venvPythonPath)) {
    console.error(`[Backend] Python executable not found at: ${venvPythonPath}`);
    console.error("[Backend] Please run: ./setup.sh");
    return false;
  }

  if (await canReachBackend()) {
    console.log(`[Backend] Reusing existing backend at ${BACKEND_WS_URL}`);
    ownsPythonProcess = false;
    return true;
  }

  console.log("[Backend] Starting Python server...");

  // Start the python process
  pythonProcess = spawn(venvPythonPath, [scriptPath], {
    cwd: cwd,
    stdio: ['ignore', 'pipe', 'pipe']
  });
  ownsPythonProcess = true;

  let sawAddressInUse = false;
  let readinessBuffer = "";
  let resolveReady;
  const readyPromise = new Promise((resolve) => {
    resolveReady = resolve;
  });

  // Pipe python stdout/stderr to our electron console
  pythonProcess.stdout.on('data', (data) => {
    const text = data.toString();
    readinessBuffer = (readinessBuffer + text).slice(-4096);
    if (readinessBuffer.includes(BACKEND_READY_SENTINEL)) {
      resolveReady(true);
    }
    process.stdout.write(`[Python] ${text}`);
  });

  pythonProcess.stderr.on('data', (data) => {
    const text = data.toString();
    if (text.includes("Errno 48") || text.includes("address already in use")) {
      sawAddressInUse = true;
    }
    process.stderr.write(`[Python ERRROR] ${text}`);
  });

  pythonProcess.on('close', (code) => {
    resolveReady(false);
    if (sawAddressInUse) {
      console.log("[Backend] Python server did not start because port 8000 is already in use.");
    } else {
      console.log(`[Backend] Python server exited with code ${code}`);
    }
    pythonProcess = null;
    ownsPythonProcess = false;
  });

  const ready = await Promise.race([
    readyPromise,
    sleep(10000).then(() => false)
  ]);
  if (ready) {
    return true;
  }

  if (await canReachBackend()) {
    return true;
  }

  if (sawAddressInUse && await canReachBackend()) {
    console.log(`[Backend] Reusing backend that is already listening at ${BACKEND_WS_URL}`);
    return true;
  }

  console.error("[Backend] Backend did not become ready in time.");
  return false;
}

function stopPythonBackend() {
  if (pythonProcess && ownsPythonProcess) {
    console.log("[Backend] Stopping Python server...");
    pythonProcess.kill('SIGTERM');
    pythonProcess = null;
    ownsPythonProcess = false;
  }
}

function emitStartListening() {
  if (!mainWindow || mainWindow.isDestroyed()) return;

  if (mainWindow.webContents.isLoading()) {
    mainWindow.webContents.once("did-finish-load", () => {
      if (!mainWindow || mainWindow.isDestroyed()) return;
      mainWindow.webContents.send("start-listening");
    });
    return;
  }

  mainWindow.webContents.send("start-listening");
}

function createWindow() {
  const display = screen.getPrimaryDisplay();
  const { width, height } = display.workAreaSize;

  mainWindow = new BrowserWindow({
    width: width,
    height: height,
    x: display.workArea.x,
    y: display.workArea.y,
    show: true,
    frame: false,
    transparent: true,
    resizable: false,
    movable: false,
    hasShadow: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    backgroundColor: "#00000000",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  mainWindow.setAlwaysOnTop(true, WINDOW_LEVEL);
  mainWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  mainWindow.setFullScreenable(false);
  setMousePassthrough(true);

  mainWindow.loadFile(path.join(__dirname, "renderer", "index.html"));
}

function centerNearTop() {
  if (!mainWindow) return;
  const display = screen.getPrimaryDisplay();
  const x = Math.round(display.workArea.x + (display.workArea.width - WINDOW_WIDTH) / 2);
  const y = Math.max(display.workArea.y + 10, 8);
  mainWindow.setPosition(x, y, false);
}

function wakeOverlay() {
  if (!mainWindow) return;
  lastWakeAt = Date.now();
  mainWindow.show();
  emitStartListening();
}

function hideOverlay() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  setMousePassthrough(true);
  mainWindow.webContents.send("overlay-hidden");
}

function registerHotkey() {
  globalShortcut.unregisterAll();
  let registeredCount = 0;

  for (const accelerator of HOTKEYS) {
    const ok = globalShortcut.register(accelerator, () => {
      wakeOverlay();
    });
    if (ok) {
      registeredCount += 1;
    } else {
      console.error(`Failed to register global shortcut: ${accelerator}`);
    }
  }

  if (registeredCount === 0) {
    console.error("No usable global shortcuts were registered.");
  }
}

function setMousePassthrough(ignore) {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  if (ignore) {
    mainWindow.setIgnoreMouseEvents(true, { forward: true });
    return;
  }
  mainWindow.setIgnoreMouseEvents(false);
}

async function configureMicrophonePermissions() {
  session.defaultSession.setPermissionCheckHandler((_, permission) => {
    if (permission === "media" || permission === "microphone") {
      return true;
    }
    return false;
  });

  session.defaultSession.setPermissionRequestHandler((_, permission, callback) => {
    if (permission === "media" || permission === "microphone") {
      callback(true);
      return;
    }
    callback(false);
  });

  if (process.platform === "darwin") {
    const status = systemPreferences.getMediaAccessStatus("microphone");
    if (status !== "granted") {
      try {
        await systemPreferences.askForMediaAccess("microphone");
      } catch (err) {
        console.error("Microphone permission prompt failed:", err);
      }
    }
  }
}

app.whenReady().then(async () => {
  await configureMicrophonePermissions();

  // Start backend before creating the window
  await startPythonBackend();

  createWindow();
  registerHotkey();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
      registerHotkey();
    }
  });
});

ipcMain.handle("overlay:hide", () => {
  hideOverlay();
});

ipcMain.on("enable-mouse", () => {
  setMousePassthrough(false);
});

ipcMain.on("disable-mouse", () => {
  setMousePassthrough(true);
});

ipcMain.on("log-error", (event, msg) => {
  console.error(`[Renderer WS Error] ${msg}`);
});

ipcMain.on("log-info", (event, msg) => {
  console.log(`[Renderer Info] ${msg}`);
});

// ═══════════════════════════════════════════════════════════════
//  Credential Storage (safeStorage-encrypted)
// ═══════════════════════════════════════════════════════════════

function loadCredentials() {
  try {
    if (!fs.existsSync(CRED_FILE)) return null;
    const encrypted = fs.readFileSync(CRED_FILE);
    if (!safeStorage.isEncryptionAvailable()) {
      console.warn("[Auth] safeStorage encryption not available");
      return null;
    }
    const decrypted = safeStorage.decryptString(encrypted);
    return JSON.parse(decrypted);
  } catch (err) {
    console.error("[Auth] Failed to load credentials:", err.message);
    return null;
  }
}

function saveCredentials(creds) {
  try {
    if (!safeStorage.isEncryptionAvailable()) {
      console.warn("[Auth] safeStorage encryption not available");
      return false;
    }
    const encrypted = safeStorage.encryptString(JSON.stringify(creds));
    fs.writeFileSync(CRED_FILE, encrypted);
    return true;
  } catch (err) {
    console.error("[Auth] Failed to save credentials:", err.message);
    return false;
  }
}

ipcMain.handle("auth:load-credentials", () => {
  return loadCredentials();
});

ipcMain.handle("auth:save-credentials", (event, creds) => {
  return saveCredentials(creds);
});

ipcMain.handle("auth:generate-user-id", () => {
  return {
    user_id: crypto.randomUUID(),
    auth_token: crypto.randomBytes(36).toString("base64url"),
  };
});

ipcMain.handle("auth:clear-credentials", () => {
  try {
    if (fs.existsSync(CRED_FILE)) fs.unlinkSync(CRED_FILE);
    return true;
  } catch { return false; }
});

ipcMain.handle("auth:is-first-launch", () => {
  return !fs.existsSync(CRED_FILE);
});

ipcMain.handle("app:get-version", () => {
  return app.getVersion();
});

ipcMain.handle("app:is-packaged", () => {
  return IS_PACKAGED;
});

app.on("will-quit", () => {
  globalShortcut.unregisterAll();
  stopPythonBackend();
});
