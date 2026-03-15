# Moonwalk — Build, Test & Distribute Guide

> **Complete, reproducible instructions** for going from a fresh clone to a signed DMG + Cloud Run deployment.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Local Setup](#2-local-setup)
3. [App Icon](#3-app-icon)
4. [Build the DMG](#4-build-the-dmg)
5. [Onboarding Wizard](#5-onboarding-wizard)
6. [Distribution — GitHub Releases](#6-distribution--github-releases)
7. [Distribution — GCS Public Bucket](#7-distribution--gcs-public-bucket)
8. [Cloud Deployment (GCP)](#8-cloud-deployment-gcp)
9. [Full-Flow Testing](#9-full-flow-testing)
10. [Auth Token Setup (Chrome Extension)](#10-auth-token-setup-chrome-extension)

---

## 1. Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| **Node.js** | ≥ 18 | `brew install node` |
| **Python** | 3.9 – 3.12 | `brew install python@3.11` |
| **Electron** | 36.2.0 | `npm install` (bundled) |
| **electron-builder** | latest | `npm install` (devDep) |
| **gcloud CLI** | latest | `brew install --cask google-cloud-sdk` |
| **Docker** | latest | `brew install --cask docker` |

---

## 2. Local Setup

```bash
# Clone
git clone https://github.com/<your-org>/Moonwalk.git
cd Moonwalk

# Node deps
npm install

# Python venv
chmod +x setup.sh && ./setup.sh
# — OR manually —
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt

# Environment
cp .env.example .env
# Fill in: OPENAI_API_KEY, GOOGLE_API_KEY, etc.

# Run
npm start
```

The Electron overlay launches, spawns the Python backend on `:8000`, and shows the glass pill.

---

## 3. App Icon

electron-builder needs `build/icon.icns`. Create one:

```bash
# From a 1024×1024 PNG
mkdir -p build/icon.iconset
for size in 16 32 64 128 256 512; do
  sips -z $size $size icon.png --out build/icon.iconset/icon_${size}x${size}.png
  sips -z $((size*2)) $((size*2)) icon.png --out build/icon.iconset/icon_${size}x${size}@2x.png
done
iconutil -c icns build/icon.iconset -o build/icon.icns
rm -rf build/icon.iconset

# Then add to package.json → build.mac:
#   "icon": "build/icon.icns"
```

---

## 4. Build the DMG

```bash
# Ensure clean state
rm -rf dist/

# Build universal macOS DMG (ARM + Intel)
npx electron-builder --mac --universal

# Output → dist/Moonwalk-1.0.0-universal.dmg
```

**First launch** on a new machine:
```
xattr -cr /Applications/Moonwalk.app
```

---

## 5. Onboarding Wizard

On first launch the overlay shows the **onboarding modal** (dark glassmorphism card):

1. **Setup checklist** — auto-checks Python backend, WebSocket, microphone
2. **Keyboard shortcuts** — shows `⌘⇧Space` (voice), `⌥Space` (command panel), `Esc` (dismiss)
3. Suggests installing the **Chrome extension** for web automation

The wizard stores completion in `localStorage` and never shows again.

---

## 6. Distribution — GitHub Releases

```bash
# Tag
git tag v1.0.0
git push origin v1.0.0

# Create release with DMG
gh release create v1.0.0 dist/Moonwalk-1.0.0-universal.dmg \
  --title "Moonwalk v1.0.0" \
  --notes "Initial release — macOS AI desktop assistant"
```

Or use the [GitHub Releases web UI](https://github.com) and drag the DMG.

---

## 7. Distribution — GCS Public Bucket

```bash
PROJECT_ID="your-project-id"
BUCKET="gs://${PROJECT_ID}-releases"

# Create bucket (one-time)
gsutil mb -l us-central1 $BUCKET
gsutil iam ch allUsers:objectViewer $BUCKET

# Upload
gsutil cp dist/Moonwalk-1.0.0-universal.dmg $BUCKET/Moonwalk-1.0.0-universal.dmg

# Public URL:
echo "https://storage.googleapis.com/${PROJECT_ID}-releases/Moonwalk-1.0.0-universal.dmg"
```

---

## 8. Cloud Deployment (GCP)

The brain runs on **Cloud Run** backed by **Firestore** and **GCS**.

```bash
PROJECT_ID="your-project-id"
REGION="us-central1"
SERVICE="moonwalk-brain"
REPO="moonwalk-repo"

# One-time setup
gcloud config set project $PROJECT_ID
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  firestore.googleapis.com \
  storage.googleapis.com

gcloud artifacts repositories create $REPO \
  --repository-format=docker \
  --location=$REGION

gcloud firestore databases create --location=$REGION

# Build & deploy
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${SERVICE}:latest"

docker build --platform linux/amd64 -t $IMAGE .
docker push $IMAGE

gcloud run deploy $SERVICE \
  --image $IMAGE \
  --region $REGION \
  --platform managed \
  --allow-unauthenticated \
  --port 8080 \
  --cpu 2 --memory 2Gi \
  --min-instances 0 --max-instances 3 \
  --timeout 300 \
  --set-env-vars "OPENAI_API_KEY=<key>,GOOGLE_API_KEY=<key>,GCP_PROJECT_ID=${PROJECT_ID},AUTH_SHARED_SECRET=<secret>"
```

**Live URL:**
```
https://moonwalk-brain-<hash>.us-central1.run.app
```

Health check: `GET /health` → `{"status":"ok","agents":0}`

---

## 9. Full-Flow Testing

```bash
# 1. Local — start Electron + backend
npm start

# 2. Voice test
#    Press ⌘⇧Space → say "What time is it?" → verify response card

# 3. Command panel test
#    Press ⌥Space → type "Open Safari" → Enter → verify action

# 4. Cloud test (from mac_client or curl)
curl -X POST https://<cloud-url>/handshake \
  -H "Content-Type: application/json" \
  -d '{"shared_secret":"<secret>","user_id":"test"}'

# 5. Chrome extension
#    Open extension options → enter Cloud URL + token
#    Navigate to any page → extension connects via bridge

# 6. Run test suite
cd tests && bash run_test.sh
```

---

## 10. Auth Token Setup (Chrome Extension)

1. **Right-click** the Moonwalk extension icon → **Options**
2. Set **Bridge URL** to your cloud server (e.g. `https://moonwalk-brain-xxx.run.app`)
3. Set **Auth Token** to the `AUTH_SHARED_SECRET` you configured in Cloud Run
4. Click **Save** — the badge turns green ✓

The extension now routes browser actions through the cloud brain.

---

## Architecture Reference

```
┌──────────────────────────────────────────────┐
│              macOS Desktop                    │
│  ┌─────────────┐     ┌──────────────────┐    │
│  │  Electron    │────▶│  Python Backend  │    │
│  │  Overlay     │ WS  │  :8000           │    │
│  │  (glass pill)│◀────│  (SPAV Agent)    │    │
│  └─────────────┘     └──────────────────┘    │
│         │                    │                │
│    ⌘⇧Space / ⌥Space    Accessibility API     │
│    Voice / Text         AppleScript / osascript│
└──────────────────────────────────────────────┘
          │
     Cloud Mode
          │
┌──────────────────────────────────────────────┐
│           Google Cloud Platform               │
│  ┌──────────────┐  ┌───────┐  ┌──────────┐  │
│  │  Cloud Run   │──│  GCS  │  │ Firestore │  │
│  │  :8080       │  │ files │  │ memory    │  │
│  └──────┬───────┘  └───────┘  └──────────┘  │
│         │                                     │
│  ┌──────┴───────┐                            │
│  │ Chrome Ext   │                            │
│  │ (bridge)     │                            │
│  └──────────────┘                            │
└──────────────────────────────────────────────┘
```

---

*Last updated: $(date +%Y-%m-%d)*
