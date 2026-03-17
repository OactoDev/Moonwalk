# Moonwalk

**AI desktop assistant for macOS** — a transparent glass-pill overlay that uses voice and text to control your Mac.

Built with Electron + Python. Thinks with LLMs. Acts through Accessibility APIs and AppleScript.

---

<p align="center">
  <img src="docs/screenshots/Moonwalk.png" alt="Logo" width="480"/>
</p>

---
Demo Video: [https://youtu.be/u3QoaT3pIMs]

## Features

- **Voice-first** — Say "Hey Moonwalk" or Press `⌘⇧Space`, speak naturally, watch it act
- **SPAV Agent** — Sense → Plan → Act → Verify loop with milestone-based execution
- **Multi-modal responses** — Cards, tables, rich text, step timelines, image viewer
- **Browser automation** — Chrome extension bridges web actions (search, fill forms, extract data)
- **Cloud-ready** — Deploy brain to GCP Cloud Run with Firestore memory + GCS storage
- **On-device privacy** — Local mode runs everything on your Mac, nothing leaves the machine

---

## UI States

The glass pill morphs between four states:

<table>
<tr>
<td align="center"><strong>Idle</strong><br/><img src="docs/screenshots/pill-idle.svg" width="280"/><br/><code>220px</code> · mic icon + "Hey Moonwalk"</td>
<td align="center"><strong>Listening</strong><br/><img src="docs/screenshots/pill-listening.svg" width="280"/><br/><code>440px</code> · typewriter transcription</td>
</tr>
<tr>
<td align="center"><strong>Thinking</strong><br/><img src="docs/screenshots/pill-loading.svg" width="280"/><br/><code>140px</code> · bouncing dots</td>
<td align="center"><strong>Doing</strong><br/><img src="docs/screenshots/pill-doing.svg" width="280"/><br/><code>320px</code> · spinner + app icon + action</td>
</tr>
</table>

### Response Card & Plan Preview

<table>
<tr>
<td align="center"><img src="docs/screenshots/response-card.svg" width="360"/><br/><strong>Streaming response card</strong><br/>Markdown, KaTeX math, code blocks</td>
<td align="center"><img src="docs/screenshots/plan-modal.svg" width="360"/><br/><strong>Plan preview modal</strong><br/>Step-by-step with "Proceed" button</td>
</tr>
</table>

### Command Panel & Onboarding

<table>
<tr>
<td align="center"><img src="docs/screenshots/command-panel.svg" width="360"/><br/><strong>Command panel</strong> (⌥Space)<br/>Type-to-prompt with send button</td>
<td align="center"><img src="docs/screenshots/onboarding.svg" width="360"/><br/><strong>First-launch onboarding</strong><br/>Auto-checks + keyboard shortcuts</td>
</tr>
</table>

---

## Architecture

<p align="center">
  <img src="docs/screenshots/architecture.svg" alt="Moonwalk Architecture" width="800"/>
</p>

### Agent Loop (SPAV)

<p align="center">
  <img src="docs/screenshots/spav-loop.svg" alt="SPAV Agent Loop" width="600"/>
</p>

---

## Quick Start

```bash
# 1. Clone & install
git clone https://github.com/<your-org>/Moonwalk.git
cd Moonwalk
npm install

# 2. Python environment
chmod +x setup.sh && ./setup.sh
# — or manually —
python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt

# 3. Environment variables
cp .env.example .env
# Fill in: GOOGLE_API_KEY

# 4. Launch
npm start
```

### Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `⌘⇧Space` | Activate voice input |
| `⌥Space` | Open command panel |
| `Esc` | Dismiss overlay |

---

## Project Structure

```
Moonwalk/
├── main.js                  # Electron main process
├── preload.js               # IPC bridge (auth, credentials, mouse)
├── package.json             # Electron + electron-builder config
├── Dockerfile               # Multi-stage cloud build
├── setup.sh                 # Post-install Python venv setup
│
├── renderer/
│   ├── index.html           # Glass-pill overlay markup
│   ├── styles.css           # Full UI styling (glassmorphism, modals)
│   └── renderer.js          # State machine, WS client, audio, modals
│
├── backend/
│   ├── runtime_state.py     # Per-user state registry
│   ├── auth.py              # Dual-mode auth (GCP ID + shared secret)
│   ├── agent/
│   │   ├── core_v2.py       # SPAV agent loop
│   │   ├── planner.py       # LLM task decomposition
│   │   ├── milestone_executor.py  # Step-by-step execution
│   │   ├── perception.py    # Screen reading + accessibility
│   │   ├── verifier.py      # Post-action verification
│   │   ├── glance.py        # Parallel screen perception
│   │   ├── memory.py        # Conversation memory
│   │   └── world_state.py   # Environment tracking
│   ├── browser/
│   │   ├── bridge.py        # Chrome extension WebSocket bridge
│   │   ├── search.py        # Web search via extension
│   │   └── selector_ai.py   # AI-powered DOM selector
│   ├── servers/
│   │   ├── cloud_server.py  # Cloud Run FastAPI server
│   │   └── mac_client.py    # Local macOS WebSocket server
│   └── tools/               # Tool implementations (click, type, etc.)
│
├── chrome_extension/
│   ├── manifest.json        # MV3 manifest
│   ├── background.js        # Service worker
│   ├── content_script.js    # Page interaction
│   ├── popup.html/js        # Extension popup
│   └── options.html/js      # Bridge URL + auth config
│
├── docs/
│   ├── GUIDE.md             # Full build/test/distribute guide
│   └── screenshots/         # UI mockup SVGs
│
├── tests/                   # Test suite
└── benchmarks/              # Quality & intelligence benchmarks
```

---

## ☁️ Cloud Deployment

Deploy the brain to GCP Cloud Run for multi-user, always-on operation:

```bash
# Build & push
docker build --platform linux/amd64 -t $IMAGE .
docker push $IMAGE

# Deploy
gcloud run deploy moonwalk-brain \
  --image $IMAGE \
  --region us-central1 \
  --allow-unauthenticated \
  --port 8080 \
  --cpu 2 --memory 2Gi \
  --set-env-vars "OPENAI_API_KEY=<key>,AUTH_SHARED_SECRET=<secret>"
```

Health check: `GET /health` → `{"status":"ok","agents":0}`

See [docs/GUIDE.md](docs/GUIDE.md) for the full step-by-step deployment guide.

---

## 🧩 Chrome Extension

The **Moonwalk Browser Bridge** extension enables web automation:

1. Install from `chrome_extension/` → `chrome://extensions` → Load unpacked
2. Open **Options** → set Bridge URL + Auth Token
3. The agent can now search the web, extract listings, fill forms, and interact with pages

---

## 🧪 Testing

```bash
# Run full test suite
cd tests && bash run_test.sh

# Individual tests
python -m pytest tests/test_agent_v2.py -v
python -m pytest tests/test_milestone_executor.py -v
python -m pytest tests/test_browser_scenarios.py -v

# Benchmarks
python benchmarks/run_benchmarks.py
```

---

## 📦 Building for Distribution

```bash
# Build universal macOS DMG
npx electron-builder --mac --universal

# Output: dist/Moonwalk-1.0.0-universal.dmg
```

See [docs/GUIDE.md](docs/GUIDE.md) for icon creation, code signing, notarization, and distribution via GitHub Releases or GCS.

---

## 📝 License

MIT

---

<p align="center">
  <strong>Moonwalk</strong> — Your AI copilot for macOS<br/>
  <sub>Voice-first · Glass UI · SPAV Agent · Cloud-ready</sub>
</p>
