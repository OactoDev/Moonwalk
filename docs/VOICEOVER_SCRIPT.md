# Moonwalk — How It Works · Voice-Over Script

> **28 slides · ~4:30 total runtime**
> Timings are per-slide durations. Breathing slides (marked ✦) are visual-only — narrator pauses.

---

## Slide 00 · Title *(8s)*
"This is Moonwalk — an AI assistant that lives on your desktop. It sees your screen, hears your voice, and acts for you."

## Slide 01 · Problem *(10s)*
"Today, every task means switching apps, clicking through menus, and stitching tools together yourself. You are the integration layer between your own software. Moonwalk changes that."

## Slide 02 · The Pill *(10s)*
"Moonwalk lives as a small frosted-glass capsule at the top of your screen. Always present, never in the way. Wake it with a keyboard shortcut or just say 'Hey Moonwalk'."

## Slide 03 · Pill States *(5s)* ✦
*[Pause — let the four pill states animate on screen]*

## Slide 04 · Input *(10s)*
"You can interact three ways: speak to it with natural voice, type in the command panel, or just let it see what's on your screen for context."

## Slide 05 · Section: Architecture *(5s)*
"Under the hood, Moonwalk is built in three layers."

## Slide 06 · Electron Layer *(10s)*
"The top layer is an Electron shell — a transparent, always-on-top window that renders the pill and all modal surfaces using GPU-accelerated frosted glass."

## Slide 07 · Python Layer *(10s)*
"Below that sits a Python backend — a FastAPI server connected over WebSocket. This is where all AI reasoning, tool orchestration, and state management happen."

## Slide 08 · macOS Layer *(8s)*
"The third layer reaches into macOS itself — AppleScript automation, screenshot capture, and native system control."

## Slide 09 · Section: The Brain *(5s)*
"Let's look at the brain — the AI models that power everything."

## Slide 10 · Gemini Tiers *(14s)*
"Moonwalk uses three tiers of Google's Gemini models. Flash handles quick answers in about 300 milliseconds. Pro takes on complex reasoning and multi-step planning. And Deep Research goes deep — synthesising information across multiple sources."

## Slide 11 · Routing *(10s)*
"Every request is automatically routed to the best model. A fast Flash classification determines complexity, checks if tools are needed, and picks the optimal tier — all before the main model even starts."

## Slide 12 · Routing Code *(5s)* ✦
*[Pause — code snippet on screen]*

## Slide 13 · Perception *(12s)*
"The perception engine gives Moonwalk eyes. It captures your active window, runs Gemini Flash vision analysis on the screenshot, and builds a structured world state — so the agent always knows what you're looking at."

## Slide 14 · Perception Snapshot *(5s)* ✦
*[Pause — JSON snapshot on screen]*

## Slide 15 · Section: The Agent *(5s)*
"Now the core: the agent loop — Sense, Plan, Act, Verify."

## Slide 16 · SPAV: Sense + Plan *(12s)*
"First, the agent senses — gathering perception data, conversation history, and clipboard contents into a complete world state. Then it plans — decomposing your request into ordered milestones, each with clear success criteria."

## Slide 17 · SPAV: Act + Verify *(12s)*
"Next, it acts — executing tools one at a time, streaming progress to the pill. After each action, it verifies — capturing a fresh screenshot and checking that the expected outcome was achieved. If not, it replans and retries."

## Slide 18 · Milestones *(12s)*
"Complex tasks are broken into milestone checkpoints. For example, booking a flight becomes four steps — open Google Flights, enter search criteria, select the best option, and confirm. The agent can't skip ahead; each gate must pass."

## Slide 19 · Milestone Gate *(5s)* ✦
*[Pause — verification gate flow and code on screen]*

## Slide 20 · Tools A *(10s)*
"The agent has access to about eighty tools across six domains. Browser tools navigate the web, file system tools manage your Mac, and communication tools handle email and calendar."

## Slide 21 · Tools B *(10s)*
"System tools control settings and apps, research tools synthesise information from multiple sources, and workspace tools connect to Google Docs, Sheets, and Drive."

## Slide 22 · Browser Bridge *(12s)*
"The browser bridge is particularly powerful. A Chrome extension injects a content script into every tab. Python sends commands over WebSocket — click this element, fill that form, extract this data — and gets structured results back."

## Slide 23 · Browser Code *(5s)* ✦
*[Pause — browser action code on screen]*

## Slide 24 · Verification *(12s)*
"Every action is verified visually. After a tool call, Moonwalk captures the screen and asks Gemini Flash: did this work? If yes, move to the next step. If not, capture what went wrong, replan, and retry."

## Slide 25 · Memory *(14s)*
"Moonwalk remembers you across sessions. Working memory holds current task context. Episodic memory recalls past task outcomes. And semantic memory stores your preferences and patterns — so it gets better the more you use it."

## Slide 26 · Cloud *(12s)*
"The same Python backend can deploy to GCP Cloud Run — containerized and auto-scaling. This unlocks multi-agent collaboration, where multiple Moonwalk instances coordinate on complex workflows."

## Slide 27 · Closing *(8s)*
"Three architecture layers. Eighty tools. An agent that sees, plans, acts, and verifies — all from a small glass pill on your desktop. That's Moonwalk."

---

## Recording Tips

| Aspect       | Guideline                                            |
|-------------|------------------------------------------------------|
| **Pace**     | Conversational, ~150 wpm. Let breathing slides rest. |
| **Tone**     | Confident but warm. Not salesy.                      |
| **Pauses**   | ✦ slides = 4-5s of visual breathing room.            |
| **Emphasis** | Bold key numbers: "eighty tools", "300 milliseconds" |
| **Total**    | ~4 min 30 sec end-to-end                             |
