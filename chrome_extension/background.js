// ═══════════════════════════════════════════════════════════════
//  Moonwalk — Background Service Worker
//  Manages WebSocket bridge to the Moonwalk backend, action
//  polling, snapshot forwarding, and keepalive pings.
// ═══════════════════════════════════════════════════════════════

const BRIDGE_URL = "ws://127.0.0.1:8765";
const BRIDGE_TOKEN = "dev-bridge-token";
const EXTENSION_NAME = "moonwalk-browser-bridge";

const ACTION_POLL_INTERVAL_MS = 300;   // ← was 1000 ms
const KEEPALIVE_INTERVAL_MS = 15000;   // ping every 15 s
const RECONNECT_DELAY_MS = 1500;

let bridgeSocket = null;
let authenticated = false;
let sessionId = `chrome-session-${Date.now()}`;
let reconnectTimer = null;
let latestSnapshotByTab = new Map();
let lastBridgeError = "";
let lastBridgeMessage = "Not connected yet";
let actionPollTimer = null;
let keepaliveTimer = null;
let actionExecutionInFlight = false;

// ── Utility ──

function isInjectableUrl(url) {
  if (!url || typeof url !== "string") return false;
  return /^(https?|file):/i.test(url);
}

async function ensureContentScript(tabId) {
  await chrome.scripting.executeScript({
    target: { tabId },
    files: ["Readability.js", "content_script.js"],
  });
}

function log(...args) {
  console.log("[Moonwalk Bridge]", ...args);
}

// ── Keepalive ──

function startKeepalive() {
  if (keepaliveTimer) return;
  keepaliveTimer = setInterval(() => {
    if (bridgeSocket && bridgeSocket.readyState === WebSocket.OPEN && authenticated) {
      bridgeSocket.send(JSON.stringify({ type: "browser_ping" }));
    }
  }, KEEPALIVE_INTERVAL_MS);
}

function stopKeepalive() {
  if (keepaliveTimer) {
    clearInterval(keepaliveTimer);
    keepaliveTimer = null;
  }
}

// ── Action Polling ──

function stopActionPolling() {
  if (actionPollTimer) {
    clearInterval(actionPollTimer);
    actionPollTimer = null;
  }
}

function startActionPolling() {
  if (actionPollTimer) return;
  actionPollTimer = setInterval(() => {
    pollBridgeActions().catch((error) => {
      lastBridgeError = String(error?.message || error);
      lastBridgeMessage = "Action polling failed";
      log("Action polling failed", error);
    });
  }, ACTION_POLL_INTERVAL_MS);
}

async function pollBridgeActions() {
  if (!bridgeSocket || bridgeSocket.readyState !== WebSocket.OPEN || !authenticated || actionExecutionInFlight) {
    return;
  }
  bridgeSocket.send(
    JSON.stringify({
      type: "browser_poll_actions",
      session_id: sessionId,
    }),
  );
}

// ── Action Execution ──

async function executeActionOnTab(action) {
  const metadata = action?.metadata || {};
  const targetTabId = Number(metadata.tab_id || latestSnapshotByTab.keys().next().value || 0);
  const tab = targetTabId ? await chrome.tabs.get(targetTabId).catch(() => null) : await getActiveTab();

  if (!tab?.id) {
    return buildActionResult(action, false, "No browser tab available for action execution.", metadata, { reason: "missing-tab" });
  }

  if (!isInjectableUrl(tab.url)) {
    return buildActionResult(action, false, `Cannot execute browser action on ${tab.url || "this page"}.`, metadata, {
      reason: "unsupported-url",
      tab_id: String(tab.id),
    });
  }

  try {
    await ensureContentScript(tab.id);

    if (action?.action === "refresh_snapshot") {
      const refreshed = await requestSnapshotFromTab(tab.id, action?.session_id || sessionId);
      return buildActionResult(
        action,
        !!refreshed?.ok,
        refreshed?.ok ? "Browser snapshot refreshed." : "Browser snapshot refresh failed.",
        metadata,
        { tab_id: String(tab.id), refreshed: String(!!refreshed?.ok) },
        Date.now(),
      );
    }

    // ── Scroll action: relay to content script via moonwalk_scroll ──
    if (action?.action === "scroll") {
      const scrollResult = await chrome.tabs.sendMessage(tab.id, {
        type: "moonwalk_scroll",
        direction: metadata.direction || "down",
        amount: metadata.amount || "page",
        sessionId: action?.session_id || sessionId,
        tabId: String(tab.id),
      });
      return buildActionResult(
        action,
        !!scrollResult?.ok,
        scrollResult?.ok
          ? `Scrolled ${metadata.direction || "down"} by ${metadata.amount || "page"}.`
          : "Scroll action failed.",
        metadata,
        {
          tab_id: String(tab.id),
          scrollY: String(scrollResult?.scrollY || 0),
          pageHeight: String(scrollResult?.pageHeight || 0),
          atBottom: String(!!scrollResult?.atBottom),
          atTop: String(!!scrollResult?.atTop),
        },
        Date.now(),
      );
    }

    // ── Highlight action: visually mark elements the agent is reading ──
    if (action?.action === "highlight") {
      const highlightResult = await chrome.tabs.sendMessage(tab.id, {
        type: "moonwalk_highlight",
        agentIds: metadata.agent_ids || [],
        duration: Number(metadata.duration || 3000),
        mode: metadata.mode || "reading",
        tool: metadata.tool || "",
        title: metadata.title || "",
        sourceUrl: metadata.source_url || "",
        snippet: metadata.snippet || "",
        itemCount: Number(metadata.item_count || 0),
      });
      return buildActionResult(
        action,
        !!highlightResult?.ok,
        highlightResult?.ok
          ? `Highlighted ${highlightResult.highlighted || 0} elements.`
          : "Highlight action failed.",
        metadata,
        {
          tab_id: String(tab.id),
          highlighted: String(highlightResult?.highlighted || 0),
          overlay_visible: String(!!highlightResult?.overlayVisible),
        },
        Date.now(),
      );
    }

    // ── Evaluate JS action: securely evaluate JS directly on the page ──
    if (action?.action === "evaluate_js") {
      const evalResult = await chrome.tabs.sendMessage(tab.id, {
        type: "moonwalk_evaluate_js",
        script: action?.text || "",
        sessionId: action?.session_id || sessionId,
        tabId: String(tab.id),
      });
      return buildActionResult(
        action,
        !!evalResult?.ok,
        evalResult?.ok ? "JS evaluation successful." : "JS evaluation failed.",
        metadata,
        {
          tab_id: String(tab.id),
          result: evalResult?.result || "",
          error: evalResult?.error || "",
        },
        Date.now(),
      );
    }

    // ── Extract Data action: safely extract specific DOM strings without eval() ──
    if (action?.action === "extract_data") {
      const evalResult = await chrome.tabs.sendMessage(tab.id, {
        type: "moonwalk_extract_data",
        target: action?.text || "",
        sessionId: action?.session_id || sessionId,
        tabId: String(tab.id),
      });
      return buildActionResult(
        action,
        !!evalResult?.ok,
        evalResult?.ok ? "Data extraction successful." : "Data extraction failed.",
        metadata,
        {
          tab_id: String(tab.id),
          result: evalResult?.result || "",
          error: evalResult?.error || "",
        },
        Date.now(),
      );
    }

    if (action?.action === "extract_readability") {
      const readabilityResult = await chrome.tabs.sendMessage(tab.id, {
        type: "moonwalk_extract_readability",
        sessionId: action?.session_id || sessionId,
        tabId: String(tab.id),
      });
      return buildActionResult(
        action,
        !!readabilityResult?.ok,
        readabilityResult?.message || (readabilityResult?.ok ? "Readability extraction successful." : "Readability extraction failed."),
        metadata,
        {
          tab_id: String(tab.id),
          result: JSON.stringify(readabilityResult || {}),
          error: readabilityResult?.error || "",
        },
        Date.now(),
      );
    }

    // ── Click pointer: show agent cursor before click actions ──
    const isClickLike = ["click", "type", "fill", "select"].includes(action?.action);
    if (isClickLike && action?.ref_id) {
      try {
        const snapshot = latestSnapshotByTab.get(tab.id) || [...latestSnapshotByTab.values()].slice(-1)[0];
        if (snapshot?.elements) {
          const el = snapshot.elements.find(e => e.ref_id === action.ref_id);
          const b = el?.bounds;
          if (b && (b.width > 0 || b.height > 0)) {
            const pageX = (b.x || 0) + (b.width || 0) / 2;
            const pageY = (b.y || 0) + (b.height || 0) / 2;
            chrome.tabs.sendMessage(tab.id, {
              type: "moonwalk_show_click_pointer",
              pageX,
              pageY,
            }).catch(() => {});
          }
        }
      } catch (_) {}
    }

    const result = await chrome.tabs.sendMessage(tab.id, {
      type: "moonwalk_execute_action",
      action,
      sessionId: action?.session_id || sessionId,
      tabId: String(tab.id),
    });

    // After a successful click, fire the burst animation
    if (result?.ok && isClickLike) {
      chrome.tabs.sendMessage(tab.id, { type: "moonwalk_trigger_click_burst" }).catch(() => {});
    }

    // After a successful action, request a fresh snapshot so the backend
    // gets the post-action DOM state quickly. The content script's
    // MutationObserver also fires, but an explicit request is faster for
    // the very next agent iteration.
    if (result?.ok) {
      requestSnapshotFromTab(tab.id, action?.session_id || sessionId).catch(() => {});
    }

    return buildActionResult(
      action,
      !!result?.ok,
      result?.message || (result?.ok ? "Browser action executed." : "Browser action failed."),
      metadata,
      {
        tab_id: String(tab.id),
        executed_ref_id: result?.executedRefId || action?.ref_id || "",
        matched_by: result?.matchedBy || "",
      },
      Date.now(),
    );
  } catch (error) {
    return buildActionResult(action, false, String(error?.message || error), metadata, {
      reason: "execution-error",
      tab_id: String(tab?.id || ""),
    });
  }
}

function buildActionResult(action, ok, message, metadata, details, postGen) {
  const errorCode = ok ? "" : String(details?.error_code || details?.reason || "bridge.action_failed");
  return {
    ok,
    message,
    action: action?.action || "",
    ref_id: action?.ref_id || "",
    action_id: action?.action_id || "",
    session_id: action?.session_id || sessionId,
    pre_generation: Number(metadata?.generation || 0),
    post_generation: postGen ?? Number(metadata?.generation || 0),
    details: details || {},
    error: ok ? null : {
      code: errorCode,
      message,
      retryable: false,
      degraded: false,
      source: "bridge.background",
      details: details || {},
    },
    meta: {
      session_id: action?.session_id || sessionId,
      provenance: "chrome_extension",
    },
  };
}

async function processBridgeActions(actions) {
  if (!Array.isArray(actions) || actions.length === 0 || actionExecutionInFlight) {
    return;
  }

  actionExecutionInFlight = true;
  try {
    for (const action of actions) {
      lastBridgeMessage =
        action.action === "refresh_snapshot"
          ? "Refreshing browser snapshot"
          : `Executing ${action.action} on ${action.ref_id}`;
      const result = await executeActionOnTab(action);
      sendToBridge({ type: "browser_action_result", result });
      lastBridgeMessage = result.ok ? `Executed ${action.action} on ${action.ref_id}` : `Action failed for ${action.ref_id}`;
      if (!result.ok) {
        lastBridgeError = result.message;
      }
    }
  } finally {
    actionExecutionInFlight = false;
  }
}

// ── Snapshot Requests ──

async function requestSnapshotFromTab(tabId, sessionIdOverride = sessionId) {
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  if (!tab?.id) {
    lastBridgeError = "No active tab available for snapshot";
    lastBridgeMessage = "Snapshot request skipped";
    return { ok: false, reason: "missing-tab" };
  }

  if (!isInjectableUrl(tab.url)) {
    lastBridgeError = `Snapshots are not supported on ${tab.url || "this page"}`;
    lastBridgeMessage = "Active page blocks content injection";
    return { ok: false, reason: "unsupported-url", url: tab.url || "" };
  }

  const payload = {
    type: "moonwalk_collect_snapshot",
    sessionId: sessionIdOverride,
    tabId: String(tab.id),
  };

  try {
    const response = await chrome.tabs.sendMessage(tab.id, payload);
    lastBridgeError = "";
    lastBridgeMessage = "Snapshot request delivered";
    return { ok: !!response?.ok, injected: false };
  } catch (error) {
    const message = String(error?.message || error);
    if (!/Receiving end does not exist/i.test(message)) {
      lastBridgeError = message;
      lastBridgeMessage = "Snapshot request failed";
      return { ok: false, reason: "send-failed", error: message };
    }
  }

  // Content script not yet injected — inject and retry
  try {
    await ensureContentScript(tab.id);
    const response = await chrome.tabs.sendMessage(tab.id, payload);
    lastBridgeError = "";
    lastBridgeMessage = "Content script injected; snapshot requested";
    return { ok: !!response?.ok, injected: true };
  } catch (error) {
    const message = String(error?.message || error);
    lastBridgeError = message;
    lastBridgeMessage = "Content script injection failed";
    return { ok: false, reason: "inject-failed", error: message };
  }
}

// ── Bridge Connection ──

function bridgeStatus() {
  const latestSnapshot = [...latestSnapshotByTab.values()].slice(-1)[0] || null;
  return {
    authenticated,
    sessionId,
    bridgeUrl: BRIDGE_URL,
    extensionName: EXTENSION_NAME,
    socketState: bridgeSocket ? bridgeSocket.readyState : WebSocket.CLOSED,
    lastBridgeError,
    lastBridgeMessage,
    latestSnapshot,
    snapshotCount: latestSnapshotByTab.size,
  };
}

function connectBridge() {
  if (bridgeSocket && (bridgeSocket.readyState === WebSocket.OPEN || bridgeSocket.readyState === WebSocket.CONNECTING)) {
    return;
  }

  bridgeSocket = new WebSocket(BRIDGE_URL);

  bridgeSocket.addEventListener("open", () => {
    authenticated = false;
    lastBridgeError = "";
    lastBridgeMessage = "Socket open; sending hello";
    log("Connected to backend bridge");
    bridgeSocket.send(
      JSON.stringify({
        type: "browser_bridge_hello",
        token: BRIDGE_TOKEN,
        session_id: sessionId,
        extension_name: EXTENSION_NAME,
      }),
    );
  });

  bridgeSocket.addEventListener("message", async (event) => {
    try {
      const message = JSON.parse(event.data);

      if (message.type === "browser_bridge_hello_ack") {
        authenticated = !!message.ok;
        lastBridgeMessage = message.ok ? "Handshake accepted" : `Handshake rejected: ${message.message || "unknown error"}`;
        log("Handshake ack", message);
        if (authenticated) {
          startActionPolling();
          startKeepalive();
          await pushActiveTabSnapshot();
          await pollBridgeActions();
        } else {
          lastBridgeError = message.message || "Handshake rejected";
          try {
            bridgeSocket?.close();
          } catch (_) {}
          scheduleReconnect();
        }
        return;
      }

      if (message.type === "browser_snapshot_ack") {
        lastBridgeMessage = `Snapshot accepted (${message.elements || 0} elements)`;
        return;
      }

      if (message.type === "browser_action_result_ack") {
        lastBridgeMessage = `Action result acknowledged (${message.action_id || "unknown"})`;
        return;
      }

      if (message.type === "browser_pong") {
        // Keepalive response — connection healthy
        return;
      }

      if (message.type === "browser_actions" && Array.isArray(message.actions)) {
        lastBridgeMessage = `Received ${message.actions.length} action(s)`;
        log("Received browser actions", message.actions);
        await processBridgeActions(message.actions);
      }
    } catch (error) {
      lastBridgeError = String(error?.message || error);
      log("Failed to process bridge message", error);
    }
  });

  bridgeSocket.addEventListener("close", () => {
    authenticated = false;
    lastBridgeMessage = "Socket closed; reconnect scheduled";
    stopActionPolling();
    stopKeepalive();
    log("Bridge connection closed; scheduling reconnect");
    scheduleReconnect();
  });

  bridgeSocket.addEventListener("error", (error) => {
    authenticated = false;
    lastBridgeError = String(error?.message || "Bridge socket error");
    lastBridgeMessage = "Socket error";
    stopActionPolling();
    stopKeepalive();
    log("Bridge error", error);
  });
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectBridge();
  }, RECONNECT_DELAY_MS);
}

function sendToBridge(payload) {
  if (!bridgeSocket || bridgeSocket.readyState !== WebSocket.OPEN || !authenticated) {
    lastBridgeMessage = "Bridge send skipped; socket not ready or unauthenticated";
    return false;
  }
  bridgeSocket.send(JSON.stringify(payload));
  lastBridgeMessage = `Sent ${payload.type}`;
  return true;
}

// ── Tab Helpers ──

async function getActiveTab() {
  const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  return tabs[0] || null;
}

async function pushActiveTabSnapshot() {
  const tab = await getActiveTab();
  if (!tab?.id) return;
  const result = await requestSnapshotFromTab(tab.id, sessionId);
  if (!result.ok) {
    log("Could not request content snapshot", result);
  }
}

// ── Chrome Event Listeners ──

chrome.runtime.onInstalled.addListener(() => {
  connectBridge();
});

chrome.runtime.onStartup.addListener(() => {
  connectBridge();
});

chrome.tabs.onActivated.addListener(() => {
  if (authenticated) {
    pushActiveTabSnapshot();
  }
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (authenticated && changeInfo.status === "complete") {
    requestSnapshotFromTab(tabId, sessionId).catch((error) => {
      log("Snapshot refresh failed after tab update", error);
    });
  }
});

// ── Internal Message Handling ──

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "moonwalk_snapshot") {
    latestSnapshotByTab.set(message.snapshot?.tab_id || sender.tab?.id || "unknown", message.snapshot);
    const ok = sendToBridge({
      type: "browser_snapshot",
      snapshot: message.snapshot,
    });
    sendResponse({ ok, authenticated });
    return true;
  }

  if (message?.type === "moonwalk_dom_change") {
    // Forward DOM mutation events from content script to the backend bridge
    const ok = sendToBridge({
      type: "browser_dom_change",
      event: message.event,
    });
    sendResponse({ ok, authenticated });
    return true;
  }

  if (message?.type === "moonwalk_get_status") {
    sendResponse(bridgeStatus());
    return true;
  }

  if (message?.type === "moonwalk_request_snapshot") {
    pushActiveTabSnapshot()
      .then(() => sendResponse({ ok: true, ...bridgeStatus() }))
      .catch((error) => sendResponse({ ok: false, error: String(error?.message || error), ...bridgeStatus() }));
    return true;
  }

  if (message?.type === "moonwalk_ping_bridge") {
    const ok = sendToBridge({ type: "browser_ping" });
    sendResponse({ ok, authenticated, sessionId });
    return true;
  }

  return false;
});

// ── Boot ──
connectBridge();
