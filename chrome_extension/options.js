// ═══════════════════════════════════════════════════════════════
//  Moonwalk — Extension Options Page
//  Configurable bridge URL and auth token (stored in chrome.storage.sync)
// ═══════════════════════════════════════════════════════════════

const DEFAULTS = {
  bridgeUrl: "ws://127.0.0.1:8765",
  bridgeToken: "dev-bridge-token",
};

const bridgeUrlEl = document.getElementById("bridgeUrl");
const bridgeTokenEl = document.getElementById("bridgeToken");
const saveBtn = document.getElementById("saveBtn");
const resetBtn = document.getElementById("resetBtn");
const statusEl = document.getElementById("status");

function showStatus(message, ok = true) {
  statusEl.textContent = message;
  statusEl.className = `status ${ok ? "ok" : "err"}`;
  setTimeout(() => {
    statusEl.className = "status";
  }, 3000);
}

async function loadSettings() {
  const result = await chrome.storage.sync.get(["bridgeUrl", "bridgeToken"]);
  bridgeUrlEl.value = result.bridgeUrl || DEFAULTS.bridgeUrl;
  bridgeTokenEl.value = result.bridgeToken || DEFAULTS.bridgeToken;
}

async function saveSettings() {
  const url = bridgeUrlEl.value.trim();
  const token = bridgeTokenEl.value.trim();

  if (!url) {
    showStatus("Bridge URL is required.", false);
    return;
  }

  try {
    new URL(url);
  } catch {
    showStatus("Invalid URL format. Use ws://host:port", false);
    return;
  }

  await chrome.storage.sync.set({
    bridgeUrl: url,
    bridgeToken: token,
  });

  showStatus("Settings saved! Restart the extension to apply.");

  // Notify background script to reconnect
  chrome.runtime.sendMessage({ type: "moonwalk_settings_updated" });
}

async function resetToDefaults() {
  await chrome.storage.sync.set(DEFAULTS);
  bridgeUrlEl.value = DEFAULTS.bridgeUrl;
  bridgeTokenEl.value = DEFAULTS.bridgeToken;
  showStatus("Reset to defaults. Restart the extension to apply.");
  chrome.runtime.sendMessage({ type: "moonwalk_settings_updated" });
}

saveBtn.addEventListener("click", saveSettings);
resetBtn.addEventListener("click", resetToDefaults);

document.addEventListener("DOMContentLoaded", loadSettings);
