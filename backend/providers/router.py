import asyncio
import os
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from functools import partial

print = partial(print, flush=True)

from providers.base import LLMProvider, LLMResponse
from providers.gemini import GeminiProvider


# ═══════════════════════════════════════════════════════════════
#  Routing Tiers
# ═══════════════════════════════════════════════════════════════

class Tier(Enum):
    FAST = 1       # Gemini 3 Flash — cheap router + simple executor
    POWERFUL = 2   # Gemini 3.1 Pro — complex reasoning, multimodal


@dataclass
class RouteDecision:
    """Result of the routing agent's classification."""
    tier: Tier
    provider: LLMProvider
    reason: str
    model_name: str = ""


# ═══════════════════════════════════════════════════════════════
#  Model Config
# ═══════════════════════════════════════════════════════════════

FAST_MODEL = "gemini-3-flash-preview"
POWERFUL_MODEL = "gemini-3.1-pro-preview-customtools"
ROUTING_MODEL = "gemini-2.5-flash"

# The routing prompt that Flash uses to classify requests
ROUTING_PROMPT = """You are a routing classifier for a desktop AI assistant called Moonwalk.

Your job: decide which AI model should handle this user request.

IMPORTANT: Requests are VOICE-TRANSCRIBED. Expect misspellings, homophones, and garbled words.
If a request looks unusual or garbled, it is likely a transcription error — route to POWERFUL so it can
interpret the intent from desktop context. Do NOT route garbled requests to FAST.

## Available Models
- **FAST**: Only for the most trivial, single-step, zero-reasoning OS-level commands where there is no ambiguity and nothing can go wrong. No web browsing, no screen reading, no writing, no multi-step.
- **POWERFUL**: Everything else. Gemini Pro. Handles all real tasks with full intelligence.

## FAST is ONLY appropriate for these exact types of requests:
- Open a named app ("open Spotify", "launch Safari")
- Close / quit / hide / minimise a named window or app
- Set volume or brightness to a number
- Play / pause / skip media
- Lock screen or sleep display
- Take a screenshot
- Simple single-word/number answers ("what is 2+2", "what day is it")

## POWERFUL is required for EVERYTHING else, including:
- Anything involving a web browser (clicking, reading pages, filling forms)
- Reading or analyzing the screen
- Writing emails, messages, documents
- Multi-step tasks (even two steps)
- Research, questions that need reasoning
- Spawning or managing background agents
- Any task that might fail and need a fallback
- Anything requiring judgment about what to do
- Garbled, misspelled, or ambiguous requests (needs context interpretation)
- Pronoun references like "close it", "open that", "do it" (needs context resolution)
- Follow-up replies like "yes", "go ahead", "the second one" (needs conversation history)

## Rules
1. When in doubt → POWERFUL. The cost of routing a simple task to POWERFUL is low. The cost of routing a complex task to FAST is failure.
2. Any task involving a browser or app UI → POWERFUL (shortcuts get blocked, clicks need vision)
3. FAST should be used for maybe 5-10% of requests at most.
4. If the request contains pronouns ("it", "this", "that") or is a follow-up ("yes", "go ahead") → POWERFUL.

Respond with EXACTLY one word: either FAST or POWERFUL"""


_TRIVIAL_APP_OPEN_RE = re.compile(
    r"^\s*(?:open|launch|start|show)\s+(?:the\s+)?([a-z0-9][a-z0-9 .+-]{0,40})\s*$",
    re.IGNORECASE,
)
_TRIVIAL_APP_QUIT_RE = re.compile(
    r"^\s*(?:quit|close|exit|kill|hide|minimise|minimize)\s+(?:the\s+)?([a-z0-9][a-z0-9 .+-]{0,40})\s*$",
    re.IGNORECASE,
)
_TRIVIAL_SYSTEM_RE = re.compile(
    r"^\s*(?:set\s+)?(?:the\s+)?(?:volume|brightness)(?:\s+(?:to|at))?\s+\d{1,3}\s*$",
    re.IGNORECASE,
)
_TRIVIAL_SYSTEM_CONTROL_RE = re.compile(
    r"^\s*(?:mute|unmute|sleep|lock\s+screen|lock\s+(?:the\s+)?screen|volume\s+(?:up|down)|brightness\s+(?:up|down)|turn\s+(?:up|down)\s+(?:the\s+)?(?:volume|brightness))\s*$",
    re.IGNORECASE,
)
_TRIVIAL_MEDIA_RE = re.compile(
    r"^\s*(?:play|pause|resume|skip|next|previous|mute)\s*(?:song|music|track|media)?\s*$",
    re.IGNORECASE,
)
_TRIVIAL_TAB_RE = re.compile(
    r"^\s*(?:new\s+tab|close\s+(?:this\s+)?tab|reopen\s+(?:last\s+)?tab|go\s+back|go\s+forward)\s*$",
    re.IGNORECASE,
)
_TRIVIAL_QUERY_RE = re.compile(
    r"^\s*(?:what\s+(?:time|day)\s+is\s+it|what\s+is\s+2\+?2|time|date)\s*$",
    re.IGNORECASE,
)


# ═══════════════════════════════════════════════════════════════
#  Model Router
# ═══════════════════════════════════════════════════════════════

class ModelRouter:
    """
    Flash-powered routing agent.
    Flash classifies requests and either handles them or escalates to Pro.
    """

    def __init__(self):
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

        self._router: Optional[GeminiProvider] = None
        self._fast: Optional[GeminiProvider] = None
        self._powerful: Optional[GeminiProvider] = None
        self._fallback: Optional[GeminiProvider] = None
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def initialize(self):
        """Lazily init Gemini providers. Thread-safe via asyncio.Lock."""
        if self._initialized:
            return
        async with self._init_lock:
            # Double-check after acquiring lock
            if self._initialized:
                return

            api_key = os.environ.get("GEMINI_API_KEY", "")
            if not api_key:
                print("[Router] ✗ No GEMINI_API_KEY set — agent cannot function.")
                return

            def get_provider(model_name: str) -> GeminiProvider:
                return GeminiProvider(api_key=api_key, model=model_name)

            # Routing model (ultra-cheap, ultra-fast tier 0)
            routing_name = os.environ.get("GEMINI_ROUTING_MODEL", ROUTING_MODEL)
            self._router = get_provider(routing_name)
            if self._router and await self._router.is_available():
                print(f"[Router] ✓ ROUTER (classifier): {routing_name}")
            else:
                print(f"[Router] ✗ ROUTER failed: {routing_name}")
                self._router = None

            # Fast model (cheap, simple tasks)
            fast_name = os.environ.get("GEMINI_FAST_MODEL", FAST_MODEL)
            self._fast = get_provider(fast_name)
            if self._fast and await self._fast.is_available():
                print(f"[Router] ✓ FAST (simple tasks): {fast_name}")
            else:
                print(f"[Router] ✗ FAST failed: {fast_name}")
                self._fast = None

            # Powerful model (complex/multimodal)
            powerful_name = os.environ.get("GEMINI_POWERFUL_MODEL", POWERFUL_MODEL)
            self._powerful = get_provider(powerful_name)
            if self._powerful and await self._powerful.is_available():
                print(f"[Router] ✓ POWERFUL (escalation): {powerful_name}")
            else:
                print(f"[Router] ✗ POWERFUL failed: {powerful_name}")
                self._powerful = None

            # Fallback model (for when primary models fail)
            fallback_name = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-2.5-pro")
            self._fallback = get_provider(fallback_name)
            if self._fallback and await self._fallback.is_available():
                print(f"[Router] ✓ FALLBACK (emergency): {fallback_name}")
            else:
                print(f"[Router] ✗ FALLBACK failed: {fallback_name}")
                self._fallback = None

            # Summary
            print(f"[Router] Ready: Using dedicated routing model")

            # Mark initialized only after everything is set up
            self._initialized = True

    @property
    def fallback(self):
        return self._fallback

    async def route(
        self,
        text: str,
        context_summary: str = "",
        has_screenshot: bool = False,
    ) -> RouteDecision:
        """
        Use the routing model to classify the request.
        """
        await self.initialize()
        start = time.time()

        # If screenshot is attached, always use Pro (needs vision analysis)
        if has_screenshot and self._powerful:
            ms = (time.time() - start) * 1000
            print(f"[Router] → POWERFUL ({ms:.0f}ms): Screenshot attached, needs vision")
            return RouteDecision(
                tier=Tier.POWERFUL,
                provider=self._powerful,
                reason="Screenshot attached → needs vision analysis",
                model_name=self._powerful._model,
            )

        if self._fast and self._looks_trivial_fast_request(text):
            ms = (time.time() - start) * 1000
            print(f"[Router] → FAST ({ms:.0f}ms): Deterministic trivial request")
            return RouteDecision(
                tier=Tier.FAST,
                provider=self._fast,
                reason="Deterministic trivial request → Flash",
                model_name=self._fast._model,
            )

        # Deterministic POWERFUL classification — skip LLM call for obviously complex requests
        if self._powerful and self._looks_obviously_powerful(text):
            ms = (time.time() - start) * 1000
            print(f"[Router] → POWERFUL ({ms:.0f}ms): Deterministic complex request")
            return RouteDecision(
                tier=Tier.POWERFUL,
                provider=self._powerful,
                reason="Deterministic complex request → Pro",
                model_name=self._powerful._model,
            )

        # If only Flash is available, use Flash for everything
        if not self._powerful and self._fast:
            ms = (time.time() - start) * 1000
            print(f"[Router] → FAST ({ms:.0f}ms): Pro unavailable, Flash handles all")
            return RouteDecision(
                tier=Tier.FAST,
                provider=self._fast,
                reason="Pro unavailable → Flash handles everything",
                model_name=self._fast._model,
            )

        # If both available, use routing model to classify
        if self._fast and self._powerful:
            tier = await self._classify_with_router(text, context_summary)
            ms = (time.time() - start) * 1000

            if tier == Tier.POWERFUL:
                print(f"[Router] → POWERFUL ({ms:.0f}ms): Classified as complex")
                return RouteDecision(
                    tier=Tier.POWERFUL,
                    provider=self._powerful,
                    reason="Classified as complex → Pro",
                    model_name=self._powerful._model,
                )
            else:
                print(f"[Router] → FAST ({ms:.0f}ms): Classified as simple/direct action")
                return RouteDecision(
                    tier=Tier.FAST,
                    provider=self._fast,
                    reason="Classified as simple request",
                    model_name=self._fast._model,
                )

        # No models available at all
        raise RuntimeError("No Gemini models available. Set GEMINI_API_KEY in .env")

    def _looks_trivial_fast_request(self, text: str) -> bool:
        normalized = (text or "").strip().lower()
        if not normalized:
            return False

        if any(token in normalized for token in (
            "http://", "https://", "www.", "browser", "tab", "click", "type", "read",
            "research", "compare", "analyze", "analyse", "document", "email", "message",
            "download", "uploads", "folder", "file", "latest", "newest", "most recent",
            " and ", " then ", " after ", " before ", " using ", " with ", " from ", " into ",
        )):
            return False

        if normalized in {"yes", "proceed", "go ahead", "do it", "approved", "start"}:
            return False

        if any(re.search(rf"\b{token}\b", normalized) for token in ("it", "this", "that", "them", "those")):
            return False

        if _TRIVIAL_APP_OPEN_RE.match(normalized):
            return True
        if _TRIVIAL_APP_QUIT_RE.match(normalized):
            return True
        if _TRIVIAL_SYSTEM_RE.match(normalized):
            return True
        if _TRIVIAL_SYSTEM_CONTROL_RE.match(normalized):
            return True
        if _TRIVIAL_MEDIA_RE.match(normalized):
            return True
        if _TRIVIAL_TAB_RE.match(normalized):
            return True
        if _TRIVIAL_QUERY_RE.match(normalized):
            return True
        return False

    def _looks_obviously_powerful(self, text: str) -> bool:
        """Deterministic check for obviously complex requests → skip LLM routing call."""
        normalized = (text or "").strip().lower()
        if not normalized:
            return False
        # Length heuristic: longer requests are almost always complex
        if len(normalized) > 80:
            return True
        # Multi-step indicators
        if any(phrase in normalized for phrase in (
            " and ", " then ", " after that", " also ", " next ",
            " step ", " first ", " second ", " finally ",
        )):
            return True
        # Browser / web tasks
        if any(token in normalized for token in (
            "browser", "click", "http://", "https://", "www.", "webpage",
            "website", "search for", "google", "look up", "navigate to",
            "go to ", "open the page", "fill in", "fill out", "submit",
            "login", "log in", "sign in", "sign up",
        )):
            return True
        # Reading / analysis / writing tasks
        if any(token in normalized for token in (
            "read ", "analyze", "analyse", "summarize", "summarise",
            "research", "compare", "write ", "draft ", "compose",
            "email", "document", "report", "review ",
        )):
            return True
        # Follow-up / context-dependent
        if normalized in {"yes", "proceed", "go ahead", "do it", "approved",
                          "start", "continue", "ok", "sure", "yep", "yeah"}:
            return True
        # Pronoun references need conversation context
        if any(re.search(rf"\b{p}\b", normalized) for p in ("it", "this", "that", "them", "those")):
            return True
        # Questions requiring reasoning
        if any(normalized.startswith(q) for q in (
            "how ", "why ", "what if ", "can you ", "could you ", "would you ",
            "help me ", "find ", "show me ", "tell me about ",
        )):
            return True
        return False

    async def _classify_with_router(self, text: str, context_summary: str) -> Tier:
        """
        Ask Router model to classify: should this be FAST or POWERFUL?
        Uses the provider abstraction rather than direct SDK calls.
        """
        router = self._router or self._fast
        if not router:
            return Tier.POWERFUL if self._powerful else Tier.FAST

        classify_input = f"User request: \"{text}\""
        if context_summary:
            classify_input += f"\nDesktop context: {context_summary}"

        try:
            response = await router.generate(
                messages=[{"role": "user", "parts": [{"text": classify_input}]}],
                system_prompt=ROUTING_PROMPT,
                tools=[],
                temperature=0.0,
            )

            if response and response.text:
                answer = response.text.strip().upper()
                if "POWERFUL" in answer:
                    return Tier.POWERFUL
                return Tier.FAST
        except Exception as e:
            print(f"[Router] Classification error: {e}, defaulting to POWERFUL")

        return Tier.POWERFUL if self._powerful else Tier.FAST

    async def route_and_call(
        self,
        user_message: str,
        system_prompt: str = "",
        force_tier: Optional[str] = None,
        image_data: Optional[bytes] = None,
    ) -> LLMResponse:
        """
        Convenience method: route + generate in one call.
        Used by benchmark judges to evaluate agent outputs.
        """
        await self.initialize()

        # Select provider based on force_tier or routing
        if force_tier == "fast" and self._fast:
            provider = self._fast
        elif force_tier == "powerful" and self._powerful:
            provider = self._powerful
        else:
            decision = await self.route(user_message)
            provider = decision.provider

        # Build messages in the format provider.generate() expects
        messages = [{"role": "user", "parts": [{"text": user_message}]}]

        response = await provider.generate(
            messages=messages,
            system_prompt=system_prompt,
            tools=[],
            image_data=image_data,
        )
        return response

    @property
    def fast(self) -> Optional[GeminiProvider]:
        return self._fast

    @property
    def powerful(self) -> Optional[GeminiProvider]:
        return self._powerful

    def status(self) -> dict:
        """Current router status for debugging."""
        return {
            "fast": self._fast.name if self._fast else "unavailable",
            "powerful": self._powerful.name if self._powerful else "unavailable",
        }
