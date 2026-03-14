/* ─────────────────────────────────────────────────────────────
   MOONWALK – Renderer (Raw Audio Streaming to Backend)
   ───────────────────────────────────────────────────────────── */

const State = Object.freeze({
  IDLE: "IDLE",
  LISTENING: "LISTENING",
  LOADING: "LOADING",
  DOING: "DOING",
  RESPONDING: "RESPONDING"
});

const WS_URL = "ws://127.0.0.1:8000/ws";

/* ── IPC Bridge ── */
const bridge = window.overlayAPI || {
  hideWindow: async () => { },
  enableMouse: () => { },
  disableMouse: () => { },
  onStartListening: () => () => { },
  onOverlayHidden: () => () => { },
  logError: () => { },
  logInfo: () => { },
};

/* ── IPC Bridge ── */

/* ── DOM Refs ── */
const wrapper = document.getElementById("ui-wrapper");
const uiIdle = document.getElementById("ui-idle");
const uiListening = document.getElementById("ui-listening");
const uiLoading = document.getElementById("ui-loading");
const uiDoing = document.getElementById("ui-doing");
const glow = document.getElementById("glow");
const uiResponse = document.getElementById("ui-response");
const statusEl = document.getElementById("status-text");
const doingTextEl = document.getElementById("doing-text");
const appIconEl = document.getElementById("app-icon");
const typewriterText = document.getElementById("typewriter-text");
const typewriterCursor = document.getElementById("typewriter-cursor");
const responseTextEl = document.getElementById("response-text");
const responseCursorEl = document.getElementById("response-cursor");
const responseDismissEl = document.getElementById("response-dismiss");
const commandPanel = document.getElementById("command-panel");
const commandInput = document.getElementById("command-input");
const commandSend = document.getElementById("command-panel-send");
const commandClose = document.getElementById("command-panel-close");

/* ── Modal DOM Refs ── */
const modalRich = document.getElementById("modal-rich");
const richTitle = document.getElementById("rich-title");
const richBody = document.getElementById("rich-body");
const richDismiss = document.getElementById("rich-dismiss");

const modalTable = document.getElementById("modal-table");
const tableTitle = document.getElementById("table-title");
const tableHead = document.getElementById("table-head");
const tableBody = document.getElementById("table-body");
const tableFooter = document.getElementById("table-footer");
const tableDismiss = document.getElementById("table-dismiss");

const modalList = document.getElementById("modal-list");
const listTitle = document.getElementById("list-title");
const listMessage = document.getElementById("list-message");
const listItems = document.getElementById("list-items");
const listDismiss = document.getElementById("list-dismiss");

const modalConfirm = document.getElementById("modal-confirm");
const confirmBody = document.getElementById("confirm-body");
const confirmActions = document.getElementById("confirm-actions");
const confirmDismiss = document.getElementById("confirm-dismiss");

const modalMedia = document.getElementById("modal-media");
const mediaMessage = document.getElementById("media-message");
const mediaImg = document.getElementById("media-img");
const mediaCaption = document.getElementById("media-caption");
const mediaDismiss = document.getElementById("media-dismiss");

const modalSteps = document.getElementById("modal-steps");
const stepsTitle = document.getElementById("steps-title");
const stepsSubtitle = document.getElementById("steps-subtitle");
const stepsMessage = document.getElementById("steps-message");
const stepsTimeline = document.getElementById("steps-timeline");
const stepsActions = document.getElementById("steps-actions");
const stepsDismiss = document.getElementById("steps-dismiss");

const modalProducts = document.getElementById("modal-products");
const productsHeader = document.getElementById("products-header");
const productsTitle = document.getElementById("products-title");
const productsSubtitle = document.getElementById("products-subtitle");
const productsMessage = document.getElementById("products-message");
const productsBody = document.getElementById("products-body");
const productsGrid = document.getElementById("products-grid");
const productsSidebar = document.getElementById("products-sidebar");
const productsDismiss = document.getElementById("products-dismiss");

/* All modal containers for bulk dismiss */
const ALL_MODALS = [uiResponse, modalRich, modalTable, modalList, modalConfirm, modalMedia, modalSteps, modalProducts];

/* ── App State ── */
const app = {
  current: State.IDLE,
  visible: true,
  ws: null,
  reconnectTimer: null,
  reconnectDelay: 700,
  reconnectMaxDelay: 7000,
  mouseEnabled: false,
  isDisposed: false,
  detectedApp: "",
  actionMessage: "Processing...",
  autoResetTimer: null,
  streamTimer: null,       // Character-by-character typing interval
  streamQueue: "",         // Text waiting to be streamed
  streamIndex: 0,          // Current position in stream

  // Audio Streaming Pipeline
  audioStream: null,
  audioContext: null,
  sourceNode: null,
  scriptProcessor: null,

  // Conversation mode
  conversationMode: false,

  // Agent tracking
  agents: {},           // id -> agent state
  runningAgents: 0,
  totalAgents: 0,
  commandPanelOpen: false,
  currentPlanId: null,  // active plan modal correlation id
  _skipAfterModalShow: false,  // Multi-modal flag: skip individual afterModalShow calls
};

/* ── UI State Management ── */
function setIslandState(nextStateClass) {
  wrapper.className = `glass-pill ${nextStateClass}`;
}

function switchContent(target) {
  uiIdle.classList.remove('active');
  uiListening.classList.remove('active');
  uiLoading.classList.remove('active');
  uiDoing.classList.remove('active');
  target.classList.add('active');
}

/** Truncate text to a maximum number of words */
function truncateToWords(text, max = 2) {
  const words = (text || '').trim().split(/\s+/);
  if (words.length <= max) return text.trim();
  return words.slice(0, max).join(' ') + '…';
}

function setState(next, { tier = "", text = null, appName = "", iconUrl = "", force = false, variant = "" } = {}) {
  if (!force && app.current === next) return;
  app.current = next;

  // Clear any previous variant class
  wrapper.classList.remove('variant-browsing', 'variant-typing', 'variant-searching', 'variant-executing', 'variant-planning');

  // Hide response card when switching to non-response states
  if (next !== State.RESPONDING && !uiResponse.classList.contains('dismissing')) {
    dismissResponseCard(true);
  }

  if (next === State.IDLE) {
    glow.classList.remove('active');
    setIslandState('state-idle');
    switchContent(uiIdle);
    statusEl.innerText = "Hey Moonwalk";
    typewriterText.innerText = '';
  }
  else if (next === State.LISTENING) {
    glow.classList.add('active');
    setIslandState('state-listening');
    switchContent(uiListening);
    typewriterText.innerText = "Listening...";
    typewriterCursor.style.display = 'inline-block';
  }
  else if (next === State.LOADING) {
    glow.classList.add('active');
    setIslandState('state-loading');
    switchContent(uiLoading);
  }
  else if (next === State.DOING) {
    glow.classList.add('active');
    setIslandState('state-doing');
    switchContent(uiDoing);

    // Apply variant class for visual differentiation
    if (variant) wrapper.classList.add(`variant-${variant}`);

    if (text) doingTextEl.innerText = truncateToWords(text, 2);

    if (iconUrl) {
      appIconEl.src = iconUrl;
      appIconEl.style.display = 'block';
    } else {
      appIconEl.style.display = 'none';
    }
  }
  else if (next === State.RESPONDING) {
    glow.classList.add('active');
    setIslandState('state-loading');
    switchContent(uiLoading);
  }
}

function clearCommandContext() {
  app.detectedApp = "";
  app.actionMessage = "Processing...";
  appIconEl.src = "";
  appIconEl.style.display = 'none';
  app.currentPlanId = null;
}

function setMouseEnabled(enabled) {
  const next = Boolean(enabled);
  if (app.mouseEnabled === next) return;
  app.mouseEnabled = next;
  next ? bridge.enableMouse() : bridge.disableMouse();
}

function openCommandPanel(prefill = "") {
  if (!commandPanel || !commandInput) return;
  app.commandPanelOpen = true;
  commandPanel.classList.remove("hidden");
  if (prefill) commandInput.value = prefill;
  setMouseEnabled(true);
  requestAnimationFrame(() => {
    commandInput.focus();
    const end = commandInput.value.length;
    commandInput.setSelectionRange(end, end);
  });
}

function closeCommandPanel({ clear = false } = {}) {
  if (!commandPanel || !commandInput) return;
  app.commandPanelOpen = false;
  commandPanel.classList.add("hidden");
  if (clear) commandInput.value = "";
  commandInput.blur();
  setMouseEnabled(false);
}

function submitCommandPanel() {
  if (!commandInput) return;
  const text = (commandInput.value || "").trim();
  if (!text) {
    commandInput.focus();
    return;
  }

  if (!app.ws || app.ws.readyState !== WebSocket.OPEN) {
    showResponseCard("Moonwalk is not connected. Try again in a moment.");
    return;
  }

  app.ws.send(JSON.stringify({
    type: "text_input",
    text,
  }));

  commandInput.value = "";
  closeCommandPanel();
  dismissAllModals(true);
  setState(State.LOADING, { force: true });
}

/* ── Response Card: Streaming Text ── */

/* ── Lightweight Markdown → HTML renderer (with KaTeX math) ── */
function renderMarkdown(text) {
  // ── 0. Extract math blocks before any other processing ──
  // We replace them with unique placeholders so markdown processing
  // (HTML escaping, bold/italic) doesn't mangle LaTeX.
  const mathSlots = [];
  function stashMath(latex, displayMode) {
    const idx = mathSlots.length;
    try {
      mathSlots.push(katex.renderToString(latex, {
        throwOnError: false,
        displayMode: displayMode,
        output: 'html',
      }));
    } catch (_) {
      // Fallback: show the raw LaTeX in a code span
      mathSlots.push(`<code>${latex}</code>`);
    }
    return `\x00MATH${idx}\x00`;
  }

  // Display math: $$...$$  (possibly multi-line)
  text = text.replace(/\$\$([\s\S]+?)\$\$/g, (_, latex) => stashMath(latex.trim(), true));
  // Inline math: $...$  (single line, non-greedy)
  text = text.replace(/\$([^$\n]+?)\$/g, (_, latex) => stashMath(latex.trim(), false));

  // ── 1. Escape HTML entities ──
  let html = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');

  // Code blocks (```lang ... ```)
  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    return `<pre class="md-code-block"><code>${code.trim()}</code></pre>`;
  });

  // Inline code (`code`)
  html = html.replace(/`([^`]+)`/g, '<code class="md-inline-code">$1</code>');

  // Bold + Italic (***text***)
  html = html.replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>');

  // Bold (**text**)
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');

  // Italic (*text*)
  html = html.replace(/(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)/g, '<em>$1</em>');

  // Headings (### h3, ## h2, # h1) — only at line start
  html = html.replace(/^### (.+)$/gm, '<h4 class="md-heading">$1</h4>');
  html = html.replace(/^## (.+)$/gm, '<h3 class="md-heading">$1</h3>');
  html = html.replace(/^# (.+)$/gm, '<h2 class="md-heading">$1</h2>');

  // Horizontal rule (--- or ***)
  html = html.replace(/^(\*{3,}|-{3,})$/gm, '<hr class="md-hr">');

  // Now split into lines for block-level processing (lists & paragraphs)
  const lines = html.split('\n');
  let result = [];
  let inOl = false;
  let inUl = false;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const olMatch = line.match(/^(\d+)\.\s+(.+)$/);
    const ulMatch = line.match(/^[-*•]\s+(.+)$/);

    if (olMatch) {
      if (!inOl) { result.push('<ol class="md-list">'); inOl = true; }
      if (inUl) { result.push('</ul>'); inUl = false; }
      result.push(`<li>${olMatch[2]}</li>`);
    } else if (ulMatch) {
      if (!inUl) { result.push('<ul class="md-list">'); inUl = true; }
      if (inOl) { result.push('</ol>'); inOl = false; }
      result.push(`<li>${ulMatch[1]}</li>`);
    } else {
      if (inOl) { result.push('</ol>'); inOl = false; }
      if (inUl) { result.push('</ul>'); inUl = false; }
      // Preserve blank lines as spacing, non-blank lines as paragraphs
      if (line.trim() === '') {
        result.push('<div class="md-spacer"></div>');
      } else if (line.startsWith('<h') || line.startsWith('<pre') || line.startsWith('<hr')) {
        result.push(line);
      } else {
        result.push(`<p class="md-paragraph">${line}</p>`);
      }
    }
  }
  if (inOl) result.push('</ol>');
  if (inUl) result.push('</ul>');

  let output = result.join('\n');

  // ── Restore math placeholders → rendered KaTeX HTML ──
  output = output.replace(/\x00MATH(\d+)\x00/g, (_, idx) => mathSlots[parseInt(idx, 10)]);

  return output;
}

function showResponseCard(fullText, awaitInput = false) {
  // Cancel any pending timers
  if (app.streamTimer) {
    clearInterval(app.streamTimer);
    app.streamTimer = null;
  }
  if (app.autoResetTimer) {
    clearTimeout(app.autoResetTimer);
    app.autoResetTimer = null;
  }

  // Reset
  responseTextEl.innerHTML = '';
  responseCursorEl.classList.remove('hidden');

  // Tokenize into words, preserving whitespace & newlines as separate tokens
  const tokens = fullText.match(/\S+|\n| +/g) || [fullText];
  let tokenIdx = 0;
  let visibleText = '';

  // Show card
  uiResponse.classList.remove('hidden', 'dismissing');
  setState(State.RESPONDING, { force: true });

  // ── Word-by-word streaming with live markdown formatting ──
  const WORD_DELAY = 35; // ms per token
  app.streamTimer = setInterval(() => {
    if (tokenIdx >= tokens.length) {
      // ── Done ──
      clearInterval(app.streamTimer);
      app.streamTimer = null;
      responseTextEl.innerHTML = renderMarkdown(fullText);
      responseCursorEl.classList.add('hidden');

      if (awaitInput || app.conversationMode) {
        setIslandState('state-listening');
        switchContent(uiListening);
        typewriterText.innerText = "Listening...";
        app.current = State.LISTENING;
        app.autoResetTimer = setTimeout(() => {
          dismissResponseCard();
          setState(State.IDLE, { force: true });
          clearCommandContext();
          app.autoResetTimer = null;
        }, app.conversationMode ? 120000 : 30000);
      } else {
        setIslandState('state-idle');
        switchContent(uiIdle);
        statusEl.innerText = "Hey Moonwalk";
        app.autoResetTimer = setTimeout(() => {
          dismissResponseCard();
          app.current = State.IDLE;
          clearCommandContext();
          app.autoResetTimer = null;
        }, 10000);
      }
      return;
    }

    visibleText += tokens[tokenIdx];
    tokenIdx++;

    // Render the visible portion as formatted markdown
    responseTextEl.innerHTML = renderMarkdown(visibleText);
    uiResponse.scrollTop = uiResponse.scrollHeight;
  }, WORD_DELAY);
}

function dismissResponseCard(instant = false) {
  // Stop any ongoing stream
  if (app.streamTimer) {
    clearInterval(app.streamTimer);
    app.streamTimer = null;
  }

  if (instant || uiResponse.classList.contains('hidden')) {
    uiResponse.classList.add('hidden');
    uiResponse.classList.remove('dismissing');
    return;
  }

  // Animate out
  uiResponse.classList.add('dismissing');
  setTimeout(() => {
    uiResponse.classList.add('hidden');
    uiResponse.classList.remove('dismissing');
  }, 300);
}

// Dismiss button
responseDismissEl.addEventListener('click', () => {
  if (app.autoResetTimer) {
    clearTimeout(app.autoResetTimer);
    app.autoResetTimer = null;
  }
  dismissResponseCard();
  dismissAllModals();
  setState(State.IDLE, { force: true });
  clearCommandContext();
});

if (commandSend) {
  commandSend.addEventListener("click", () => {
    submitCommandPanel();
  });
}

if (commandClose) {
  commandClose.addEventListener("click", () => {
    closeCommandPanel();
  });
}

if (commandInput) {
  commandInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submitCommandPanel();
    }
  });
}

/* ═══════════════════════════════════════════════════════════════
   MODAL SYSTEM — Type-specific renderers
   ═══════════════════════════════════════════════════════════════ */

/** Dismiss every modal and reset to idle */
function dismissAllModals(instant = false) {
  ALL_MODALS.forEach(el => {
    if (instant || el.classList.contains('hidden')) {
      el.classList.add('hidden');
      el.classList.remove('dismissing');
    } else {
      el.classList.add('dismissing');
      setTimeout(() => {
        el.classList.add('hidden');
        el.classList.remove('dismissing');
      }, 300);
    }
  });
  // Restore response dismiss button visibility (hidden during multi-modal stacking)
  responseDismissEl.style.display = '';
}

/** Wire dismiss buttons for all non-text modals */
[richDismiss, tableDismiss, listDismiss, confirmDismiss, mediaDismiss, stepsDismiss, productsDismiss].forEach(btn => {
  if (!btn) return;
  btn.addEventListener('click', () => {
    if (btn === stepsDismiss && modalSteps.classList.contains('mode-plan')) {
      if (app.ws && app.ws.readyState === WebSocket.OPEN) {
        app.ws.send(JSON.stringify({
          type: 'user_action',
          action: 'cancel_plan',
          plan_id: app.currentPlanId || undefined,
        }));
      }
    }
    if (app.autoResetTimer) { clearTimeout(app.autoResetTimer); app.autoResetTimer = null; }
    dismissAllModals();
    setState(State.IDLE, { force: true });
    clearCommandContext();
  });
});

/** Schedule auto-dismiss after a modal is shown */
function scheduleModalAutoDismiss(awaitInput, delayMs) {
  if (app.autoResetTimer) { clearTimeout(app.autoResetTimer); app.autoResetTimer = null; }
  const timeout = delayMs || (awaitInput ? 30000 : 12000);
  app.autoResetTimer = setTimeout(() => {
    dismissAllModals();
    setState(State.IDLE, { force: true });
    clearCommandContext();
    app.autoResetTimer = null;
  }, timeout);
}

/** Modal types that stay on screen until manually dismissed */
const PERSISTENT_MODALS = new Set(['plan', 'confirm']);

/**
 * Master entry-point for all response modals.
 * Called from the WS message handler. Parses the payload and dispatches
 * to the right modal renderer.
 */
function showResponseModal(payload, awaitInput = false) {
  // Dismiss everything first
  dismissAllModals(true);
  dismissResponseCard(true);

  // Parse structured payload — may be a JSON string from the tool
  let data = payload;
  if (typeof payload === 'string') {
    try { data = JSON.parse(payload); } catch { data = { message: payload, modal: 'text' }; }
  }

  // ── Multi-modal: array of stacked modals ──
  if (data.modals && Array.isArray(data.modals)) {
    app._skipAfterModalShow = true;
    let hasPersistent = false;

    data.modals.forEach(modalDef => {
      const type = modalDef.modal || 'text';
      if (PERSISTENT_MODALS.has(type)) hasPersistent = true;
      dispatchModal(modalDef, awaitInput);
    });

    app._skipAfterModalShow = false;
    setState(State.RESPONDING, { force: true });
    afterModalShow(awaitInput || hasPersistent, undefined, hasPersistent);
    return;
  }

  // ── Single modal ──
  dispatchModal(data, awaitInput);
}

/** Route a single modal definition to its renderer */
function dispatchModal(data, awaitInput) {
  const modalType = data.modal || 'text';
  const message = data.message || data.text || '';

  switch (modalType) {
    case 'rich':
      showRichModal(data, awaitInput);
      break;
    case 'table':
      showTableModal(data, awaitInput);
      break;
    case 'list':
      showListModal(data, awaitInput);
      break;
    case 'confirm':
      showConfirmModal(data, awaitInput);
      break;
    case 'media':
      showMediaModal(data, awaitInput);
      break;
    case 'steps':
      showStepsModal(data, awaitInput);
      break;
    case 'plan':
      data._planMode = true;
      showStepsModal(data, true);
      break;
    case 'cards':
    case 'products':
      showProductsModal(data, awaitInput);
      break;
    case 'text':
    default:
      if (app._skipAfterModalShow) {
        // Multi-modal text: show instantly (no streaming), hide its own dismiss
        responseTextEl.innerHTML = renderMarkdown(message);
        responseCursorEl.classList.add('hidden');
        responseDismissEl.style.display = 'none';
        uiResponse.classList.remove('hidden', 'dismissing');
      } else {
        responseDismissEl.style.display = '';
        showResponseCard(message, awaitInput);
      }
      break;
  }
}

/* ── Rich Modal ── */
function showRichModal(data, awaitInput) {
  richTitle.textContent = data.title || '';
  richTitle.style.display = data.title ? '' : 'none';
  richBody.innerHTML = renderMarkdown(data.message || '');

  modalRich.classList.remove('hidden', 'dismissing');
  setState(State.RESPONDING, { force: true });
  if (!app._skipAfterModalShow) afterModalShow(awaitInput, 15000);
}

/* ── Table Modal ── */
function showTableModal(data, awaitInput) {
  tableTitle.textContent = data.title || '';
  tableTitle.style.display = data.title ? '' : 'none';

  // Render header
  tableHead.innerHTML = '';
  if (data.headers && data.headers.length) {
    const tr = document.createElement('tr');
    data.headers.forEach(h => {
      const th = document.createElement('th');
      th.textContent = h;
      tr.appendChild(th);
    });
    tableHead.appendChild(tr);
  }

  // Render rows
  tableBody.innerHTML = '';
  if (data.rows && data.rows.length) {
    data.rows.forEach(row => {
      const tr = document.createElement('tr');
      (Array.isArray(row) ? row : [row]).forEach(cell => {
        const td = document.createElement('td');
        td.textContent = String(cell);
        tr.appendChild(td);
      });
      tableBody.appendChild(tr);
    });
  }

  // Footer message
  tableFooter.innerHTML = data.message ? renderMarkdown(data.message) : '';
  tableFooter.style.display = data.message ? '' : 'none';

  modalTable.classList.remove('hidden', 'dismissing');
  setState(State.RESPONDING, { force: true });
  if (!app._skipAfterModalShow) afterModalShow(awaitInput, 15000);
}

/* ── List Modal ── */
function showListModal(data, awaitInput) {
  listTitle.textContent = data.title || '';
  listTitle.style.display = data.title ? '' : 'none';

  listMessage.innerHTML = data.message ? renderMarkdown(data.message) : '';
  listMessage.style.display = data.message ? '' : 'none';

  listItems.innerHTML = '';
  (data.items || []).forEach((item, idx) => {
    const card = document.createElement('div');
    card.className = 'modal-list-card';
    // Use explicit icon if provided, otherwise auto-number
    const indicator = item.icon
      ? `<span class="list-card-icon">${escapeHtml(item.icon)}</span>`
      : `<span class="list-card-num">${idx + 1}</span>`;
    card.innerHTML = `
      ${indicator}
      <div class="list-card-body">
        <div class="list-card-title">${escapeHtml(item.title)}</div>
        ${item.description ? `<div class="list-card-desc">${escapeHtml(item.description)}</div>` : ''}
      </div>
    `;
    listItems.appendChild(card);
  });

  modalList.classList.remove('hidden', 'dismissing');
  setState(State.RESPONDING, { force: true });
  if (!app._skipAfterModalShow) afterModalShow(awaitInput, 15000);
}

/* ── Confirm Modal ── */
function showConfirmModal(data, awaitInput) {
  confirmBody.innerHTML = renderMarkdown(data.message || '');

  confirmActions.innerHTML = '';
  (data.actions || []).forEach((action, i) => {
    const btn = document.createElement('button');
    btn.className = i === 0 ? 'modal-confirm-btn primary' : 'modal-confirm-btn secondary';
    btn.textContent = action.label;
    btn.addEventListener('click', () => {
      // Send the choice back via WS
      if (app.ws && app.ws.readyState === WebSocket.OPEN) {
        app.ws.send(JSON.stringify({
          type: "user_action",
          action: action.value
        }));
      }
      dismissAllModals();
      setState(State.LOADING, { force: true });
    });
    confirmActions.appendChild(btn);
  });

  modalConfirm.classList.remove('hidden', 'dismissing');
  setState(State.RESPONDING, { force: true });
  // Confirm modals are persistent — stay until user picks or dismisses
  if (!app._skipAfterModalShow) afterModalShow(true, 60000, true);
}

/* ── Media Modal ── */
function showMediaModal(data, awaitInput) {
  mediaMessage.innerHTML = data.message ? renderMarkdown(data.message) : '';
  mediaMessage.style.display = data.message ? '' : 'none';

  mediaImg.src = data.media_url || '';
  mediaImg.alt = data.caption || 'Image';
  mediaCaption.textContent = data.caption || '';
  mediaCaption.style.display = data.caption ? '' : 'none';

  modalMedia.classList.remove('hidden', 'dismissing');
  setState(State.RESPONDING, { force: true });
  if (!app._skipAfterModalShow) afterModalShow(awaitInput, 15000);
}

/* ── Steps Modal (dual mode: progress / plan) ── */
function showStepsModal(data, awaitInput) {
  const isPlan = data._planMode || data.steps_mode === 'plan' || data.modal === 'plan';
  const stepsList = data.steps || [];
  app.currentPlanId = isPlan ? (data.plan_id || null) : null;

  // ── Mode class on container ──
  modalSteps.classList.remove('mode-progress', 'mode-plan');
  modalSteps.classList.add(isPlan ? 'mode-plan' : 'mode-progress');

  // ── Title & subtitle ──
  if (isPlan) {
    // Plan modal: headerless — pair with a text bubble above for context
    stepsTitle.style.display = 'none';
    stepsSubtitle.style.display = 'none';
    stepsMessage.style.display = 'none';
  } else {
    stepsTitle.textContent = data.title || 'Task Progress';
    stepsTitle.style.display = '';
    const done = stepsList.filter(s => s.status === 'done').length;
    if (stepsList.length > 0) {
      stepsSubtitle.textContent = `${done} of ${stepsList.length} complete`;
      stepsSubtitle.style.display = '';
    } else {
      stepsSubtitle.style.display = 'none';
    }
  }

  // ── Message (only for progress mode) ──
  if (!isPlan && data.message) {
    stepsMessage.innerHTML = renderMarkdown(data.message);
    stepsMessage.style.display = '';
  } else if (!isPlan) {
    stepsMessage.style.display = 'none';
  }

  // ── Timeline / Plan items ──
  stepsTimeline.innerHTML = '';
  stepsList.forEach((step, i) => {
    const el = document.createElement('div');
    if (isPlan) {
      // Card-row style for plan items
      el.className = 'modal-step-item planned';
      el.style.animationDelay = `${i * 50}ms`;
      el.innerHTML = `
        <div class="step-plan-num">${i + 1}</div>
        <div class="step-body">
          <div class="step-label">${escapeHtml(step.label)}</div>
          ${step.detail ? `<div class="step-detail">${escapeHtml(step.detail)}</div>` : ''}
        </div>
      `;
    } else {
      el.className = `modal-step-item ${step.status}`;
      const icon = step.status === 'done' ? 'OK' : step.status === 'current' ? 'NOW' : '...';
      el.innerHTML = `
        <div class="step-indicator">${icon}</div>
        <div class="step-body">
          <div class="step-label">${escapeHtml(step.label)}</div>
          ${step.detail ? `<div class="step-detail">${escapeHtml(step.detail)}</div>` : ''}
        </div>
      `;
    }
    stepsTimeline.appendChild(el);
  });

  // ── Plan actions ──
  if (isPlan) {
    stepsActions.innerHTML = '';

    const startBtn = document.createElement('button');
    startBtn.className = 'modal-steps-btn-start';
    startBtn.innerHTML = `Proceed
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
        <path d="M5 12h14"/><path d="M12 5l7 7-7 7"/>
      </svg>`;
    startBtn.addEventListener('click', () => {
      if (app.ws && app.ws.readyState === WebSocket.OPEN) {
        app.ws.send(JSON.stringify({
          type: 'user_action',
          action: 'approve_plan',
          plan_id: app.currentPlanId || undefined,
        }));
      }
      dismissAllModals();
      setState(State.LOADING, { force: true });
    });

    stepsActions.appendChild(startBtn);
    stepsActions.classList.remove('hidden');
  } else {
    stepsActions.classList.add('hidden');
  }

  modalSteps.classList.remove('hidden', 'dismissing');
  setState(State.RESPONDING, { force: true });

  if (!app._skipAfterModalShow) {
    if (isPlan) {
      // Plan is persistent — stays until user approves, modifies, or cancels
      afterModalShow(true, undefined, true);
    } else {
      afterModalShow(awaitInput, 15000);
    }
  }
}

/* ── Cards Modal (general-purpose image+text cards) ── */
function showProductsModal(data, awaitInput) {
  // Accept both 'cards' (new) and 'products' (legacy) item arrays
  const items = data.cards || data.products || [];

  // ── Header ──
  if (data.title) {
    productsTitle.textContent = data.title;
    productsTitle.style.display = '';
  } else {
    productsTitle.style.display = 'none';
  }
  if (items.length > 0 && data.subtitle) {
    productsSubtitle.textContent = data.subtitle;
    productsSubtitle.style.display = '';
  } else if (items.length > 0) {
    productsSubtitle.textContent = `${items.length} result${items.length !== 1 ? 's' : ''}`;
    productsSubtitle.style.display = '';
  } else {
    productsSubtitle.style.display = 'none';
  }

  // ── Message ──
  productsMessage.innerHTML = data.message ? renderMarkdown(data.message) : '';
  productsMessage.style.display = data.message ? '' : 'none';

  // ── Sidebar (removed — keep it hidden) ──
  modalProducts.classList.remove('has-sidebar');
  productsSidebar.classList.add('hidden');

  // ── Cards ──
  productsGrid.innerHTML = '';
  items.forEach((item, idx) => {
    const card = document.createElement('div');
    card.className = 'card-item';
    card.style.animationDelay = `${idx * 50}ms`;
    if (item.url || item.link) card.classList.add('has-link');

    // ── Image ──
    let imgHtml = '';
    if (item.image) {
      imgHtml = `<div class="card-img-wrap"><img class="card-img" src="${escapeHtml(item.image)}" alt="${escapeHtml(item.name || item.title || '')}" loading="lazy" /></div>`;
    }

    // ── Title ──
    const title = item.name || item.title || '';

    // ── Description ──
    let descHtml = '';
    if (item.description) {
      descHtml = `<div class="card-desc">${escapeHtml(item.description)}</div>`;
    }

    // ── Meta line: price, rating, source ──
    let metaParts = [];
    if (item.price) {
      const priceClass = item.original_price ? 'card-price on-sale' : 'card-price';
      let priceStr = `<span class="${priceClass}">${escapeHtml(item.price)}</span>`;
      if (item.original_price) {
        priceStr += `<span class="card-price-original">${escapeHtml(item.original_price)}</span>`;
      }
      metaParts.push(priceStr);
    }
    if (item.rating != null) {
      const full = Math.floor(item.rating);
      const half = item.rating % 1 >= 0.5 ? 1 : 0;
      const empty = 5 - full - half;
      let stars = '<span class="card-stars">'
        + '★'.repeat(full) + (half ? '⯨' : '') + '<span class="star-empty">' + '★'.repeat(empty) + '</span>'
        + `</span> <span class="card-rating-num">${item.rating}</span>`;
      if (item.reviews) stars += ` <span class="card-reviews">(${escapeHtml(String(item.reviews))})</span>`;
      metaParts.push(stars);
    }
    if (item.source) {
      metaParts.push(`<span class="card-source">${escapeHtml(item.source)}</span>`);
    }
    const metaHtml = metaParts.length ? `<div class="card-meta">${metaParts.join('<span class="card-meta-sep">·</span>')}</div>` : '';

    // ── Link / CTA ──
    const linkUrl = item.url || item.link || '';
    let linkHtml = '';
    if (linkUrl) {
      const linkLabel = item.link_label || 'View';
      linkHtml = `<div class="card-link" data-url="${escapeHtml(linkUrl)}">
        <span>${escapeHtml(linkLabel)}</span>
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M7 17L17 7M17 7H7M17 7v10"/></svg>
      </div>`;
    }

    card.innerHTML = `
      ${imgHtml}
      <div class="card-body">
        <div class="card-title">${escapeHtml(title)}</div>
        ${descHtml}
        ${metaHtml}
        ${linkHtml}
      </div>
    `;

    // Link click handler
    const linkEl = card.querySelector('.card-link');
    if (linkEl) {
      linkEl.addEventListener('click', (e) => {
        e.stopPropagation();
        if (app.ws && app.ws.readyState === WebSocket.OPEN) {
          app.ws.send(JSON.stringify({ type: 'open_url', url: linkUrl }));
        }
      });
    }

    // Whole-card click if has link
    if (linkUrl) {
      card.addEventListener('click', () => {
        if (app.ws && app.ws.readyState === WebSocket.OPEN) {
          app.ws.send(JSON.stringify({ type: 'open_url', url: linkUrl }));
        }
      });
    }

    productsGrid.appendChild(card);
  });

  modalProducts.classList.remove('hidden', 'dismissing');
  setState(State.RESPONDING, { force: true });
  if (!app._skipAfterModalShow) afterModalShow(awaitInput, 20000);
}

/** Shared post-show: set island state + schedule dismiss
 *  persistent = true → modal stays until manually dismissed (no timer) */
function afterModalShow(awaitInput, defaultTimeout, persistent = false) {
  if (awaitInput || app.conversationMode) {
    setIslandState('state-listening');
    switchContent(uiListening);
    typewriterText.innerText = "Listening...";
    app.current = State.LISTENING;
    if (!persistent) scheduleModalAutoDismiss(true, app.conversationMode ? 120000 : 30000);
  } else {
    setIslandState('state-idle');
    switchContent(uiIdle);
    statusEl.innerText = "Hey Moonwalk";
    if (!persistent) scheduleModalAutoDismiss(false, defaultTimeout);
  }
}

/* ── Audio Encoding (PCM to Base64 WAV) ── */

function floatTo16BitPCM(output, offset, input) {
  for (let i = 0; i < input.length; i++, offset += 2) {
    // Clamp between -1 and 1
    const s = Math.max(-1, Math.min(1, input[i]));
    // Convert to 16-bit integer (multiply by 0x7FFF)
    output.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
  }
}

function writeString(view, offset, string) {
  for (let i = 0; i < string.length; i++) {
    view.setUint8(offset + i, string.charCodeAt(i));
  }
}

// Packages the raw Float32Array PCM chunk into a full WAV file Buffer
function encodeWAVChunk(samples, sampleRate) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);

  // RIFF chunk descriptor
  writeString(view, 0, 'RIFF');
  view.setUint32(4, 36 + samples.length * 2, true);
  writeString(view, 8, 'WAVE');

  // fmt sub-chunk
  writeString(view, 12, 'fmt ');
  view.setUint32(16, 16, true);             // Subchunk1Size (16 for PCM)
  view.setUint16(20, 1, true);              // AudioFormat (1 for PCM)
  view.setUint16(22, 1, true);              // NumChannels (1: mono)
  view.setUint32(24, sampleRate, true);     // SampleRate
  view.setUint32(28, sampleRate * 2, true); // ByteRate (SampleRate * NumChannels * BitsPerSample/8)
  view.setUint16(32, 2, true);              // BlockAlign (NumChannels * BitsPerSample/8)
  view.setUint16(34, 16, true);             // BitsPerSample

  // data sub-chunk
  writeString(view, 36, 'data');
  view.setUint32(40, samples.length * 2, true);

  // Write the PCM samples
  floatTo16BitPCM(view, 44, samples);

  return buffer;
}

function arrayBufferToBase64(buffer) {
  let binary = '';
  const bytes = new Uint8Array(buffer);
  const len = bytes.byteLength;
  for (let i = 0; i < len; i++) {
    binary += String.fromCharCode(bytes[i]);
  }
  return window.btoa(binary);
}

/* ── Continuous Microphone Streaming ── */

async function startAudioStreaming() {
  if (app.audioStream) return; // Already running

  try {
    app.audioStream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1 } });
    app.audioContext = new (window.AudioContext || window.webkitAudioContext)({
      sampleRate: 16000 // Force 16kHz for backend speech models
    });

    // We use ScriptProcessorNode because it's the easiest cross-platform way 
    // to access raw PCM data without AudioWorklet complexity.
    app.sourceNode = app.audioContext.createMediaStreamSource(app.audioStream);
    // Higher frequency chunks for lower latency (1024 samples @ 16kHz = 64ms)
    app.scriptProcessor = app.audioContext.createScriptProcessor(1024, 1, 1);

    app.scriptProcessor.onaudioprocess = (event) => {
      // Only send if websocket is open
      if (!app.ws || app.ws.readyState !== WebSocket.OPEN) return;

      const inputBuffer = event.inputBuffer.getChannelData(0); // Mono Float32Array
      const sampleRate = app.audioContext.sampleRate; // Typically 16000 here

      // Pack into a WAV wrapper
      const wavBuffer = encodeWAVChunk(inputBuffer, sampleRate);

      // Convert to base64
      const base64Audio = arrayBufferToBase64(wavBuffer);

      // Stream to Python Backend
      app.ws.send(JSON.stringify({
        type: "audio_chunk",
        payload: base64Audio
      }));
    };

    app.sourceNode.connect(app.scriptProcessor);
    app.scriptProcessor.connect(app.audioContext.destination);

    console.log("Started continuous audio streaming at 16kHz");
  } catch (err) {
    console.error("Failed to access microphone:", err);
    statusEl.innerText = "Mic Error";
    if (bridge.logError) {
      bridge.logError(`Mic Access Failed: ${err.message}`);
    }
  }
}

async function stopAudioStreaming() {
  if (app.scriptProcessor) {
    app.scriptProcessor.disconnect();
    app.scriptProcessor = null;
  }
  if (app.sourceNode) {
    app.sourceNode.disconnect();
    app.sourceNode = null;
  }
  if (app.audioContext) {
    await app.audioContext.close();
    app.audioContext = null;
  }
  if (app.audioStream) {
    app.audioStream.getTracks().forEach(track => track.stop());
    app.audioStream = null;
  }
}


/* ── WebSocket ── */

function scheduleReconnect() {
  if (app.isDisposed || app.reconnectTimer) return;
  app.reconnectTimer = window.setTimeout(() => {
    app.reconnectTimer = null;
    if (!app.isDisposed) connectWebSocket();
  }, app.reconnectDelay);
  app.reconnectDelay = Math.min(Math.round(app.reconnectDelay * 1.6), app.reconnectMaxDelay);
}

function connectWebSocket() {
  if (app.isDisposed) return;
  if (app.ws && (app.ws.readyState === WebSocket.OPEN || app.ws.readyState === WebSocket.CONNECTING)) return;

  try {
    app.ws = new WebSocket(WS_URL);
    app.ws.addEventListener("open", () => {
      app.reconnectDelay = 700;
      statusEl.innerText = "Hey Moonwalk";
    });

    app.ws.addEventListener("message", (event) => {
      if (typeof event.data !== "string") return;
      let msg;
      try { msg = JSON.parse(event.data); } catch { return; }

      console.log("[WS] Received:", msg);

      // ── Agent message types ──

      // 1. "thinking" — Agent is reasoning (show bouncing dots)
      if (msg.type === "thinking" || msg.type === "progress" || msg.state === "state-loading") {
        setState(State.LOADING, { force: true });
        return;
      }

      // 2. "doing" — Agent is executing a tool (show spinner + action text)
      if (msg.type === "doing") {
        // Cancel any pending auto-reset so sequential tool steps show properly
        if (app.autoResetTimer) {
          clearTimeout(app.autoResetTimer);
          app.autoResetTimer = null;
        }
        setState(State.DOING, {
          text: msg.text || "Working...",
          appName: msg.app || "",
          iconUrl: msg.icon_url || "",
          variant: msg.variant || "",
          force: true
        });
        return;
      }

      // 3. "response" — Agent finished, show final answer (routed through modal system)
      if (msg.type === "response" || msg.type === "action") {
        let payload = msg.payload || {};
        

        
        const text = payload.text || payload.message || "Done!";
        const awaitInput = payload.await_input || false;
        app.detectedApp = payload.app || "";

        // If payload contains structured modal data, route through modal system
        if (payload.modal_data || payload.modal) {
          showResponseModal(payload.modal_data || payload, awaitInput);
        } else {
          // Legacy: plain text → default text card
          showResponseCard(text, awaitInput);
        }
        return;
      }

      // 3b. "conversation_mode" — Toggle persistent listening mode
      if (msg.type === "conversation_mode") {
        app.conversationMode = !!msg.enabled;
        const pill = document.getElementById("ui-wrapper");
        if (app.conversationMode) {
          pill.classList.add('conversation-mode');
        } else {
          pill.classList.remove('conversation-mode');
        }
        return;
      }

      // 4. "status" — Direct state transitions (idle, listening, etc.)
      const stateStr = msg.state || (msg.type === "status" ? msg.state : null);
      if (stateStr) {
        if (app.autoResetTimer) {
          clearTimeout(app.autoResetTimer);
          app.autoResetTimer = null;
        }
        const nextState = State[stateStr.toUpperCase().replace("STATE-", "")];
        if (nextState) {
          setState(nextState, { force: true });
          if (nextState === State.IDLE) clearCommandContext();
        }
      }

    });

    app.ws.addEventListener("error", (err) => {
      console.error("[WS] Connection Error:", err);
      if (bridge.logError) {
        bridge.logError("WebSocket connection failed to ws://127.0.0.1:8000/ws");
      }
    });

    app.ws.addEventListener("close", (e) => {
      console.warn("[WS] Connection Closed:", e.code, e.reason);
      if (bridge.logError) {
        bridge.logError(`WebSocket closed: ${e.code} ${e.reason}`);
      }
      scheduleReconnect();
    });
  } catch {
    scheduleReconnect();
  }
}

/* ── Events ── */
// Hit-test: check if mouse is over any interactive element
function isOverInteractive(event) {
  const x = event.clientX;
  const y = event.clientY;
  const rects = [wrapper.getBoundingClientRect()];
  if (app.commandPanelOpen && commandPanel && !commandPanel.classList.contains("hidden")) {
    rects.push(commandPanel.getBoundingClientRect());
  }
  // Check response card if visible
  if (!uiResponse.classList.contains('hidden')) rects.push(uiResponse.getBoundingClientRect());
  // Check all modal types
  ALL_MODALS.forEach(m => {
    if (!m.classList.contains('hidden')) rects.push(m.getBoundingClientRect());
  });

  return rects.some(r => x >= r.left && x <= r.right && y >= r.top && y <= r.bottom);
}

document.addEventListener("mousemove", (event) => {
  if (!app.visible) return setMouseEnabled(false);
  setMouseEnabled(isOverInteractive(event));
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    if (app.commandPanelOpen) return closeCommandPanel();
    return bridge.hideWindow();
  }
});

// Global shortcut opens the text command panel for direct prompting.
bridge.onStartListening(() => {
  dismissAllModals(true);
  setState(State.IDLE, { force: true });
  openCommandPanel();
});

bridge.onOverlayHidden(async () => {
  clearCommandContext();
  closeCommandPanel({ clear: true });
  app.visible = true;
  wrapper.classList.remove("hidden");
  setMouseEnabled(false);
  setState(State.IDLE, { force: true });
});

window.addEventListener("beforeunload", async () => {
  app.isDisposed = true;
  await stopAudioStreaming();
  if (app.reconnectTimer) clearTimeout(app.reconnectTimer);
  if (app.ws && app.ws.readyState <= WebSocket.OPEN) app.ws.close();
});

/* ── Init ── */
setState(State.IDLE, { force: true });
wrapper.classList.remove("hidden");
setMouseEnabled(false);

// 1. Connect WS
connectWebSocket();

// 2. Start continuously recording and streaming Base64 WAV chunks
startAudioStreaming();

/* ══════════════════════════════════════════════════════════════
   Foreground Agent Event Tracking (no drawer UI)
   ══════════════════════════════════════════════════════════════ */
// ── Utility Functions ──

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}
