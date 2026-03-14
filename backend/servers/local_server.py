"""
Moonwalk — Backend Server (Agentic)
=====================================
WebSocket server that:
  1. Receives audio from Electron → detects wake word → transcribes speech
  2. Passes transcribed text to the Agent Loop (perception + planning + tools)
  3. Streams UI state updates back to the Electron overlay
"""

import asyncio
import websockets
import json
import base64
import wave
import io
import struct
import time
import numpy as np
import sys
import os
from functools import partial

from dotenv import load_dotenv

# Force print to flush immediately so Electron gets the logs in real-time
print = partial(print, flush=True)

# Ensure the backend package root is on sys.path so 'agent', 'tools', etc. resolve
# regardless of the working directory (Electron launches with cwd = project root).
_backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

load_dotenv(os.path.join(_backend_dir, ".env"))

# Voice libraries
import pvporcupine
import speech_recognition as sr

# Moonwalk Agent (V2 runtime)
from agent import create_agent
import agent.perception as perception
from servers.browser_bridge_server import BRIDGE_HOST, BRIDGE_PORT, bridge_handler
from browser import BrowserResolver, browser_bridge, browser_store, ActionRequest
from browser.selector_ai import build_ranked_candidates, select_browser_candidate_with_flash

# Agent version toggle (deprecated compatibility env; V2 is always used)
AGENT_VERSION = os.environ.get("MOONWALK_AGENT_VERSION", "v2")

# Picovoice access key (loaded from .env)
PICOVOICE_ACCESS_KEY = os.environ.get("PICOVOICE_ACCESS_KEY", "")

browser_resolver = BrowserResolver()


def _map_user_action_to_text(action: str) -> str:
    normalized = (action or "").strip().lower()
    if not normalized:
        return ""
    if normalized == "approve_plan":
        return "proceed"
    if normalized == "cancel_plan":
        return "cancel"
    return normalized


class VoiceAssistant:
    def __init__(self):
        self.state = "IDLE"  # IDLE, LISTENING, LOADING, DOING
        self.porcupine = None
        self.agent = create_agent(AGENT_VERSION)
        print("[Server] Agent initialized: V2")
        
        # We try to initialize Porcupine, but it will fail if the key is default
        try:
            if PICOVOICE_ACCESS_KEY != "YOUR_PICOVOICE_ACCESS_KEY_HERE":
                # Look for the .ppn file in the project root (one level above _backend_dir)
                project_root = os.path.abspath(os.path.join(_backend_dir, ".."))
                custom_ppn = os.path.join(project_root, "hey_moonwalk.ppn")
                
                if os.path.exists(custom_ppn):
                    self.porcupine = pvporcupine.create(
                        access_key=PICOVOICE_ACCESS_KEY,
                        keyword_paths=[custom_ppn]
                    )
                    print(f"Porcupine initialized with CUSTOM wake word: 'Hey Moonwalk'")
                else:
                    self.porcupine = pvporcupine.create(
                        access_key=PICOVOICE_ACCESS_KEY,
                        keywords=["porcupine"]
                    )
                    print(f"Porcupine initialized with built-in keyword 'Porcupine'.")
                    print("NOTE: To use 'Hey Moonwalk', place 'hey_moonwalk.ppn' in the project root.")
            else:
                print("WARNING: Picovoice Access Key not set. Wake word detection will not work.")
        except Exception as e:
            print(f"Failed to initialize Porcupine: {e}")

        # For capturing the command after wake
        self.audio_buffer = bytearray()
        self.recognizer = sr.Recognizer()
        self.consecutive_silence_chunks = 0
        self.SILENCE_THRESHOLD_CHUNKS = 7  # Approx 0.45 seconds of sustained silence
        self.MIN_BUFFER_SIZE = 16000 * 2 * 0.3  # Min 0.3 seconds of audio
        self.grace_chunks_remaining = 0  # Grace period after await_reply
        self.waiting_for_voice = False  # True = require voice onset before recording
        self.waiting_for_reply = False  # True = do not abort on STT silence

        # Conversation mode: user can talk without saying the wake word each time
        self.conversation_mode = False
        self.conversation_mode_timeout = 120  # seconds of silence before auto-off
        self._conversation_timer = None

    async def run_agent_text(self, websocket, text: str):
        print(f"=> INPUT: {text}")

        async def ws_callback(msg: dict):
            try:
                await websocket.send(json.dumps(msg))
            except Exception as e:
                print(f"[WS Callback] Error sending: {e}")

        context, _ = await asyncio.gather(
            perception.snapshot(text),
            self.agent.router.initialize(),
        )

        result = await self.agent.run(text, context, ws_callback=ws_callback)

        if isinstance(result, tuple):
            response_text, awaiting_reply = result
        else:
            response_text = str(result)
            awaiting_reply = False

        if "[CONVERSATION_MODE_ON]" in (response_text or ""):
            self.conversation_mode = True
            print("[Backend] 🗣 Conversation mode ENABLED")
            await websocket.send(json.dumps({
                "type": "conversation_mode", "enabled": True
            }))
        elif "[CONVERSATION_MODE_OFF]" in (response_text or ""):
            self.conversation_mode = False
            print("[Backend] 🔇 Conversation mode DISABLED")
            await websocket.send(json.dumps({
                "type": "conversation_mode", "enabled": False
            }))

        if awaiting_reply:
            print("[Backend] Agent awaiting reply — listening without wake word")
            self.state = "LISTENING"
            self.audio_buffer = bytearray()
            self.consecutive_silence_chunks = 0
            self.grace_chunks_remaining = 64
            self.waiting_for_voice = True
            self.waiting_for_reply = True
            return

        if self.conversation_mode:
            print("[Backend] 🗣 Conversation mode — listening for next input")
            self.state = "LISTENING"
            self.audio_buffer = bytearray()
            self.consecutive_silence_chunks = 0
            self.grace_chunks_remaining = 48
            self.waiting_for_voice = True
            self.waiting_for_reply = False
            return

        self.waiting_for_reply = False
        self.state = "IDLE"
        self.audio_buffer = bytearray()

    async def handle_audio_chunk(self, websocket, b64_payload):
        """Decode base64 WAV, strip header, get raw PCM bytes."""
        try:
            wav_bytes = base64.b64decode(b64_payload)
            
            # Use Python's wave module to read the WAV chunk
            with wave.open(io.BytesIO(wav_bytes), 'rb') as w:
                pcm_data = w.readframes(w.getnframes())
            
            if self.state == "IDLE":
                await self.process_wake_word(websocket, pcm_data)
            elif self.state == "LISTENING":
                await self.buffer_command(websocket, pcm_data)
                
        except Exception as e:
            print(f"Error processing audio chunk: {e}")

    async def process_wake_word(self, websocket, pcm_data):
        """Feed audio frames into Porcupine to detect wake word."""
        if not self.porcupine:
            return  # Can't detect without the engine

        chunk_size = 1024
        for i in range(0, len(pcm_data), chunk_size):
            chunk = pcm_data[i:i+chunk_size]
            if len(chunk) == chunk_size:
                pcm_tuple = struct.unpack_from("h" * self.porcupine.frame_length, chunk)
                
                keyword_index = self.porcupine.process(pcm_tuple)
                if keyword_index >= 0:
                    print("=> WAKE WORD DETECTED!")
                    self.state = "LISTENING"
                    self.audio_buffer = bytearray()
                    await websocket.send(json.dumps({
                        "type": "status", "state": "state-listening"
                    }))
                    break

    async def buffer_command(self, websocket, pcm_data):
        """Buffer incoming audio while listening, detect silence to stop."""

        # Grace period: discard audio (don't buffer silence before user speaks)
        if self.grace_chunks_remaining > 0:
            self.grace_chunks_remaining -= 1
            return

        # Calculate RMS of the incoming chunk
        ints = np.frombuffer(pcm_data, dtype=np.int16)
        if len(ints) == 0:
            return
        rms = np.sqrt(np.mean(ints.astype(np.float32)**2))

        # Phase 1: Wait for voice onset (after await_reply grace period)
        # Don't buffer until we hear actual speech — avoids capturing
        # silence or quiet system audio (e.g. YouTube playing)
        if self.waiting_for_voice:
            if rms > 1500:  # Voice onset threshold (significantly higher to filter out speaker audio bleed)
                print(f"[Audio] Voice detected (RMS={rms:.0f}), recording...")
                self.waiting_for_voice = False
                self.audio_buffer.extend(pcm_data)
            return  # Skip until voice detected

        # Phase 2: Normal buffering + silence detection
        self.audio_buffer.extend(pcm_data)

        if rms < 250:
            self.consecutive_silence_chunks += 1
        else:
            self.consecutive_silence_chunks = 0

        if len(self.audio_buffer) > self.MIN_BUFFER_SIZE:
            if self.consecutive_silence_chunks >= self.SILENCE_THRESHOLD_CHUNKS:
                print(f"=> SUSTAINED SILENCE ({self.consecutive_silence_chunks} chunks). Processing Command...")
                self.state = "LOADING"
                self.consecutive_silence_chunks = 0
                await websocket.send(json.dumps({
                    "type": "progress", "state": "state-loading"
                }))
                
                # Start processing the audio asynchronously
                asyncio.create_task(self.transcribe_and_act(websocket))

    async def transcribe_and_act(self, websocket):
        """
        Run Speech-to-Text on the buffered audio, then hand off
        to the agentic pipeline.
        """
        print(f"Transcribing {len(self.audio_buffer)} bytes of audio...")
        
        try:
            # Wrap raw PCM buffer into WAV for SpeechRecognition
            wav_io = io.BytesIO()
            with wave.open(wav_io, 'wb') as w:
                w.setnchannels(1)
                w.setsampwidth(2)  # 16-bit
                w.setframerate(16000)
                w.writeframes(self.audio_buffer)
            wav_io.seek(0)

            # Recognize using Google Web Speech API
            with sr.AudioFile(wav_io) as source:
                audio_data = self.recognizer.record(source)
                
            try:
                text = self.recognizer.recognize_google(audio_data)
                print(f"=> TRANSCRIBED: {text}")
                
                # ════════════════════════════════════════════
                #  AGENTIC PIPELINE — This is where the magic happens
                # ════════════════════════════════════════════
                
                await self.run_agent_text(websocket, text)
                if self.state != "IDLE":
                    return
                
            except sr.UnknownValueError:
                print("Google STT could not understand audio")
                # If we were in an await_reply loop or conversation mode, keep listening
                if getattr(self, "waiting_for_reply", False) or self.conversation_mode:
                    print("[Backend] Keeping microphone open (conversation mode or await reply)...")
                    self.state = "LISTENING"
                    self.audio_buffer = bytearray()
                    self.consecutive_silence_chunks = 0
                    self.grace_chunks_remaining = 0
                    self.waiting_for_voice = True
                    await websocket.send(json.dumps({
                        "type": "status", "state": "state-listening"
                    }))
                    return
                # Otherwise, it was a wake word trigger that failed, reset to idle
                await websocket.send(json.dumps({
                    "type": "response",
                    "payload": {"text": "Sorry, I didn't catch that.", "app": ""}
                }))
                
            except sr.RequestError as e:
                print(f"Could not request results from Google; {e}")
                if self.conversation_mode:
                    print("[Backend] Network error but conversation mode on — keeping mic open")
                    self.state = "LISTENING"
                    self.audio_buffer = bytearray()
                    self.consecutive_silence_chunks = 0
                    self.grace_chunks_remaining = 16
                    self.waiting_for_voice = True
                    await websocket.send(json.dumps({
                        "type": "status", "state": "state-listening"
                    }))
                    return
                await websocket.send(json.dumps({
                    "type": "response",
                    "payload": {"text": "Network error processing speech.", "app": ""}
                }))

        except Exception as e:
            print(f"Transcription error: {e}")
            if self.conversation_mode:
                print("[Backend] Error but conversation mode on — keeping mic open")
                self.state = "LISTENING"
                self.audio_buffer = bytearray()
                self.consecutive_silence_chunks = 0
                self.grace_chunks_remaining = 16
                self.waiting_for_voice = True
                try:
                    await websocket.send(json.dumps({
                        "type": "status", "state": "state-listening"
                    }))
                except Exception:
                    pass
                return
            await websocket.send(json.dumps({
                "type": "response",
                "payload": {"text": "Internal error.", "app": ""}
            }))

        # Reset state
        self.state = "IDLE"
        self.audio_buffer = bytearray()


async def main_handler(websocket):
    print("Electron App Connected!")
    assistant = VoiceAssistant()

    # Initialize UI
    try:
        await websocket.send(json.dumps({"type": "status", "state": "state-idle"}))
        
        async for message in websocket:
            try:
                data = json.loads(message)
                msg_type = data.get("type")
                
                if msg_type == "audio_chunk":
                    await assistant.handle_audio_chunk(websocket, data.get("payload", ""))
                elif msg_type == "text_input":
                    text = (data.get("text") or "").strip()
                    if text:
                        assistant.state = "LOADING"
                        await websocket.send(json.dumps({
                            "type": "progress", "state": "state-loading"
                        }))
                        await assistant.run_agent_text(websocket, text)
                elif msg_type == "user_action":
                    mapped_text = _map_user_action_to_text(data.get("action", ""))
                    if mapped_text:
                        assistant.state = "LOADING"
                        await websocket.send(json.dumps({
                            "type": "progress", "state": "state-loading"
                        }))
                        await assistant.run_agent_text(websocket, mapped_text)
                elif msg_type == "browser_debug_action":
                    query = (data.get("query") or "").strip()
                    action = (data.get("action") or "click").strip().lower()
                    session_id = (data.get("session_id") or "").strip()
                    text = data.get("text") or ""
                    option = data.get("option") or ""
                    clear_first = bool(data.get("clear_first", False))
                    timeout = float(data.get("timeout", 8.0) or 8.0)

                    if not query:
                        await websocket.send(json.dumps({
                            "type": "browser_debug_result",
                            "ok": False,
                            "message": "query is required",
                        }))
                        continue

                    snapshot = browser_store.get_snapshot(session_id or None)
                    if not snapshot:
                        await websocket.send(json.dumps({
                            "type": "browser_debug_result",
                            "ok": False,
                            "message": "No active browser snapshot is available.",
                        }))
                        continue

                    candidate = browser_resolver.best_candidate(query, snapshot.elements, action=action)
                    if not candidate:
                        await websocket.send(json.dumps({
                            "type": "browser_debug_result",
                            "ok": False,
                            "message": f"No browser candidate matched query '{query}' for action '{action}'.",
                            "session_id": snapshot.session_id,
                            "generation": snapshot.generation,
                        }))
                        continue

                    request = ActionRequest(
                        action=action,
                        ref_id=candidate.ref_id,
                        session_id=snapshot.session_id,
                        text=text,
                        option=option,
                        clear_first=clear_first,
                        timeout=timeout,
                    )
                    queued = browser_bridge.queue_action(request)
                    if not queued.ok:
                        await websocket.send(json.dumps({
                            "type": "browser_debug_result",
                            "ok": False,
                            "message": queued.message,
                            "query": query,
                            "candidate": {
                                "ref_id": candidate.ref_id,
                                "label": candidate.primary_label(),
                                "role": candidate.role or candidate.tag,
                            },
                        }))
                        continue

                    started = time.time()
                    result = None
                    while time.time() - started < timeout:
                        result = browser_bridge.latest_action_result(queued.action_id)
                        if result is not None:
                            break
                        await asyncio.sleep(0.1)

                    if result is None:
                        await websocket.send(json.dumps({
                            "type": "browser_debug_result",
                            "ok": False,
                            "message": f"Timed out waiting for browser action result for '{query}'.",
                            "query": query,
                            "action": action,
                            "action_id": queued.action_id,
                            "candidate": {
                                "ref_id": candidate.ref_id,
                                "label": candidate.primary_label(),
                                "role": candidate.role or candidate.tag,
                            },
                        }))
                        continue

                    await websocket.send(json.dumps({
                        "type": "browser_debug_result",
                        "ok": result.ok,
                        "message": result.message,
                        "query": query,
                        "action": action,
                        "action_id": result.action_id,
                        "session_id": result.session_id,
                        "pre_generation": result.pre_generation,
                        "post_generation": result.post_generation,
                        "candidate": {
                            "ref_id": candidate.ref_id,
                            "label": candidate.primary_label(),
                            "role": candidate.role or candidate.tag,
                        },
                        "details": result.details,
                    }))
                elif msg_type == "browser_flash_action":
                    query = (data.get("query") or "").strip()
                    action = (data.get("action") or "click").strip().lower()
                    session_id = (data.get("session_id") or "").strip()
                    text = data.get("text") or ""
                    option = data.get("option") or ""
                    clear_first = bool(data.get("clear_first", False))
                    timeout = float(data.get("timeout", 10.0) or 10.0)

                    snapshot, _, snapshot_error = build_ranked_candidates(query, action, session_id=session_id, limit=8)
                    if not snapshot:
                        await websocket.send(json.dumps({
                            "type": "browser_flash_result",
                            "ok": False,
                            "message": snapshot_error or "No active browser snapshot is available.",
                        }))
                        continue

                    selection, error = await select_browser_candidate_with_flash(
                        query=query,
                        action=action,
                        session_id=snapshot.session_id,
                        text=text,
                        option=option,
                    )
                    if not selection:
                        await websocket.send(json.dumps({
                            "type": "browser_flash_result",
                            "ok": False,
                            "message": error,
                            "query": query,
                            "action": action,
                            "session_id": snapshot.session_id,
                            "generation": snapshot.generation,
                        }))
                        continue

                    candidate = browser_store.get_element(selection["ref_id"], snapshot.session_id)
                    if not candidate:
                        await websocket.send(json.dumps({
                            "type": "browser_flash_result",
                            "ok": False,
                            "message": f"Selected ref '{selection['ref_id']}' is no longer present.",
                            "selection": selection,
                        }))
                        continue

                    request = ActionRequest(
                        action=action,
                        ref_id=candidate.ref_id,
                        session_id=snapshot.session_id,
                        text=text,
                        option=option,
                        clear_first=clear_first,
                        timeout=timeout,
                    )
                    queued = browser_bridge.queue_action(request)
                    if not queued.ok:
                        await websocket.send(json.dumps({
                            "type": "browser_flash_result",
                            "ok": False,
                            "message": queued.message,
                            "selection": selection,
                        }))
                        continue

                    started = time.time()
                    result = None
                    while time.time() - started < timeout:
                        result = browser_bridge.latest_action_result(queued.action_id)
                        if result is not None:
                            break
                        await asyncio.sleep(0.1)

                    if result is None:
                        await websocket.send(json.dumps({
                            "type": "browser_flash_result",
                            "ok": False,
                            "message": f"Timed out waiting for Gemini Flash browser action '{query}'.",
                            "action_id": queued.action_id,
                            "selection": selection,
                        }))
                        continue

                    await websocket.send(json.dumps({
                        "type": "browser_flash_result",
                        "ok": result.ok,
                        "message": result.message,
                        "query": query,
                        "action": action,
                        "action_id": result.action_id,
                        "session_id": result.session_id,
                        "pre_generation": result.pre_generation,
                        "post_generation": result.post_generation,
                        "selection": selection,
                        "details": result.details,
                    }))
                    
                elif msg_type == "hotkey_pressed":
                    print("=> HOTKEY PRESSED. Forcing wake...")
                    assistant.state = "LISTENING"
                    assistant.audio_buffer = bytearray()
                    await websocket.send(json.dumps({
                        "type": "status", "state": "state-listening"
                    }))
                    
            except json.JSONDecodeError:
                pass
            except Exception as inner_e:
                print(f"Error handling message: {inner_e}")
                
    except websockets.exceptions.ConnectionClosed as e:
        print(f"Electron disconnected: {e}")
    except Exception as e:
        print(f"Unexpected websocket error: {e}")


async def main():
    if PICOVOICE_ACCESS_KEY == "YOUR_PICOVOICE_ACCESS_KEY_HERE":
        print("!" * 60)
        print("ACTION REQUIRED: You must set PICOVOICE_ACCESS_KEY in ")
        print("backend_server.py to enable the wake word.")
        print("!" * 60)

    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if not gemini_key:
        print("!" * 60)
        print("NOTE: GEMINI_API_KEY not set. Agent will run in fallback mode.")
        print("Set it with: export GEMINI_API_KEY='your-key-here'")
        print("!" * 60)
        
    async with websockets.serve(
        main_handler,
        "127.0.0.1",
        8000,
        origins=None,
        ping_interval=120,
        ping_timeout=600,
    ):
        async with websockets.serve(
            bridge_handler,
            BRIDGE_HOST,
            BRIDGE_PORT,
            origins=None,
            ping_interval=120,
            ping_timeout=600,
        ):
            print("Server running on ws://127.0.0.1:8000 (Allow All Origins)")
            print(f"Browser bridge running on ws://{BRIDGE_HOST}:{BRIDGE_PORT}")
            print("[Backend] READY")
            await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
