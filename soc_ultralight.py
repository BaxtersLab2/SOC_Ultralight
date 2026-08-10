#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SOC Ultralight — Agent Message Router + OCR Watcher
====================================================
No vision LLM required. Uses Tesseract OCR for screen reading.

AGENT MESSAGE PROTOCOL
----------------------
Agents structure outgoing messages using this 3-line format:

    To agent1
    "message content here"
    paste then send this now

  Line 1:  routing header  — "to agent1" or "to agent2"
  Middle:  message body    — any text, quotes stripped automatically
  Last:    sentinel phrase — "paste then send this now"
               OCR sees sentinel → knows full message is on screen → sends it

The sentinel prevents partial captures when text is still streaming.
OCR enters rapid mode (0.3s scans) the moment it sees "to agent" on screen,
then fires as soon as the sentinel appears.

FALLBACK: single-line format also supported:  to agent1: message here

THREE PIPELINES
---------------
  A. OCR watcher  — Tesseract reads screen, routes sentinel messages
                    Normal: 1.5s  |  Rapid: 0.3s after "to agent" spotted
  B. File outbox  — polls outbox/agent1/ and outbox/agent2/ for .md files
                    VS Code agent writes file → widget injects + clicks Send
                    Processed files archived to sent/
  C. Manual       — type "to agent1: hello" in widget, press Enter

PER-AGENT CONFIG (hover + countdown):
  • Window handle  — for focus/restore
  • Input field XY — clicked before paste
  • Send button XY — clicked after paste

FAILSAFE: move mouse to top-left corner to stop pyautogui.

Tesseract install: https://github.com/UB-Mannheim/tesseract/wiki
  Default path:    C:\\Program Files\\Tesseract-OCR\\tesseract.exe

--- FUTURE: "Disconnected Hand" (Bing → OCR → local action) ---
Bing chat outputs:  [CMD: write_file outbox/agent1/msg.md Hello agent1]
OCR reads it and executes whitelisted local actions (CMD_ENABLED = False).
Effectively gives a browser-only chat agent reach into the local filesystem
via OCR as the communication channel.
"""

import os
import sys
import ctypes
import tkinter as tk
from tkinter import scrolledtext, messagebox
import threading
import time
import re
import hashlib
import shutil
import json
from collections import OrderedDict
from pathlib import Path
import datetime

import pyperclip
import pyautogui
import mss
_mss_ctor = getattr(mss, 'MSS', None) or getattr(mss, 'mss', None)
from PIL import Image, ImageTk, ImageGrab, ImageEnhance, ImageFilter, ImageOps, ImageStat
import pytesseract

# ── Platform seam (S8) ────────────────────────────────────────────────────────
# ALL OS-coupled desktop calls (window find/focus/rect, cursor, mouse state,
# virtual screen, input hook, instance lock) go through this one backend —
# win32 today, X11 on Linux. See platform_layer/__init__.py for the contract.
from platform_layer import get_platform
PLATFORM = get_platform()

# On X11, pin pyperclip to xclip explicitly: auto-detection picks clip.exe
# under a WSL kernel (the Podman proving ground) and can pick flaky backends;
# xclip is the one we apt-install. Harmless on native Ubuntu.
if PLATFORM.name == "x11":
    try:
        pyperclip.set_clipboard("xclip")
    except Exception:
        pass

# ── Hands guard — Stage 1 of the hands scheduler (FOCUS_SCHEDULER_SPEC.md) ────
# The desktop's mouse/keyboard is a resource shared between the OPERATOR and
# every agent action sequence. OCR (reading) is passive and never gated — only
# the HANDS are. Rule: the operator outranks everything. SOC ledgers its own
# synthetic input; cursor movement it didn't cause = the human → all agent
# hands pause until the human has been idle HANDS_OPERATOR_COOLOFF seconds.
# Implemented as a wrap of pyautogui's input functions at the module boundary,
# so every call site (and all future ones) is covered with zero per-site edits.
# Synthetic input fired from the Tk MAIN thread is operator-initiated (a button
# they clicked) and passes ungated — gating there would freeze the GUI against
# the very human it is yielding to.
HANDS_OPERATOR_COOLOFF = 8.0   # s of human idle before agent hands resume
_HANDS_SYNTH_GRACE     = 1.2   # s after a synthetic burst attributed to SOC
HANDS_TARGET_RADIUS    = 30.0  # px: cursor this close to SOC's own target = SOC's move
_hands_state = {"synthetic_until": 0.0, "operator_until": 0.0, "waits": 0,
                "target": None,   # (x, y) of SOC's last cursor destination
                "estop": False}   # master E-STOP: freeze ALL agent hands
_hands_mutex = threading.Lock()


def _estop_set(active: bool):
    """Master E-STOP: True freezes every synthetic input (agent hands) at the
    pyautogui boundary until released. The eyes (OCR) and loops are gated
    separately by SOCUltralight._estop — this is the hands half."""
    with _hands_mutex:
        _hands_state["estop"] = bool(active)


def _hands_operator_active() -> bool:
    return time.time() < _hands_state["operator_until"]


def _hands_mark_synthetic():
    with _hands_mutex:
        _hands_state["synthetic_until"] = time.time() + _HANDS_SYNTH_GRACE


def _hands_move_is_operator(pos, now=None) -> bool:
    """Attribute one observed cursor movement. SOC's wrapped calls record their
    own cursor TARGET — so during a synthetic burst, movement near that target
    is SOC and movement anywhere else is the OPERATOR. This is deterministic
    (no injected-flag OS support needed) and has no burst-masking race: a human
    wiggle mid-sequence lands off-target and is seen. Pure logic → unit-tested."""
    now = now or time.time()
    if now >= _hands_state["synthetic_until"]:
        return True                       # no synthetic activity → human
    tgt = _hands_state.get("target")
    if not tgt:
        return False                      # in grace, no cursor target (typing) → SOC
    dx, dy = pos[0] - tgt[0], pos[1] - tgt[1]
    return (dx * dx + dy * dy) ** 0.5 > HANDS_TARGET_RADIUS


def _hands_watcher():
    """PRIMARY detector: poll the cursor ~5 Hz and attribute each movement via
    _hands_move_is_operator. Human movement → operator cool-off (agent hands
    pause). Runs for the process lifetime."""
    last = None
    while True:
        try:
            pos = tuple(pyautogui.position())
            if last is not None and pos != last and _hands_move_is_operator(pos):
                with _hands_mutex:
                    _hands_state["operator_until"] = time.time() + HANDS_OPERATOR_COOLOFF
            last = pos
        except Exception:
            pass
        time.sleep(0.2)


def _hands_hook():
    """PRIMARY detector: OS-level injected-vs-hardware input hook (win32
    LL-hooks today; other platforms may return False). Hardware events are the
    OPERATOR, with zero attribution guessing, even mid-SOC-burst. BLOCKING
    (backend runs its own message pump); returns False if installation fails
    (caller starts the fallback). Code moved to platform_layer (S8)."""
    def _mark_operator():
        with _hands_mutex:
            _hands_state["operator_until"] = time.time() + HANDS_OPERATOR_COOLOFF

    # Backend blocks inside install_input_hook on success; flag the hook as
    # live just before handing over (same instant the win32 code set it).
    _hands_state["hook"] = True
    ok = PLATFORM.install_input_hook(_mark_operator)
    if not ok:
        _hands_state["hook"] = False
    return ok


def _hands_start_detector():
    """Run BOTH detectors. The position watcher (target-distance attribution)
    is the dependable primary; the injected-flag hook is best-effort extra
    coverage (it also catches keyboard-only human activity where it works —
    probing showed some environments deliver no LL-hook events at all)."""
    threading.Thread(target=_hands_watcher, daemon=True).start()
    def _try_hook():
        try:
            _hands_hook()
        except Exception:
            pass
    threading.Thread(target=_try_hook, daemon=True).start()


def _hands_wrap(fn):
    """Gate one pyautogui input function behind the operator-yield rule, and
    ledger the cursor TARGET of coordinate calls so the watcher can attribute
    movements (near target = SOC, elsewhere = operator)."""
    def _gated(*args, **kwargs):
        if threading.current_thread() is not threading.main_thread():
            # E-STOP freezes agent hands entirely; operator activity pauses them.
            # Main-thread input (operator-initiated) bypasses both — the human
            # must always be able to act, especially DURING an e-stop.
            while _hands_state["estop"] or _hands_operator_active():
                _hands_state["waits"] += 1
                time.sleep(0.3)
        # Record where WE are sending the cursor (click(x,y), moveTo(x,y), …).
        x = kwargs.get("x", args[0] if len(args) >= 1 else None)
        y = kwargs.get("y", args[1] if len(args) >= 2 else None)
        with _hands_mutex:
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                _hands_state["target"] = (float(x), float(y))
            _hands_state["synthetic_until"] = time.time() + _HANDS_SYNTH_GRACE
        return fn(*args, **kwargs)
    _gated.__name__ = getattr(fn, "__name__", "hands")
    return _gated


for _hands_fn in ("click", "rightClick", "doubleClick", "tripleClick",
                  "moveTo", "moveRel", "dragTo", "dragRel",
                  "typewrite", "write", "hotkey", "press",
                  "keyDown", "keyUp", "mouseDown", "mouseUp", "scroll"):
    if hasattr(pyautogui, _hands_fn):
        setattr(pyautogui, _hands_fn, _hands_wrap(getattr(pyautogui, _hands_fn)))

_hands_start_detector()   # injected-flag hook (primary) → position watcher (fallback)
# ── end hands guard ───────────────────────────────────────────────────────────

# vdd — Parsec Virtual Display Driver control (optional, requires setup_vdd.bat)
try:
    from vdd import VddController as _VddController
    _VDD_OK = True
except ImportError:
    _VDD_OK = False

# opencv-python is optional — enables template matching for auto-calibration
try:
    import cv2
    import numpy as np
    _CV2_OK = True
except ImportError:
    _CV2_OK = False

# ── Tesseract binary ──────────────────────────────────────────────────────────
# shutil.which checks PATH first; falls back to the standard install location
_tess_path = (
    shutil.which("tesseract")
    or r"C:\Program Files\Tesseract-OCR\tesseract.exe"
)
pytesseract.pytesseract.tesseract_cmd = _tess_path

# ── Config ────────────────────────────────────────────────────────────────────
SCAN_NORMAL      = 0.5    # seconds between OCR scans (idle)
SCAN_RAPID       = 0.3    # seconds between scans in rapid mode
AGENT1_COPY_LEAD = 45     # seconds to wait after down-arrow detected before copying agent1
AGENT1_COPY_COOL = 3      # seconds to cool after a failed clipboard read (dedup handles stale)
# Copilot's copy icon is HOVER-REVEALED — it only paints while the cursor dwells
# over the response and needs a beat to become clickable. A single fast/blind
# click fired before it armed and left the clipboard empty (the recurring A1
# stall). These tune the hover-dwell-click-VERIFY-retry copy path.
AGENT1_COPY_DWELL    = 0.35   # seconds to dwell on the icon before clicking (let it arm)
AGENT1_COPY_POLLS    = 7      # clipboard reads (×0.2s ≈ 1.4s) to confirm a copy landed
AGENT1_COPY_NUDGES   = (0, -8, 8, -16, 16)  # vertical px offsets tried around the anchor
AGENT1_OVERFLOW_TIMEOUT   = 15   # secs awaiting an agent1 reply with NO trigger in region → blind scroll+copy
AGENT1_OVERFLOW_MAX_TRIES = 2    # bound the blind scans so they never scroll aimlessly
ROLL_CALL_TURN_TIMEOUT    = 20   # secs to await one agent's SOC-ACK before pinging the next
                                 # (turn-taking so agents sharing a window don't reply at once)
RAPID_DURATION   = 8.0    # seconds to stay rapid after "to agent" spotted
TRIGGER_PERSIST_SECS     = 30.0   # seconds a seen trigger stays remembered after scrolling off
SCROLL_ACCUM_TIMEOUT     = 45.0   # give up scroll-accumulation after this many seconds
SCROLL_ACCUM_MIN_INTERVAL = 0.2   # minimum seconds between accumulation scroll steps
AUTO_HUNT_COOLDOWN       = 25.0   # after a failed sentinel-only trigger hunt, suppress the
                                  # next hunt for this agent this long — the hunt SCROLLS,
                                  # which changes the OCR hash and defeats the tick dedup, so
                                  # without a cooldown it re-fires every tick (infinite scroll
                                  # churn observed live when the bridge went quiet, 2026-07-15)
WAIT_REPLY_TIMEOUT   = 180.0  # seconds before hold state auto-releases (3 min for large blocks)
HOLD_LOG_INTERVAL    = 30.0   # log "holding" at most this often (seconds)
HOLD_SCROLL_INTERVAL = 3.0    # scroll held agent window down every N seconds
SCROLL_GRACE         = 60.0   # seconds to keep scrolling after hold times out
HEARTBEAT_IDLE       = 120.0  # seconds region must be pixel-static → triggers auto-welfare (2 min)
PASTE_DELAY      = 0.25   # seconds after window focus before paste
SEND_DELAY       = 2.0    # seconds after paste before clicking Send
                          # (VS Code/Bing send button only appears after text is entered)
OUTBOX_POLL      = 0.5    # seconds between outbox folder checks
MAX_SEEN_HASHES  = 300    # rolling dedup window
REMINDER_EVERY_AGENT1 = 10   # inject role reminder every N messages sent to Agent 1
REMINDER_EVERY_AGENT2 = 5    # inject role reminder every N messages sent to Agent 2
REMINDER_EVERY_AGENT5 = 5    # inject role reminder every N messages sent to Agent 5
REMINDER_EVERY        = 5    # fallback for agent3 / legacy
SESSION_MAX_AGENT1_MSGS = 20 # Agent-1 messages before recommending a New Session (archive + fresh chat)
TEMPLATE_CAPTURE = 60     # px square crop saved when hover-capturing a target

# ── A4/A5 model-swap "CD changer" (Phase 1: operator-prompted disk swap) ──────
# GGUF Chatbox serves ONE model at a time on its OpenAI proxy. Agent 4 (vision)
# and Agent 5 (writing) each need THEIR "disk" loaded. Before dispatching to A4
# or A5, SOC probes the loaded model; if the wrong disk is in, it beacons the
# operator to swap it and defers the dispatch (Phase 2 will auto-swap via a new
# GGUF Chatbox backend control port). Disk names are matched as case-insensitive
# substrings against the model id the proxy reports — so "qwen2-vl" matches
# "qwen2-vl-7b-instruct-q4.gguf". An empty disk name disables gating for that agent.
CD_PROXY_MODELS_URL = "http://127.0.0.1:8080/v1/models"  # read-only loaded-model probe
CD_PROBE_TTL        = 4.0    # seconds to cache a loaded-disk probe (per-tick checks are cheap)
CD_SWAP_TIMEOUT     = 90.0   # seconds before SOC stops actively nagging about a pending swap
# Automatic CD change via GGUF Chatbox's swap-control endpoint (additive :8086).
CD_SWAP_URL           = "http://127.0.0.1:8086/swap"
CD_SWAP_STATUS_URL    = "http://127.0.0.1:8086/swap/status"
CD_CHAT_CLEAR_URL     = "http://127.0.0.1:8086/chat/clear"  # remote New-Chat (hop hygiene)
# Adaptive per-model guidance: llama-server exposes the loaded model's native
# chat template at /props — we read it to tell a tool-trained model (holds the
# routing envelope) from a plain one (drifts + mangles payloads).
MODEL_PROPS_URL       = "http://127.0.0.1:8080/props"   # proxy (preferred)
MODEL_PROPS_URL_BE    = "http://127.0.0.1:8081/props"   # backend fallback
CD_SWAP_LOAD_TIMEOUT  = 300.0  # max graceful wait for a swapped FP disk to come up
CD_SWAP_POLL_INTERVAL = 3.0    # watcher poll cadence while a swap loads
CD_CHAT_CLEAR_SETTLE  = 2.5    # seconds after /chat/clear before redispatch (chatbox
                               # drains the clear flag on a 1s poll — typing any sooner
                               # could land the new message before the wipe)
GGUF_SETTINGS_FILE = Path.home() / ".gguf-chatbox" / "settings.json"  # magazine disk registry

# A1 (Copilot) stall-breaker. The single most persistent stall is A1 sitting with
# a message it can't copy because the copy button scrolled out of view. This
# watchdog periodically jumps A1 to the bottom to bring the button back — but
# ONLY when the run is actually stuck (nothing routed recently AND no local agent
# is mid-inference), so it never steals focus during a healthy A5/6/7 ping-pong.
A1_STALL_SCROLL_INTERVAL = 60.0   # watchdog wake cadence (seconds)
A1_STALL_SCROLL_AFTER    = 55.0   # only scroll if no successful route in this long
A1_STALL_SCROLL_BURSTS   = 4      # scroll-down clicks per breakout (enough to reach bottom)

# ── Unified local-GPU inference lock ──────────────────────────────────────────
# Agent 4 (vision) and Agent 5 (writing) both run on the local GPU, so only ONE
# may infer at a time. A single lock is claimed SYNCHRONOUSLY at dispatch (closing
# the same-tick race the old asymmetric guard had) and released when the holder
# finishes (derived from its existing busy signal), with a hard timeout so it can
# never deadlock. Cloud agents (A1/A2/A3) don't touch the GPU and are unaffected.
GPU_LOCK_TIMEOUT  = 90.0   # hard safety: force-release a slot held longer than this
GPU_ACQUIRE_GRACE = 8.0    # release if the holder never actually starts inferring in time

BASE_DIR      = Path(__file__).parent
OUTBOX_DIR    = BASE_DIR / "outbox"
TRANSCRIPT_DIR = BASE_DIR / "transcript"  # durable, human-readable inter-agent log

# ── SOC bridge (exact local-agent reply channel) ────────────────────────────
# The local CD-changer agents (A5/A6/A7) all render into the ONE GGUF Chatbox
# window. We used to OCR that window to capture their replies — but OCR misreads
# digits ("Agent7" → "Agent?"), which mis-routes the relay. Instead the chatbox
# backend drops each completed reply as an exact-text file here; SOC reads it
# verbatim (like the A1/A2/A3 outbox), bypassing OCR entirely. SOC writes a
# presence marker the chatbox keeps checking; standalone (no SOC) the chatbox
# feature stays dormant. Shared home-relative path both apps derive independently.
SOC_BRIDGE_DIR       = Path.home() / ".gguf-chatbox" / "soc_bridge"
SOC_BRIDGE_REPLIES   = SOC_BRIDGE_DIR / "replies"
SOC_BRIDGE_PROCESSED = SOC_BRIDGE_REPLIES / "processed"
SOC_BRIDGE_MARKER    = SOC_BRIDGE_DIR / "soc_active.json"
BRIDGE_MARKER_REFRESH = 30.0    # rewrite the marker this often so it stays "fresh"
BRIDGE_POLL           = 0.5     # replies-folder poll interval
BRIDGE_TRUST_WINDOW   = 300.0   # suppress the OCR route this long after the last bridge reply
HEARTBEAT_FILE = BASE_DIR / ".soc_alive"  # liveness ping — transcript monitor self-closes when it stops
SENT_DIR      = BASE_DIR / "sent"
# Agent 2 file-relay: the agent writes its hand-off here as <anything>.md/.txt
# containing the normal "To AgentN … end message now" block. SOC watches this
# folder, size-stabilises each file, then feeds the body through
# _route_text(source_agent="agent2") — identical downstream handling to OCR, but
# with perfect text (no scroll/sentinel/garble).
AGENT2_OUTBOX_DIR = BASE_DIR / "agent2_outbox"
TEMPLATE_DIR  = BASE_DIR / "buttons database"   # drop cropped PNGs here
CONFIG_FILE   = BASE_DIR / "config.json"         # auto-saved coords + window titles

TEMPLATE_THRESH  = 0.80   # minimum match confidence (0-1)
SCROLL_PAUSE     = 0.40   # seconds between scroll steps
SCROLL_MAX_STEPS = 40     # give up after this many scroll clicks
TRAINED_THRESHOLD = 10    # successful matches before a template is "trained"
REGISTRY_FILE = TEMPLATE_DIR / "registry.json"  # per-template match history

AUTOCLICK_SCAN     = 1.5   # seconds between auto-click scans
AUTOCLICK_COOLDOWN = 3.0   # seconds before re-clicking the same button

TRAIN_CAPTURE_W = 150   # px width  — region saved when user clicks during training
TRAIN_CAPTURE_H =  50   # px height — region saved when user clicks during training
TRAIN_TIMEOUT   =  15   # seconds user has to click before training is cancelled

# Template stems containing these substrings are routing infrastructure.
# They are shown as locked (no toggle) in the Auto-Click panel.
AUTOCLICK_LOCKED = ("input_field", "_input", "_send", "send_message", "_scroll")

# Template stems matching these substrings are hardcoded copy-sequence steps.
# They are shown as locked in the Auto-Click panel — not user-toggleable.
AUTOCLICK_SEQUENCE = ("down_arrow", "copy_button", "copy_center")

# Template stems containing these substrings are permanent geo/visual landmarks
# used as backend anchors only (always active, never toggled by the user).
# These are hidden entirely from the Auto-Click panel.
AUTOCLICK_HIDDEN = ("geo_point", "geo_hover", "_landmark", "_scroll_indicator")

for _d in [OUTBOX_DIR / "agent1", OUTBOX_DIR / "agent2", OUTBOX_DIR / "agent3",
           SENT_DIR   / "agent1", SENT_DIR   / "agent2", SENT_DIR   / "agent3",
           AGENT2_OUTBOX_DIR, AGENT2_OUTBOX_DIR / "processed",
           TRANSCRIPT_DIR, TEMPLATE_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

pyautogui.FAILSAFE = True
pyautogui.PAUSE    = 0.1

# ── Implementation mode format reminders ────────────────────────────────────
# Appended to EVERY outgoing message while _mode == "implementation".
# Agent1 drifts extremely fast without per-message reinforcement.

# NOTE: example envelopes inside SOC's own reminders MUST NOT be parseable by
# the router — OCR reads these templates back off the agent's window and, if
# they match SENTINEL_RE/INLINE_RE, SOC routes its own instructions as content
# (observed live 2026-07-13: reminder debris forwarded to A1 instead of the
# real reply). The "<number>" placeholder breaks the regex (no literal digit)
# AND is recipient-AGNOSTIC — the reminder must teach the FORMAT, never dictate
# WHO to reply to (observed 2026-07-15: a hardcoded "Agent[2]" made A1 ping
# Agent2 with a relayed answer). The occupant fills the digit from context.
IMPL_FORMAT_REMINDER_AGENT1 = (
    "\n\n[HARD FORMAT RULE — your reply must use this exact envelope, no exceptions."
    " Replace <number> with the digit of the agent you are replying to — no brackets]\n"
    "To Agent<number>\n"
    "[your message content]\n"
    "end message now\n"
    "ignore edge browser metadata\n"
    "Do not add commentary, planning, or examples. Do not omit the envelope.\n"
    "If this format is not used the workflow will stall."
)

IMPL_FORMAT_REMINDER_AGENT2 = (
    "\n\n[SOC format-note — no reply needed: wrap your response as "
    "To Agent<number> / your content / end message now — replace <number> with the "
    "digit of the agent you are replying to, no brackets]"
)

# Format-envelope reinforcement cadence, per agent slot — tuned to the OCCUPANT's
# capability. A weak model (Bing Copilot, a small local GGUF) drifts off the routing
# envelope within a few messages and needs it reinforced EVERY message; a strong model
# can go sparser. 1 = every message, N = every Nth, 0 = rely on the every-N role
# reminder only. Applied in ALL modes — NOT just implementation mode.
FORMAT_REINFORCE_EVERY = {"agent1": 1, "agent2": 2}


def _local_agent_header(digit: str, tool_capable: bool = True) -> str:
    """Head-guidance for the local CD-changer agents (A5/A6/A7). Prepended to
    EVERY message routed to them: small local models weight the start of the
    prompt most heavily and drift off the envelope without a constant rail.

    ADAPTIVE per the loaded model (the operator's 'envelope is a tool call'
    insight): a tool-trained model holds the routing envelope and gets the lean
    form; a model WITHOUT tool structure in its chat template drifts and mangles
    payloads (observed live: gemma-4B relayed 'solve 2+2' as '3+3'), so it ALSO
    gets an explicit relay-fidelity clause. The clause is harmless to a strong
    model, so 'unknown' safely defaults to including it (tool_capable=False)."""
    fidelity = "" if tool_capable else (
        "\nRELAY FIDELITY: when the message asks you to forward or relay content to\n"
        "another agent, reproduce that content EXACTLY — do NOT solve it, change any\n"
        "number, or reword it. Copy it verbatim."
    )
    # Agent 6 is the swarm's HANDS: it can act on the VSCodium workspace. Give it a
    # tight tool rail on every message (small local models weight the prompt head).
    # The a6-tool fenced block is the ONE thing allowed outside the routing envelope;
    # GGUF Chatbox's proxy runs it and feeds the result back, then A6 replies normally.
    tools = "" if str(digit) != "6" else (
        "\nWORKSPACE TOOL (Agent 6 only): to act on the workspace, output a fenced block\n"
        "tagged a6-tool holding ONE flat JSON object — this block is the ONLY thing\n"
        "allowed outside the envelope. The system runs it and returns the result; then\n"
        "continue, and finish with your normal To Agent<number> reply.\n"
        "```a6-tool\n"
        '{"op": "create_folder", "path": "src/models"}\n'
        "```\n"
        "ops: create_folder, create_file (+\"content\"), read_file, list_dir,\n"
        "add_workspace_folder, remove_workspace_folder, list_workspace_folders."
    )
    return (
        f"[SOC LOOP — you are Agent {digit}. Reply ONLY in this exact format:\n"
        "To Agent<number>\n"
        "<your reply>\n"
        "end message now\n"
        "RULES: First line = To Agent<number> (the digit of the agent you are\n"
        "replying to, no brackets). LAST line MUST be exactly: end message now\n"
        "— never omit it. No commentary outside the envelope."
        f"{fidelity}{tools}]\n\n"
    )


# Chat-template substrings that mark a tool-trained model. Such models were
# fine-tuned to emit structured calls, so they hold the SOC routing envelope
# reliably; a template without any of these is a plain chat model that drifts.
_TOOL_TEMPLATE_MARKERS = (
    "tool_call", "tool_calls", "<tool", "available_tools",
    "function_call", "tool_response", "tojson",
)


def _model_profile(model_path: str, chat_template: str = "",
                   overrides: dict | None = None) -> dict:
    """Classify a model for adaptive guidance. Pure — the live /props read and
    caching live in the SOCUltralight methods, so this is fully unit-testable.

    Precedence: an operator override (substring match on the model name) wins;
    otherwise a tool-trained chat template ⇒ 'strong', anything else ⇒ 'weak'.
    Returns {name, tier, tool_capable}."""
    # Normalize separators so a Windows-written cd_disk path ("C:\m\x.gguf")
    # yields the same model name when the config is read on Linux (POSIX Path
    # doesn't split on backslashes — caught by the Ubuntu-container suite run).
    name = Path((model_path or "").replace("\\", "/")).stem if model_path else ""
    low = name.lower()
    for key, tier in (overrides or {}).items():
        if key and key.lower() in low:
            return {"name": name, "tier": tier, "tool_capable": tier == "strong"}
    tmpl = (chat_template or "").lower()
    tool_capable = any(m in tmpl for m in _TOOL_TEMPLATE_MARKERS)
    return {"name": name, "tier": "strong" if tool_capable else "weak",
            "tool_capable": tool_capable}


def _welfare_due(last_change: float, now: float, route_gap: float,
                 idle_threshold: float, swap_active: bool) -> bool:
    """Whether an auto-welfare (re-sync to A1/A2) should fire. Pure/testable.
    Guards against two false stalls seen live: an UNINITIALIZED region timestamp
    (<=0 = the window was never scanned, e.g. uncalibrated → time.time()-0 reads
    as a ~56-year idle and misfires the 'format guide to A1'), and a CD swap in
    flight (the relay is progressing, not stalled)."""
    if last_change <= 0 or swap_active:
        return False
    return (now - last_change) >= idle_threshold and route_gap >= idle_threshold


def _title_match(saved: str, candidate: str) -> bool:
    """Partial, case-insensitive window-title match — tolerant of tab-name changes
    (a 30-char prefix of either contained in the other). Pure, so the window
    (re)resolution used by auto-locate AND snap-to-grid is unit-testable without
    win32."""
    s = (saved or "").strip().lower()
    c = (candidate or "").strip().lower()
    if not s or not c:
        return False
    return s[:30] in c or c[:30] in s


# ── Per-agent anti-drift recalibration reminders ────────────────────────────
# Injected every REMINDER_EVERY sends to keep each agent on-role.

GROUND_RULES_AGENT1 = (
    "[PROTOCOL RESET — AGENT 1]\n"
    "You are Agent 1. Your ONLY job right now: send the next module block to Agent 2.\n"
    "\n"
    "RULES — NO EXCEPTIONS:\n"
    "1. Your entire response must be ONLY the block, nothing else.\n"
    "2. No preamble. No commentary. No explanation. No sign-off.\n"
    "3. Do NOT write 'start message now'. Do NOT echo Agent 2's confirmation format.\n"
    "4. Do NOT respond conversationally to any system message you receive.\n"
    "5. The module blocks are FINITE. Do not invent additional blocks beyond the\n"
    "   agreed project scope. Every block must map to the approved project summary.\n"
    "\n"
    "FORMAT — copy exactly:\n"
    "To Agent2\n"
    "[block content]\n"
    "end message now\n"
    "\n"
    "WHEN ALL BLOCKS ARE SENT AND AGENT 2 HAS CONFIRMED EACH ONE:\n"
    "Use the mode-switch tool below — copy it exactly, nothing else:\n"
    "\n"
    "To Agent2\n"
    "[SOC:EXECUTE]\n"
    "All instruction blocks have been sent and confirmed by Agent 2.\n"
    "Begin implementation in alphanumeric order now.\n"
    "end message now\n"
    "\n"
    "Do NOT use the words 'implement' or 'execute' anywhere else. [SOC:EXECUTE] is\n"
    "the only authorized way to start implementation. Receive confirmation → send\n"
    "next block. That is your entire role until all blocks are confirmed."
)

GROUND_RULES_AGENT2 = (
    "[PROTOCOL RESET — AGENT 2 — do NOT acknowledge this, just follow it]\n"
    "You are Agent 2. You are NOT a conversational assistant.\n"
    "You do NOT ask questions. You do NOT say 'acknowledged'. "
    "You do NOT offer options or add commentary.\n"
    "\n"
    "THE ONLY PERMITTED RESPONSES ARE:\n"
    "\n"
    "After saving a block:\n"
    "To Agent1\n"
    "module block BLOCK_ID saved, ready for next block\n"
    "end message now\n"
    "\n"
    "If you have a blocker:\n"
    "To Agent1\n"
    "PROBLEM: <one sentence>\n"
    "QUESTION: <what you need>\n"
    "end message now\n"
    "\n"
    "When implementation is complete:\n"
    "To Agent1\n"
    "implementation of instruction blocks is complete\n"
    "end message now\n"
    "\n"
    "HARD RULES — NEVER OVERRIDE:\n"
    "- NEVER run git push, gh pr, or any command that sends code to a remote server.\n"
    "- NEVER deploy, publish, or share project files externally.\n"
    "- If a remote repo URL appears in the project, do NOT push to it.\n"
    "- Before any git commit: delete build artifacts and temp files, then verify\n"
    "  .gitignore exists. Run all tests. Only commit when tests pass.\n"
    "- Pushing is the LAST step, authorized only after user review and 100% test pass.\n"
    "\n"
    "Nothing else. No other output is permitted. Resume your current task now."
)


GROUND_RULES_VSCODE_BRIEF = (
    "[SOC] Agent 3 — ORCHESTRATOR (not implementer).\n"
    "Your role is to delegate, not to execute. Choose the right downstream agent:\n"
    "  • Planning, context, project memory → Agent 1 (Bing Copilot, free tokens)\n"
    "  • Code implementation, file edits, git work → Agent 2 (Claude Code)\n"
    "  • Visual tasks, GUI inspection, screen actions → Agent 4 (vision plugin)\n"
    "Only engage your own intelligence at vital decision points. Do not write\n"
    "code yourself unless explicitly asked by the user.\n\n"
    "Routing format:\n"
    "  To Agent2\n"
    "  <instruction>\n"
    "  end message now\n\n"
    "Wait for the destination agent to reply before sending another message."
)

# Startup briefing written to outbox/agent3/ when VS Code mode activates.
GROUND_RULES_VSCODE_AGENT3 = GROUND_RULES_VSCODE_BRIEF

GROUND_RULES_AGENT5 = (
    "[PROTOCOL RESET — AGENT 5]\n"
    "You are Agent 5 — the uncensored local intelligence agent in this SOC system.\n"
    "You run as a local GGUF model. You provide analysis without built-in content\n"
    "restrictions. Your role is to give a raw, unfiltered perspective that complements\n"
    "the other agents.\n\n"
    "ROUTING FORMAT — use this EXACTLY when addressing the team:\n"
    "  To Agent1\n"
    "  [your findings or analysis]\n"
    "  end message now\n\n"
    "Route to Agent1 (planner/context), Agent2 (implementer), or Agent3 (orchestrator)\n"
    "as appropriate. Do NOT route to Agent4 or Agent5.\n\n"
    "RULES:\n"
    "1. Always close team responses with the routing format above.\n"
    "2. Address only the specific question or mission sent to you.\n"
    "3. Provide uncensored analysis — that is your purpose in this loop.\n"
    "4. Do NOT initiate unprompted. Wait for a mission from another agent.\n"
    "5. Do NOT route to yourself."
)


GROUND_RULES_AGENT6 = (
    "[PROTOCOL RESET — AGENT 6]\n"
    "You are Agent 6 — the local HANDS of this SOC system. You can ACT on the\n"
    "VSCodium workspace: create/read/write files and folders and manage workspace\n"
    "folders. The other agents plan; you carry out the workspace actions they ask for.\n\n"
    "HOW TO ACT — output a fenced block tagged a6-tool containing ONE flat JSON object.\n"
    "The system runs it in VSCodium and hands you back the result; then you continue.\n"
    "This block is the ONLY output allowed outside the routing envelope.\n"
    "  ```a6-tool\n"
    '  {"op": "create_folder", "path": "src/models"}\n'
    "  ```\n"
    "OPERATIONS (one op per block):\n"
    '  create_folder            {"op":"create_folder","path":"..."}\n'
    '  create_file              {"op":"create_file","path":"...","content":"..."}\n'
    '  read_file                {"op":"read_file","path":"..."}\n'
    '  list_dir                 {"op":"list_dir","path":"."}\n'
    '  add_workspace_folder     {"op":"add_workspace_folder","path":"..."}\n'
    '  remove_workspace_folder  {"op":"remove_workspace_folder","path":"..."}\n'
    '  list_workspace_folders   {"op":"list_workspace_folders"}\n'
    "Paths are relative to the workspace; file ops cannot escape it.\n\n"
    "ROUTING FORMAT — after the action(s) are done, report back EXACTLY like this:\n"
    "  To Agent1\n"
    "  [what you did / the result]\n"
    "  end message now\n\n"
    "RULES:\n"
    "1. Use a6-tool blocks ONLY to perform a requested workspace action.\n"
    "2. One operation per block; wait for its result before a dependent next step.\n"
    "3. Always close your team reply with the routing format above.\n"
    "4. Do NOT initiate unprompted — wait for a mission from another agent.\n"
    "5. Route to Agent1 (planner), Agent2 (implementer), or Agent3 (orchestrator);\n"
    "   do NOT route to Agent4/5/6/7, and do NOT route to yourself."
)


#   "message body"
#   end message now
# OCR commonly garbles digits: "1"→l/i/I/!/|, "2"→z/Z, "3"→B/8
# Single-char garble map
_OCR_DIGIT_NORM: dict[str, str] = {
    "l": "1", "i": "1", "I": "1", "!": "1", "|": "1", "t": "1",
    "z": "2", "Z": "2",
    "B": "3", "8": "3",
}
_D = r"[1234567liI!|t]"  # digit-or-garble character class (6/7 = local CD-changer disk agents)

# Multi-char garble pre-normaliser: "Agentt" / "Agentll" → "Agent1"
_AGENT_REF_GARBLE_RE = re.compile(r"(?i)(to\s+agent\s*)([liI!|t]{2,}|[zZ]{2,}|[B8]{2,})")
_EDGE_METADATA_RE = re.compile(
    r"(?i)(edge_all_open_tabs\s*=.*?(?=\n[A-Z]|\Z)|"
    r"#\s*User.s Edge browser tabs.*?(?=\n[A-Z]|\Z)|"
    r"\{\"pageTitle\".*?\}[\],]*)",
    re.DOTALL)

def _preprocess_ocr(text: str) -> str:
    # Strip Edge browser tab-metadata blocks — they change on every tab switch
    # but contain no routing content, so including them in the text hash causes
    # spurious _ocr_process calls without any real message change.
    text = _EDGE_METADATA_RE.sub("", text)
    def _fix(m: re.Match) -> str:
        first = m.group(2)[0].lower()
        digit = "1" if first in "liit|!" else "2" if first in "z" else "3"
        return m.group(1) + digit
    return _AGENT_REF_GARBLE_RE.sub(_fix, text)

def _prepare_img_for_ocr(img: Image.Image) -> Image.Image:
    """Preprocess a screenshot for Tesseract.
    Auto-inverts dark-theme captures so Tesseract always gets dark-on-light.
    2× upscale before processing: screen captures are ~96 DPI but Tesseract
    performs best at 300 DPI — scaling up significantly improves digit accuracy."""
    img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    img = img.convert("L")                           # greyscale
    avg = ImageStat.Stat(img).mean[0]
    if avg < 128:                                    # dark background → invert
        img = ImageOps.invert(img)
    img = ImageEnhance.Contrast(img).enhance(2.0)   # punch up contrast
    img = img.filter(ImageFilter.SHARPEN)            # crisp edges
    return img

SENTINEL_RE = re.compile(
    rf"(?i)to\s+agent\s*({_D})[^\r\n]*[\r\n]+"  # header line — allow trailing chars (colon, markdown, etc.)
    r"(.*?)"                                       # message body (any lines)
    r"\s*end\s+message\s+now",                    # sentinel — \s* instead of [\r\n]+ so no newline required before it
    re.DOTALL)

# Fallback: single-line  "to agent1: message here"
INLINE_RE = re.compile(
    rf"(?i)\bto\s+agent\s*({_D})\s*[:\-]\s*(.+?)(?=\bto\s+agent\s*{_D}\b|$)",
    re.DOTALL)

# Trigger: just seeing "to agent" text → enter rapid mode
TRIGGER_RE = re.compile(rf"(?i)\bto\s+agent\s*{_D}\b")

# Attendance check: agent responds with "SOC-ACK-N" (or "SOC ACK N").
# The prompt says "reply with SOC-ACK followed by your number" — so the digit
# form never appears in the prompt itself, only in the agent's actual reply.
ROLL_CALL_RE = re.compile(r"(?i)\bSOC[\s\-]?ACK[\s\-]?(\d)\b")

# A routed body that is NOTHING BUT ack token(s) is an attendance echo, not
# content — routing it feeds a self-reinforcing ack loop (A2 "saves" it as a
# module block, confirms the ack name back to A1, weak A1 parrots it forever).
_PURE_ACK_RE = re.compile(r"(?i)^\s*(?:SOC[\s\-]?ACK[\s\-]?[\dliI!|t][\s.,;:]*)+$")

# Rate-limit detection: Claude.ai (Agent 3) shows "You've hit your session limit · resets HH:MMpm"
# Extract reset time to dynamically extend hold timeout until quota replenishes.
RATE_LIMIT_RE = re.compile(
    r"(?i)you've?\s+hit\s+your\s+session\s+limit.*?resets\s+(\d{1,2}):(\d{2})\s*(am|pm)\s*(?:\(([^\)]+)\))?",
    re.DOTALL)

# Common OCR garbling variants of the sentinel phrase
_SENTINEL_VARIANTS = (
    "end message now",
    "end rnessage now",
    "end messaqe now",
    "end message now.",
)

# ── Colours (VS Code dark) ────────────────────────────────────────────────────
BG = "#1e1e1e"; BG2 = "#2d2d2d"; FG = "#d4d4d4"
RED = "#e05555"; GREEN = "#4ec994"; ACCENT = "#569cd6"
YELLOW = "#dcdcaa"; ORANGE = "#ce9178"


BING_NOISE_PREFIX = "Ignore Edge browser metadata noise. "

# ── Mode + Anti-Drift system ──────────────────────────────────────────────────
# Phrases that activate implementation mode (checked case-insensitively)
# Deliberate mode-switch command token Agent 1 sends when all blocks are delivered.
# Using a bracketed command token avoids false positives from natural language.
# Agent 1 is taught this token in its every-10-message reminder.
IMPL_TRIGGER_CMD    = "[SOC:EXECUTE]"
IMPL_TRIGGER_PHRASE = (          # kept for Phase 1a template injection
    f"To Agent2\n{IMPL_TRIGGER_CMD}\n"
    "All instruction blocks have been sent and confirmed by Agent 2.\n"
    "Begin implementation in alphanumeric order now.\n"
    "end message now"
)

IMPL_COMPLETE_PHRASE = "implementation of instruction blocks is complete"
MODULE_BLOCK_HEADER  = "<Module Block Mode Active — Do Not Implement Until Authorized>"
ANTIDRIFT_MSG_REM    = "<Reminder: Module Block Mode is active. Do not implement until authorized.>"
ANTIDRIFT_BLOCK_REM  = ("<Anti-Drift Reminder: Continue sending module blocks only. "
                        "Implementation is not permitted.>")
ANTIDRIFT_EVERY      = 10   # every Nth message to Agent1 triggers count-based reminder
IMPL_RUNAWAY_LIMIT   = 3    # implementation attempts before Agent2 HOLD

BLOCK_SAVED_RE  = re.compile(
    r"block\s+\S+\s+saved[.,!;]?\s*[—\-]?\s*ready\s+for\s+(?:the\s+)?next\s+block",
    re.IGNORECASE)
# Guards against Agent 2 drifting into implementation without authorization.
# Does NOT fire when the message contains IMPL_TRIGGER_CMD (the authorized path).
IMPL_ATTEMPT_RE = re.compile(
    r"\b(begin\s+implementation|start\s+implementing|implement\s+now"
    r"|now\s+implement)\b", re.IGNORECASE)

# ── Agent SOP prompts (loaded from .txt files beside this script) ─────────────
def _load_sop(filename: str, fallback: str) -> str:
    p = BASE_DIR / filename
    try:
        return p.read_text(encoding="utf-8").strip()
    except Exception:
        return fallback

AGENT1_SOP = _load_sop(
    "agent1 soc ultralight .txt",
    "You are Agent1. Generate module blocks in alphanumeric order and deliver to Agent2.")
AGENT2_SOP = _load_sop(
    "agent 2 soc ultralight.txt",
    "You are Agent2. Store every module block exactly as received. "
    "Do not implement until Agent1 sends the final block phrase.")
AGENT5_SOP = _load_sop(
    "agent5 soc ultralight.txt",
    GROUND_RULES_AGENT5)
AGENT6_SOP = _load_sop(
    "agent6 soc ultralight.txt",
    GROUND_RULES_AGENT6)

# ── Claude project improvement prompt (Phase 1a, after brainstorm) ────────────
# Copied to clipboard by the "📋 Improvement Prompt" button in Phase 1a.
# User pastes this into their own Claude session, then appends Agent 1's summary.
CLAUDE_IMPROVEMENT_PROMPT = """\
You are a senior technical architect reviewing a project before implementation begins. \
The summary below was produced by a design session and is about to become fixed \
implementation instructions. Before it does, push it through four lenses and return \
an improved version:

1. STACK & ARCHITECTURE — Is anything here already dated or heading toward technical \
debt? Flag it and recommend the cutting-edge alternative. Push toward bleeding-edge \
where the project context supports it without adding unnecessary complexity.

2. SECURITY-FIRST — Identify every surface that creates an attack vector. Redesign \
or harden those points now. Security designed into the architecture costs a fraction \
of security retrofitted after code exists.

3. RAISE THE CEILING — What is the most ambitious realistic version of this project? \
What one or two additions would make it genuinely impressive or differentiated without \
blowing the scope? Name them explicitly.

4. PRECISION — Tighten every ambiguous requirement. Anything an implementing agent \
must guess at will drift into the wrong implementation. Eliminate the guesswork.

Return the complete improved project summary with your changes integrated, followed \
by a 'CHANGES' section: one line per change with the reason. Be direct and opinionated \
— this is not a validation exercise. The goal is the best possible version of this \
project before a single line of code is written.

PROJECT SUMMARY TO IMPROVE:
"""

# ── Phase 2a Security Audit SOP template ──────────────────────────────────────
# Slot tokens replaced at runtime: {workspace} {project} {git_log} {stack}
PHASE2A_SOP_TEMPLATE = """
=== PHASE 2a: SECURITY AUDIT ===

You are acting as a Security Auditor for a locally-built project that has not yet
been pushed to any remote repository. Your job is to review the source code for
security issues, prioritize findings, and work through them with the user until
the app is clean enough to proceed to functional testing (Phase 3).

PROJECT: {project}
WORKSPACE: {workspace}

RECENT GIT LOG:
{git_log}

TECH STACK / NOTES:
{stack}

---

## YOUR AUDIT CHECKLIST

Work through every item below. For each finding, state:
- SEVERITY: Critical / High / Medium / Low
- LOCATION: file path and line number if applicable
- ISSUE: what the problem is
- FIX: what specifically needs to change

### 1. Hardcoded Secrets
Search the codebase for literal API keys, tokens, passwords, connection strings,
and private keys embedded in source files, config files, or test fixtures.
Patterns to grep: common key prefixes (sk-, pk-, Bearer, password=, secret=,
token=, api_key=, Authorization:), long random-looking hex/base64 strings.
Any found = Critical severity.

### 2. Personal Information & Machine Paths
Search for real names, email addresses, phone numbers, and absolute file system
paths that contain usernames (C:\\Users\\..., /home/username/, /Users/name/).
Any found in committed or committable source = High severity.

### 3. .gitignore and .env hygiene
- Verify .gitignore exists in the project root.
- Verify .env (if present) is listed in .gitignore.
- Verify .env.example exists with placeholder values only.
- If .gitignore is missing or incomplete = High severity.

### 4. Input Validation
List every external input surface in the project (HTTP endpoints, CLI arguments,
file reads, IPC, WebSocket, form fields). For each one, confirm validation and
sanitization is present before the data is used. Missing validation on an
external surface = High severity.

### 5. SQL and Query Injection
Verify all database queries use parameterized statements or an ORM with no raw
string interpolation. Any string-interpolated query = Critical severity.

### 6. Authentication & Authorization
Review any auth implementation. Check: tokens/sessions are validated before
protected routes are accessed; passwords are hashed (bcrypt/argon2/scrypt),
never stored in plain text; no auth bypass via parameter manipulation.

### 7. Error Handling & Information Leakage
Confirm error responses do not expose stack traces, internal paths, DB schemas,
or secret values to end users. Debug modes must be off in production config.

### 8. Dependency Audit
List the declared dependencies. Flag any that are known to have security
advisories. If a package manager lock file exists, note whether it is committed.

### 9. Dangerous Function Use
Search for use of eval(), exec(), os.system(), subprocess with shell=True
(without sanitized input), pickle.loads() on untrusted data, or equivalent
in the project's language. Each occurrence with untrusted input = High severity.

### 10. Secrets in Git History
If a .git directory exists, check recent commit diffs for any secrets that may
have been added and removed (they remain in history). Flag if found.

---

## OUTPUT FORMAT

After your audit, present:

CRITICAL FINDINGS (fix before any push):
[list or "None"]

HIGH FINDINGS (fix before app is considered complete):
[list or "None"]

MEDIUM FINDINGS (recommended before release):
[list or "None"]

LOW FINDINGS (best practice improvements):
[list or "None"]

Then work through Critical and High findings with the user, one at a time,
until all are resolved. Re-check each fix before marking it resolved.

When all Critical and High findings are resolved, state:
"Phase 2a security audit complete. No Critical or High findings remain."
"""

# ── Phase 3 Debug SOP template ────────────────────────────────────────────────
# Slot tokens replaced at runtime: {workspace} {project} {git_log} {user_report}
PHASE3_SOP_TEMPLATE = """
=== PHASE 3: DEBUGGING AGENT ===

You are now operating as a Debugging Agent for a SOC Ultralight-built project.
The user has completed Phase 2 (app built and delivered) and needs your help
diagnosing and fixing the parts of the app that are not working correctly.

== YOUR CAPABILITIES ==

You have direct access to:
  Read / Edit / Write / Bash / Grep / Glob  — inspect and modify any project file
  Agent (subagent)                          — delegate parallel research tasks

You also have pc.py — a visual debugging tool in the workspace. Use it via Bash:
  py pc.py screenshot [x0 y0 x1 y1]    capture screen region → snap_screen.png
  py pc.py ocr [agent1|agent2|x y x y] read text from a screen region via OCR
  py pc.py find template.png [thresh]  locate a UI element; returns center x,y
  py pc.py click x y                   left-click at Windows screen coordinates
  py pc.py rclick x y                  right-click
  py pc.py paste "text" x y            click xy then paste text via clipboard
  py pc.py pos                         print current mouse cursor position

After any screenshot, use the Read tool on the saved PNG path to visually inspect it.
Example workflow:
  Bash: py pc.py screenshot
  Read: C:\\path\\to\\snap_screen.png

== PROJECT CONTEXT ==

Workspace : {workspace}
Project   : {project}

Recent commits:
{git_log}

== USER REPORT — WHAT IS NOT WORKING ==

{user_report}

== DEBUGGING WORKFLOW ==

Work through each reported issue in order. For each issue:

  1. OBSERVE    Take a screenshot to see the current app state.
  2. REPRODUCE  Interact with the app to trigger the bug (click, type, etc.).
  3. DIAGNOSE   Read the relevant code — use Grep to find the right function.
  4. HYPOTHESIZE Form a specific, testable theory about the root cause.
  5. FIX        Make the minimal targeted change. Do not touch unrelated code.
  6. VERIFY     Screenshot again to confirm the fix works visually.
  7. NEXT       Move to the next issue and repeat.

== GROUND RULES ==

- Take action autonomously. Do not ask for permission before each step.
- Only pause and message the user when you genuinely cannot proceed alone:
    * You need the user to physically do something (restart the app, click a
      button that requires their credentials, confirm a destructive action).
    * You have tried multiple approaches and are stuck.
- Fix issues one at a time and verify each before moving on.
- If you cannot reproduce an issue, say so and ask the user to demonstrate it.
- Commit working fixes with a clear commit message after each issue is resolved.
- When all reported issues are resolved (or blocked), give the user a clear
  summary: what was fixed, what still needs attention, and suggested next steps.

Begin now: acknowledge the user's report, take a screenshot to see the current
state of the app, and start working through the issue list.
""".strip()

# ── Agent3 outbox response protocol ───────────────────────────────────────────
# Appended to every Agent3 SOP at inject / file-prepare time.
# {outbox_path} is filled in at runtime from config.
AGENT3_OUTBOX_PROTOCOL = """
---
## Response Delivery Protocol (Agent3 → SOC Outbox)

Deliver your complete response via file — do NOT write long content in this chat.

STEPS:
1. Write your full response to:
     {outbox_path}\\[descriptive_name]_to_agent2.md
   Use _to_agent1.md if the response is addressed to Agent1 instead.
   Use a short, meaningful [descriptive_name] (e.g. security_audit, improvement_v1).

2. After the file is written, send this short notification in chat (one line only):
     OUTBOX: [descriptive_name]_to_agent2.md

SOC watches {outbox_path} and will automatically read the file, route it to the
correct agent, and archive the file to {outbox_path}\\processed\\.
Do not include your full response in chat — the file IS the response.
"""

# ── Agent2 outbox awareness note ──────────────────────────────────────────────
# Appended to Agent2 SOP so Agent2 knows Agent3 responses arrive via SOC paste.
AGENT2_OUTBOX_NOTE = """
---
## Receiving Agent3 Responses

When Agent3 (project improver / security auditor) finishes a task it delivers
its response via file. SOC will automatically paste that content into your input.
You do not need to read Agent3's chat window or scroll for a long reply — the
content will arrive here as if typed by SOC. Treat it as any other inbound message.
"""


# ── Self-Modification Safety Gate ─────────────────────────────────────────────
# Hard boundary: SOC must never silently modify its own code or configuration.
# Any routed message whose body references writing/editing/deleting a protected
# path triggers a blocking Tkinter modal asking the user to Approve or Deny.
# Bypasses are NOT permitted — not by Agent 3 authority, not by implementation
# mode, not by any agent. The user is the only authority for self-modification.
SELF_MOD_LOG = BASE_DIR / "data_log" / "self_mod_gate.jsonl"

# Filenames inside SOC_Ultralight/ that are off-limits without user approval.
# Stem-only matches so renames (.py.bak etc.) still catch.
SELF_MOD_PROTECTED_NAMES = (
    "soc_ultralight.py",
    "v_plugin.py",
    "pc.py",
    "calibrate.py",
    "vdd.py",
    "config.json",
    "registry.json",
    "agent1 soc ultralight .txt",
    "agent 2 soc ultralight.txt",
    "agent3 soc ultralight.txt",
)

# Directories inside SOC_Ultralight/ that are off-limits without user approval.
SELF_MOD_PROTECTED_DIRS = (
    "buttons database",
    "instructions",
    "plugins",
    "templates",
)

# Write/edit verb patterns. Match alongside a protected name/dir for a trip.
SELF_MOD_VERBS_RE = re.compile(
    r"\b(write|edit|modify|patch|delete|remove|rm\s+-?rf?|overwrite|replace"
    r"|append\s+to|append_to|rewrite|create\s+file|chmod|mv\s+|move\s+|rename)\b",
    re.IGNORECASE,
)


class SelfModGate:
    """Inspects routed message bodies and prompts the user before SOC modifies
    itself. Constructed once on the SOCU instance. Thread-safe — the modal is
    always raised on the Tk main loop via root.after()."""

    def __init__(self, socu_app):
        self.app = socu_app
        self._session_allow = False   # one-session bypass (user must opt in)
        try:
            SELF_MOD_LOG.parent.mkdir(exist_ok=True)
        except Exception:
            pass

    def _matches_protected(self, body: str) -> list[str]:
        """Return the list of protected paths referenced by `body`."""
        low = body.lower()
        hits = []
        for name in SELF_MOD_PROTECTED_NAMES:
            if name.lower() in low:
                hits.append(name)
        for d in SELF_MOD_PROTECTED_DIRS:
            # Match the dir token as a path-ish segment, not generic prose.
            if (d.lower() + "/") in low or (d.lower() + "\\") in low or \
               (d.lower() + " folder") in low:
                hits.append(d + "/")
        # Treat "soc_ultralight/" or absolute path mentions as broad self-ref.
        if "soc_ultralight" in low and ("/" in low or "\\" in low):
            hits.append("SOC_Ultralight/ (broad)")
        return hits

    def check_and_prompt(self, source_agent: str, dest_agent: str,
                         body: str) -> bool:
        """Return True if routing is allowed, False if blocked.
        Prompts a blocking modal on the Tk main thread if a trip is detected."""
        if not body:
            return True
        hits = self._matches_protected(body)
        if not hits:
            return True
        if not SELF_MOD_VERBS_RE.search(body):
            return True   # mere mention without write verb — allow
        if self._session_allow:
            self._log_decision(source_agent, dest_agent, body, hits,
                               "allowed (session-allow active)")
            return True

        # Raise the modal on the main thread and wait for the user.
        decision = {"value": None}
        done_evt = threading.Event()

        def _show_modal():
            try:
                d = self._make_modal(source_agent, dest_agent, body, hits)
                decision["value"] = d
            finally:
                done_evt.set()

        self.app.root.after(0, _show_modal)
        done_evt.wait(timeout=600)   # 10-min max wait for user decision
        result = decision["value"] or "denied (timeout)"

        if result == "session_allow":
            self._session_allow = True
            self._log_decision(source_agent, dest_agent, body, hits,
                               "session_allow (until restart)")
            return True
        if result == "approved":
            self._log_decision(source_agent, dest_agent, body, hits, "approved")
            return True
        self._log_decision(source_agent, dest_agent, body, hits, result)
        return False

    def _make_modal(self, source_agent: str, dest_agent: str,
                    body: str, hits: list[str]) -> str:
        """Build the Tk modal. Returns 'approved' | 'denied' | 'session_allow'."""
        result = {"value": "denied"}
        modal = tk.Toplevel(self.app.root)
        modal.title("SOC Self-Modification Request")
        modal.configure(bg=BG)
        modal.attributes("-topmost", True)
        modal.transient(self.app.root)
        modal.grab_set()
        try:
            modal.geometry("520x420")
        except Exception:
            pass

        tk.Label(
            modal,
            text="⚠  SOC SELF-MODIFICATION REQUEST",
            bg=BG, fg=RED, font=("Segoe UI", 11, "bold"),
            pady=8,
        ).pack(fill="x")
        tk.Label(
            modal,
            text=("You have just asked SOC to change SOC itself.\n"
                  "This strictly requires user/admin approval."),
            bg=BG, fg=FG, font=("Segoe UI", 9),
            pady=4, justify="left",
        ).pack(fill="x", padx=12)

        info = tk.Frame(modal, bg=BG2)
        info.pack(fill="x", padx=12, pady=6)
        tk.Label(info, text=f"From:   {source_agent}", bg=BG2, fg=YELLOW,
                 font=("Consolas", 9), anchor="w").pack(fill="x", padx=6, pady=(4, 0))
        tk.Label(info, text=f"To:     {dest_agent}", bg=BG2, fg=YELLOW,
                 font=("Consolas", 9), anchor="w").pack(fill="x", padx=6)
        tk.Label(info, text=f"Hits:   {', '.join(hits)}", bg=BG2, fg=ORANGE,
                 font=("Consolas", 9), anchor="w", wraplength=480, justify="left"
                 ).pack(fill="x", padx=6, pady=(0, 4))

        tk.Label(modal, text="Message excerpt:", bg=BG, fg=FG,
                 font=("Segoe UI", 8), anchor="w"
                 ).pack(fill="x", padx=12, pady=(4, 0))
        excerpt = tk.Text(modal, bg=BG2, fg=FG, font=("Consolas", 8),
                          height=10, wrap="word", relief="flat")
        excerpt.pack(fill="both", expand=True, padx=12, pady=4)
        excerpt.insert("1.0", body[:1500] + ("\n…[truncated]" if len(body) > 1500 else ""))
        excerpt.config(state="disabled")

        btns = tk.Frame(modal, bg=BG)
        btns.pack(fill="x", padx=12, pady=8)

        def _close(val):
            result["value"] = val
            try:
                modal.grab_release()
            except Exception:
                pass
            modal.destroy()

        tk.Button(btns, text="Deny", command=lambda: _close("denied"),
                  bg=BG2, fg=RED, font=("Segoe UI", 9, "bold"),
                  relief="flat", cursor="hand2", padx=14, pady=4
                  ).pack(side="right", padx=(4, 0))
        tk.Button(btns, text="Approve Once",
                  command=lambda: _close("approved"),
                  bg=BG2, fg=GREEN, font=("Segoe UI", 9, "bold"),
                  relief="flat", cursor="hand2", padx=14, pady=4
                  ).pack(side="right", padx=(4, 0))
        tk.Button(btns, text="Always Allow This Session  (discouraged)",
                  command=lambda: _close("session_allow"),
                  bg=BG2, fg="#888888", font=("Segoe UI", 8),
                  relief="flat", cursor="hand2", padx=10, pady=4
                  ).pack(side="left")

        modal.protocol("WM_DELETE_WINDOW", lambda: _close("denied"))
        modal.wait_window()
        return result["value"]

    def _log_decision(self, source: str, dest: str, body: str,
                      hits: list[str], decision: str):
        try:
            entry = {
                "ts":       time.strftime("%Y-%m-%dT%H:%M:%S"),
                "source":   source,
                "dest":     dest,
                "hits":     hits,
                "decision": decision,
                "excerpt":  body[:500],
            }
            with open(SELF_MOD_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass


class AgentConfig:
    __slots__ = ("hwnd", "title", "input_xy", "send_xy",
                 "scroll_dn_xy", "scroll_up_xy", "ocr_region",
                 "lbl_window", "lbl_input", "lbl_send", "lbl_scroll", "lbl_region",
                 "lbl_pending", "lbl_pending_dot",
                 "prefix_var", "prefix_enabled", "msg_count")

    def __init__(self):
        self.hwnd:         int | None                    = None
        self.title:        str                           = "(not set)"
        self.input_xy:     tuple[int, int] | None        = None
        self.send_xy:      tuple[int, int] | None        = None
        self.scroll_dn_xy: tuple[int, int] | None        = None
        self.scroll_up_xy: tuple[int, int] | None        = None
        self.ocr_region:   tuple[int,int,int,int] | None = None  # x1,y1,x2,y2
        self.lbl_window    = None
        self.lbl_input     = None
        self.lbl_send      = None
        self.lbl_scroll    = None
        self.lbl_region    = None
        self.lbl_pending     = None   # status text label (idle / pending / routed)
        self.lbl_pending_dot = None   # coloured dot label
        self.prefix_var     = None
        self.prefix_enabled = None
        self.msg_count      = 0


class SOCUltralight:

    def __init__(self, root: tk.Tk):
        self.root = root
        self._drag_x = self._drag_y = 0

        self.agents = {
            "agent1": AgentConfig(),
            "agent2": AgentConfig(),
            "agent3": AgentConfig(),
            "agent5": AgentConfig(),
        }

        self._ocr_running = False
        self._ocr_thread  = None
        self._agent1_scroll_thread = None   # A1 stall-breaker watchdog (lazy start)
        # SOC bridge: exact local-agent reply channel (bypasses OCR digit misreads)
        self._bridge_thread     = None      # replies-folder watcher (lazy start)
        self._bridge_last_seen  = 0.0       # epoch of last exact reply → suppress OCR route
        self._bridge_marker_at  = 0.0       # epoch the presence marker was last refreshed
        self._bridge_seen: dict[str, int] = {}   # filename → size (write-complete stability gate)
        self._rapid_until = 0.0          # epoch time: stay rapid until this
        self._waiting_reply: str | None = None   # agent we just sent to; hold until they reply
        self._waiting_since: float      = 0.0    # epoch time the hold started
        self._agent1_copy_fail_at: float = 0.0  # last time agent1 copy returned empty clipboard
        self._agent1_last_hash: str      = ""    # last OCR hash seen while waiting for agent1
        self._agent1_hash_stable_since: float = 0.0  # when current hash was first seen
        self._agent1_lead_observed: float = 0.0  # last time the 45s lead-time completed
        self._agent1_expect_since: float = 0.0   # epoch of last agent1 inject we await a reply for (overflow handler)
        self._agent1_overflow_tries: int = 0     # bounded blind scroll-to-bottom attempts this expect cycle
        self._agent2_copy_fail_at: float = 0.0  # last time agent2 clipboard copy returned empty
        self._agent2_last_hash: str      = ""    # last OCR hash seen while waiting for agent2
        self._agent2_hash_stable_since: float = 0.0  # when current hash was first seen
        self._agent3_outbox_seen: dict[str, int] = {}  # filename → size at previous poll (stability gate)
        self._last_hold_log: float      = 0.0    # throttle hold log to once per 30s
        self._last_heartbeat_log: float = 0.0   # throttle heartbeat-suppressed log
        self._rate_limited:      dict[str, float] = {}   # agent_id → epoch time when rate limit resets

        self._fw_running  = False
        self._fw_thread   = None
        self._vscode_mode = False   # Copilot+Claude Code mode (outbox + auto-click)
        self._bing_mode   = False   # Agent 1 Edge-browser-aware mode

        self._seen_hashes: OrderedDict[str, None] = OrderedDict()
        self._dedup_lock        = threading.Lock()    # guards _seen_hashes
        self._transcript_lock   = threading.Lock()    # guards transcript file append
        self._transcript_seen: OrderedDict[str, None] = OrderedDict()  # transcript dedup ring
        self._waiting_body_hash: str | None = None    # hash to clear when hold times out
        self._last_scroll:       dict[str, float] = {}   # agent_id → last auto-scroll time
        self._scroll_grace:      dict[str, float] = {}   # agent_id → keep scrolling until this time
        self._last_routed_body:  dict[str, str]  = {}   # agent_id → hash of last body routed to them
        self._last_routed_text:  dict[str, str]  = {}   # agent_id → first line of last body (welfare check)
        self._last_sent_by:      dict[str, str]  = {}   # source_agent → full body it last sent (replay handshake)
        self._last_handoff:      tuple | None = None    # (source, dest, body) of last routed message — replay target
        self._last_route_time:   float = time.time()    # when last successful route happened
        self._welfare_fired:     bool  = False          # True after auto-welfare fires; reset on next successful route
        self._region_frame:      dict[str, str]   = {} # agent_id → pixel-hash of last captured frame
        self._region_last_change:dict[str, float] = {} # agent_id → when region pixels last changed
        self._sentinel_scroll_at:dict[str, float] = {} # agent_id → last time we scrolled seeking sentinel
        self._ocr_blindzone:     tuple | None = None  # (x0,y0,x1,y1) screen region masked from all OCR grabs
        self._last_ocr_text:     dict[str, str]   = {} # agent_id → md5 of last OCR text processed
        self._last_strip_state:  dict[str, tuple]  = {} # agent_id → (has_trigger, has_sentinel) of last strip that triggered full scan
        self._force_scan_active: dict[str, bool]  = {} # agent_id → True while nudge force-scan is running (blocks _ocr_tick)
        self._auto_hunt_cool:    dict[str, float] = {} # agent_id → epoch until sentinel-only auto-hunt may re-fire
        self._inject_grace:      dict[str, float] = {} # agent_id → epoch until OCR routing suppressed
        self._pending_trigger:   dict[str, tuple | None] = {}  # agent_id → (dest_agent, expiry) | None
        self._scroll_accum:      dict[str, str]   = {}  # agent_id → accumulated OCR text across frames
        self._scroll_accum_active: dict[str, bool] = {} # agent_id → True while accumulating
        self._scroll_accum_since:  dict[str, float] = {} # agent_id → epoch when accumulation started
        self._manual_hold:       dict[str, bool] = {"agent1": False, "agent2": False, "agent3": False, "agent5": False}
        self._bypass_agent3:     bool = True   # when True, agent3 is ignored entirely
        self._bypass_agent5:     bool = True   # when True, agent5 (GGUF chatbox) is ignored entirely
        self._disable_vplugin:   bool = False  # when True, v_plugin is not loaded even if file is present
        # Agent 2 read mode: "file" = read the reply from AGENT2_OUTBOX_DIR
        # (robust, no screen needed); "ocr" = legacy screen-scrape path. Default file.
        self._agent2_read_mode:  str  = "file"
        self._agent2_outbox_seen: dict[str, int] = {}  # filename → size at previous poll (stability gate)
        self._attendance:        dict[str, bool] = {"agent1": False, "agent2": False, "agent3": False, "agent4": False, "agent5": False}
        self._paused:            bool = False
        self._collapsed:         bool = False
        self._p1a_workspace:     str  = ""
        self._p1a_source_name:   str  = ""
        self._p1a_source_created:bool = False
        self._p1a_constitution:  str  = ""
        self._p1a_summary_file:  str  = ""
        self._p1a_summary_sent:  bool = False
        self._p1a_template_sent: bool = False
        self._inject_lock  = threading.Lock()    # serialises clipboard writes
        self._click_count  = 0
        self._registry: dict = self._load_registry()  # template training history
        self._vdd_active:      bool = False
        self._vdd_controller               = None

        # Auto-click state
        self._autoclick_vars:    dict[str, tk.BooleanVar] = {}   # stem → BooleanVar (UI only)
        self._autoclick_enabled: set[str]                 = set() # plain set — safe from bg threads
        self._autoclick_last:    dict[str, float]         = {}   # stem → last click epoch
        self._autoclick_images:  list                     = []   # keep PhotoImage refs alive
        self._autoclick_running  = False
        self._autoclick_thread   = None
        self._template_cache:    dict[str, tuple]         = {}   # stem → (mtime, cv2_ndarray)
        self._autoclick_panel_open = False   # collapsed by default
        self._training_stem: str | None = None  # stem currently being trained; None = idle

        self._project_name_var    = tk.StringVar()  # active project name — prepended to every Agent 1 message
        self._agent3_outbox_var   = tk.StringVar()  # path to agent3_outbox folder (user-configured)
        self._agent3_workspace_var = tk.StringVar()  # Agent 3's independent workspace path (post-Anthropic update)

        # ── Mode + anti-drift state ───────────────────────────────────────────
        self._mode                    = "module_block"  # "module_block" | "implementation"
        self._agent1_inbound_count    = 0   # messages delivered to agent1
        self._session_agent1_count    = 0      # Agent-1 msgs THIS session (New-Session prompt)
        self._session_full_flagged    = False  # prompted once per session
        self._session_refresh_pending = False  # mid 2-step refresh (archived, awaiting re-establish)
        self._session_btn             = None   # the ↻ New Session button widget
        self._consecutive_saved_count = 0   # consecutive "Block X saved" messages
        self._agent2_impl_attempts    = 0   # impl attempt intercepts in module_block mode
        self._agent2_hold             = False  # runaway HOLD state
        self._impl_format_count: dict[str, int] = {"agent1": 0, "agent2": 0}  # per-agent impl msg counter

        # ── A4/A5 model-swap "CD changer" (operator-prompted disk swap) ───────
        # Required model-name substring per agent. agent5/6/7 are the local
        # chat-channel disk agents: when their token is empty they auto-map to
        # magazine slot (N-4), i.e. A5→MODEL 1, A6→MODEL 2, A7→MODEL 3.
        self._cd_disk: dict[str, str] = {
            "agent4": "", "agent5": "", "agent6": "", "agent7": ""}
        self._cd_disk_var: dict[str, tk.StringVar] = {
            "agent4": tk.StringVar(), "agent5": tk.StringVar(),
            "agent6": tk.StringVar(), "agent7": tk.StringVar()}
        self._cd_loaded_cache: tuple = (0.0, None)  # (probe_epoch, loaded model id | None)
        # Adaptive per-model guidance: profile cache (model_path → {tier,tool_capable})
        # + operator overrides (name-substring → 'strong'|'weak'). Cleared when the
        # magazine changes so a swapped-in model is re-profiled.
        self._model_profiles_cache: dict = {}
        self._model_profile_overrides: dict = {}
        self._cd_swap_for: str | None = None        # agent we're currently waiting on a swap for
        self._cd_swap_since: float = 0.0            # epoch the current swap prompt was raised
        self._cd_status_lbl = None                  # GUI beacon widget (set in _build_phase2_ui)
        self._estop = False                         # master E-STOP: freezes eyes, hands, outbox, auto-click
        # Automatic CD change (trigger-then-wait): parked messages awaiting a disk load
        self._cd_parked: dict[str, list] = {}       # agent_id -> [(envelope, source_agent), ...]
        self._cd_watchers: set = set()              # agent_ids with an active swap watcher
        self._cd_park_lock = threading.Lock()

        # ── Unified local-GPU inference lock (A4/A5 share the GPU, never both) ──
        self._gpu_holder: str | None = None    # "agent4" | "agent5" | None
        self._gpu_since: float = 0.0
        self._gpu_seen_active = False           # observed the holder actually inferring?
        self._gpu_lock = threading.Lock()

        self._build_window()
        self._build_ui()
        self._update_mode_indicator()              # sync mode bar to initial state
        self._load_config()                        # restore saved coords
        self._self_mod_gate = SelfModGate(self)    # hard boundary for self-edits
        self._vplugin = None                       # optional vision plugin (loaded next)
        self._load_plugins()                       # discover plugins/v_plugin.py etc.
        self.root.after(2000, self._poll_vplugin_file)  # watch for hot-drop-in
        self._soc_alive_tick()                          # heartbeat → transcript monitor exits with SOC
        self.root.after(600, self._gpu_monitor_tick)    # release the A4/A5 GPU lock when its holder finishes
        self.root.after(100, self._fit_window)     # shrink window to content height
        self.root.after(1800, self._startup_calibrate)  # auto-match templates
        threading.Thread(target=self._agent3_relay_loop, daemon=True).start()
        threading.Thread(target=self._soc_control_loop, daemon=True).start()

    # ── Window ────────────────────────────────────────────────────────────────

    def _quit(self):
        try:
            self._save_config()
        except Exception:
            pass
        try:
            HEARTBEAT_FILE.unlink(missing_ok=True)   # signal the transcript monitor to close
        except Exception:
            pass
        try:
            self._bridge_clear_marker()   # let the chatbox bridge return to dormant
        except Exception:
            pass
        # Stop background loops so their daemon threads wind down cleanly.
        self._ocr_running       = False
        self._fw_running        = False
        self._autoclick_running = False
        # Shut down the optional vision plugin (may hold its own threads/sockets).
        try:
            vp = getattr(self, "_vplugin", None)
            if vp is not None and hasattr(vp, "shutdown"):
                vp.shutdown()
        except Exception:
            pass
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass
        # Hard-exit: guarantees the process actually terminates (and releases the
        # single-instance lock) even if a non-daemon thread — plugin, MCP server,
        # tray icon — is still alive. This is what was leaving pyw.exe zombies.
        os._exit(0)

    def _soc_alive_tick(self):
        """Liveness heartbeat for the standalone transcript monitor: while SOC is
        alive this file's mtime keeps updating; a clean _quit deletes it and a crash
        lets it go stale — so the monitor self-closes instead of zombie-ing."""
        try:
            HEARTBEAT_FILE.write_text(str(time.time()), encoding="utf-8")
        except Exception:
            pass
        self.root.after(2000, self._soc_alive_tick)

    def _minimize(self):
        """Collapse the window to the title bar strip. Click again to restore."""
        self._collapsed = not self._collapsed
        if self._collapsed:
            self._body.pack_forget()
            self._min_btn.config(text="□")
        else:
            self._body.pack(fill="x")
            self._min_btn.config(text="—")
        self.root.after(50, self._fit_window)

    def _build_window(self):
        self.root.title("SOC Ultralight")
        # SOC's own mark, set EXPLICITLY on this window rather than left to
        # whichever plugin last called iconbitmap(default=...). The V-plugin's
        # A4v call uses the -default form, which in Tk on Windows sets the
        # process-wide default; without the window-scoped call below, SOC's main
        # window showed the A4v mark (if the plugin loaded) or the bare pythonw
        # icon (if it did not), depending on plugin load order. The default= call
        # claims the app-wide default for SOC so dialogs opened before any plugin
        # loads inherit it too. Best-effort — a missing asset must never stop SOC
        # from opening. (assets/ob_icon.ico is the OUTBOX mark and is unrelated.)
        try:
            _ico = BASE_DIR / "assets" / "soc-ultralight.ico"
            if _ico.exists():
                self.root.iconbitmap(default=str(_ico))
                self.root.iconbitmap(str(_ico))
        except Exception:
            pass
        self.root.attributes("-topmost", True)
        self.root.overrideredirect(True)
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._quit)

        self._win_w = 250
        sw = self.root.winfo_screenwidth()
        # Position only — height will be set by _fit_window after UI is built
        self.root.geometry(f"{self._win_w}x600+{sw - self._win_w - 20}+20")

        self.root.bind("<Button-1>",  self._drag_start)
        self.root.bind("<B1-Motion>", self._drag_move)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self._build_titlebar()
        self._body = tk.Frame(self.root, bg=BG)
        self._body.pack(fill="x")
        self._slide = tk.Frame(self._body, bg=BG)
        self._slide.pack(fill="x")
        self._p1_frame  = tk.Frame(self._slide, bg=BG)
        self._p1a_frame = tk.Frame(self._slide, bg=BG)
        self._p2_frame  = tk.Frame(self._slide, bg=BG)
        self._build_phase1_ui()
        self._build_phase1a_ui()
        self._build_phase2_ui()
        self._build_log_status()
        self._show_phase(1)

    def _build_titlebar(self):
        tb = tk.Frame(self.root, bg=BG2, height=28)
        tb.pack(fill="x")
        tb.bind("<Button-1>",  self._drag_start)
        tb.bind("<B1-Motion>", self._drag_move)
        tk.Label(tb, text="  SOC Ultralight",
                 bg=BG2, fg=FG, font=("Segoe UI", 9, "bold")
                 ).pack(side="left", pady=4)
        tk.Button(tb, text="X", command=self._quit,
                  bg=BG2, fg=FG, relief="flat", font=("Segoe UI", 9, "bold"),
                  activebackground=RED, activeforeground="white",
                  cursor="hand2", bd=0, padx=8).pack(side="right")
        self._min_btn = tk.Button(tb, text="—", command=self._minimize,
                  bg=BG2, fg=FG, relief="flat", font=("Segoe UI", 9, "bold"),
                  activebackground=BG2, activeforeground="white",
                  cursor="hand2", bd=0, padx=8)
        self._min_btn.pack(side="right")
        self._setup_btn = tk.Button(
            tb, text="← Setup", command=lambda: self._show_phase(1),
            bg=BG2, fg=BG2, relief="flat", font=("Segoe UI", 8),
            cursor="hand2", bd=0, padx=6, state="disabled")
        self._setup_btn.pack(side="right", padx=2)

    def _build_phase1_ui(self):
        p = self._p1_frame

        hdr = tk.Frame(p, bg=BG2, pady=4)
        hdr.pack(fill="x")
        self._p1_progress_var = tk.StringVar(value="SETUP — 0/6 required")
        self._p1_progress_lbl = tk.Label(
            hdr, textvariable=self._p1_progress_var,
            bg=BG2, fg=ORANGE, font=("Segoe UI", 9, "bold"))
        self._p1_progress_lbl.pack(side="top", anchor="w", padx=8)
        # Toggle buttons live on their OWN row beneath the progress label so the
        # narrow window grows downward. Packing them side-by-side on the header
        # row pushed them off the right edge of the 250px window (invisible).
        _toggles = tk.Frame(hdr, bg=BG2)
        _toggles.pack(side="top", fill="x", pady=(3, 0))
        _v_lbl = "V:⊘" if self._disable_vplugin else "V:on"
        _v_fg  = "#666666" if self._disable_vplugin else "#4ec9b0"
        self._disable_v_btn = tk.Button(
            _toggles, text=_v_lbl, command=self._toggle_disable_vplugin,
            bg=BG2, fg=_v_fg, font=("Segoe UI", 7, "bold"),
            relief="flat", cursor="hand2", padx=4, pady=0)
        self._disable_v_btn.pack(side="right", padx=6)
        # Agent 2 read-mode toggle: file-relay outbox vs legacy OCR scrape
        self._a2_mode_btn = tk.Button(
            _toggles, command=self._toggle_a2_read_mode,
            bg=BG2, font=("Segoe UI", 7, "bold"),
            relief="flat", cursor="hand2", padx=4, pady=0)
        self._a2_mode_btn.pack(side="right", padx=6)
        self._refresh_a2_mode_button()
        tk.Frame(p, bg=BG2, height=1).pack(fill="x")

        self._build_agent_panel(p, "agent1", "Agent 1")
        tk.Frame(p, bg=BG2, height=1).pack(fill="x", padx=10, pady=2)
        self._build_agent_panel(p, "agent2", "Agent 2")
        tk.Frame(p, bg=BG2, height=1).pack(fill="x", padx=10, pady=2)

        # Agent 3 bypass toggle + panel
        a3_toggle_row = tk.Frame(p, bg=BG, pady=2)
        a3_toggle_row.pack(fill="x", padx=12)
        self._a3_bypass_btn = tk.Button(
            a3_toggle_row, text="⊘ Agent 3  [bypassed]",
            command=self._toggle_bypass_agent3,
            bg=BG2, fg="#666666", font=("Segoe UI", 8),
            relief="flat", cursor="hand2", padx=8, pady=2)
        self._a3_bypass_btn.pack(side="left")

        self._a3_panel_frame = tk.Frame(p, bg=BG)
        # Agent 3 panel starts hidden (bypass on by default)
        self._build_agent_panel(self._a3_panel_frame, "agent3", "Agent 3")
        tk.Frame(p, bg=BG2, height=1).pack(fill="x", padx=10, pady=4)

        # Agent 5 bypass toggle + panel (GGUF Chatbox — uncensored local model)
        a5_toggle_row = tk.Frame(p, bg=BG, pady=2)
        a5_toggle_row.pack(fill="x", padx=12)
        self._a5_bypass_btn = tk.Button(
            a5_toggle_row, text="⊘ Agent 5  [bypassed]  (GGUF Chatbox)",
            command=self._toggle_bypass_agent5,
            bg=BG2, fg="#666666", font=("Segoe UI", 8),
            relief="flat", cursor="hand2", padx=8, pady=2)
        self._a5_bypass_btn.pack(side="left")

        self._a5_panel_frame = tk.Frame(p, bg=BG)
        # Agent 5 panel starts hidden (bypass on by default)
        self._build_agent_panel(self._a5_panel_frame, "agent5", "Agent 5")
        tk.Frame(p, bg=BG2, height=1).pack(fill="x", padx=10, pady=4)

        cal_row = tk.Frame(p, bg=BG, pady=2)
        cal_row.pack(fill="x", padx=12)
        tk.Button(
            cal_row, text="⌖ Auto-Calibrate", command=self._auto_calibrate,
            bg=BG2, fg=YELLOW, font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2", padx=10, pady=4
        ).pack(side="left")
        # Snap-to-Grid moved to the SOC Master Widget (soc_master_widget.py) —
        # the widget can move any top-level window by title, so the whole-desktop
        # align-all lives there now, keeping this panel uncluttered.
        tk.Button(
            cal_row, text="↺ Re-calibrate", command=self._recalibrate,
            bg=BG2, fg=ORANGE, font=("Segoe UI", 8),
            relief="flat", cursor="hand2", padx=6, pady=4
        ).pack(side="left", padx=(4, 0))
        self._blindzone_btn = tk.Button(
            cal_row, text="🚫 Blind Zone", command=self._set_blindzone_mode,
            bg=BG2, fg="#888888", font=("Segoe UI", 8),
            relief="flat", cursor="hand2", padx=6, pady=4)
        self._blindzone_btn.pack(side="left", padx=(4, 0))
        self._smart_cal_btn = tk.Button(
            cal_row, text="◈ Smart Cal",
            command=self._smart_calibrate,
            bg=BG2, fg="#4ec9b0", font=("Segoe UI", 8, "bold"),
            relief="flat", cursor="hand2", padx=6, pady=4)
        # packed by _refresh_smart_cal_button when V plugin is loaded
        self._cal_status_lbl = tk.Label(
            cal_row, text="not run yet",
            bg=BG, fg=FG, font=("Segoe UI", 8, "italic"))
        self._cal_status_lbl.pack(side="left", padx=6)

        tk.Frame(p, bg=BG2, height=1).pack(fill="x", padx=10, pady=4)

        test_row = tk.Frame(p, bg=BG, pady=2)
        test_row.pack(fill="x", padx=12)
        tk.Button(
            test_row, text="⬡ Test Inject", command=self._test_inject,
            bg=BG2, fg=ACCENT, font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2", padx=8, pady=4
        ).pack(side="left")
        tk.Button(
            test_row, text="⬡ Test Round-trip", command=self._test_roundtrip,
            bg=BG2, fg=ACCENT, font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2", padx=8, pady=4
        ).pack(side="left", padx=(4, 0))

        tk.Frame(p, bg=BG2, height=1).pack(fill="x", padx=10, pady=4)

        # Roll call row — attendance check before launch
        rc_row = tk.Frame(p, bg=BG, pady=2)
        rc_row.pack(fill="x", padx=12)
        tk.Button(
            rc_row, text="⬡ Roll Call", command=self._roll_call,
            bg=BG2, fg=YELLOW, font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2", padx=8, pady=4
        ).pack(side="left")

        self._attendance_lbls: dict[str, tk.Label] = {}
        for _aid, _short in [("agent1", "A1"), ("agent2", "A2"), ("agent3", "A3"), ("agent4", "A4"), ("agent5", "A5")]:
            lbl = tk.Label(rc_row, text=f"{_short}:○", bg=BG, fg="#666666",
                           font=("Segoe UI", 8, "bold"))
            lbl.pack(side="left", padx=(8, 0))
            self._attendance_lbls[_aid] = lbl

        self._attendance_status_lbl = tk.Label(
            p, text="Roll call required before launch",
            bg=BG, fg="#666666", font=("Segoe UI", 7, "italic"), anchor="w")
        self._attendance_status_lbl.pack(fill="x", padx=12, pady=(2, 0))

        tk.Frame(p, bg=BG, height=4).pack()
        self._launch_btn = tk.Button(
            p, text="→ Plan Project  (0/6 ready)",
            command=lambda: self._show_phase(2),   # → Phase 1a (project priming)
            bg=BG2, fg="#666666", font=("Segoe UI", 10, "bold"),
            relief="flat", cursor="hand2", pady=6, state="disabled")
        self._launch_btn.pack(fill="x", padx=12, pady=(0, 2))

        self._jumpin_btn = tk.Button(
            p, text="⚡ Jump In  (calibrate first)",
            command=lambda: self._show_phase(3),   # → Phase 2 directly, no Phase 1a
            bg=BG2, fg="#666666", font=("Segoe UI", 9),
            relief="flat", cursor="hand2", pady=4, state="disabled")
        self._jumpin_btn.pack(fill="x", padx=12, pady=(0, 2))

        self._autoaccept_btn = tk.Button(
            p, text="⚡ Auto Accept  (skip calibration)",
            command=self._launch_autoaccept_mode,
            bg="#1a2e1a", fg="#4ec9b0", font=("Segoe UI", 9),
            relief="flat", cursor="hand2", pady=4, state="normal")
        self._autoaccept_btn.pack(fill="x", padx=12, pady=(0, 8))

    def _build_phase1a_ui(self):
        p = self._p1a_frame

        # Header
        hdr = tk.Frame(p, bg=BG2, pady=4)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Project Priming", bg=BG2, fg=YELLOW,
                 font=("Segoe UI", 9, "bold")).pack(side="left", padx=10)
        tk.Label(hdr, text="Phase 1a", bg=BG2, fg="#666666",
                 font=("Segoe UI", 8)).pack(side="right", padx=10)

        # Returning-user bypass bar
        skip_bar = tk.Frame(p, bg="#1a2a1a", pady=3)
        skip_bar.pack(fill="x")
        tk.Label(skip_bar, text="Returning? Phase 1a already done →",
                 bg="#1a2a1a", fg="#888888", font=("Segoe UI", 7)).pack(side="left", padx=8)
        tk.Button(skip_bar, text="Skip to Phase 2 ▶",
                  command=lambda: self._show_phase(3),
                  bg="#1a2a1a", fg=GREEN, font=("Segoe UI", 7, "bold"),
                  relief="flat", cursor="hand2", padx=6, pady=1
                  ).pack(side="right", padx=6)

        tk.Frame(p, bg=BG, height=4).pack()

        # ── Step 1: Workspace ─────────────────────────────────────────
        tk.Label(p, text="1. Set project workspace",
                 bg=BG, fg=FG, font=("Segoe UI", 8, "bold"),
                 anchor="w").pack(fill="x", padx=12, pady=(4, 2))

        ws_row = tk.Frame(p, bg=BG)
        ws_row.pack(fill="x", padx=12)
        tk.Button(ws_row, text="Browse…", command=self._p1a_browse_workspace,
                  bg=BG2, fg=FG, font=("Segoe UI", 8),
                  relief="flat", cursor="hand2", padx=8, pady=2
                  ).pack(side="left")
        self._p1a_ws_lbl = tk.Label(ws_row, text="No workspace selected",
                 bg=BG, fg="#666666", font=("Segoe UI", 7),
                 anchor="w", wraplength=160)
        self._p1a_ws_lbl.pack(side="left", padx=(6, 0), fill="x", expand=True)

        src_row = tk.Frame(p, bg=BG)
        src_row.pack(fill="x", padx=12, pady=(4, 0))
        tk.Label(src_row, text="Source folder:", bg=BG, fg=FG,
                 font=("Segoe UI", 8)).pack(side="left")
        self._p1a_src_var = tk.StringVar()
        tk.Entry(src_row, textvariable=self._p1a_src_var, width=14,
                 bg="#2d2d2d", fg=FG, insertbackground=FG,
                 relief="flat", font=("Segoe UI", 8)).pack(side="left", padx=(4, 4))
        self._p1a_src_btn = tk.Button(src_row, text="Create",
                  command=self._p1a_create_source,
                  bg=BG2, fg="#666666", font=("Segoe UI", 8),
                  relief="flat", cursor="hand2", padx=6, pady=1, state="disabled")
        self._p1a_src_btn.pack(side="left")
        self._p1a_src_status = tk.Label(src_row, text="", bg=BG,
                 fg=GREEN, font=("Segoe UI", 8))
        self._p1a_src_status.pack(side="left", padx=(4, 0))

        tk.Frame(p, bg="#333333", height=1).pack(fill="x", padx=12, pady=6)

        # ── Step 2: Constitution ──────────────────────────────────────
        tk.Label(p, text="2. Constitution folder (agent rules & constraints)",
                 bg=BG, fg=FG, font=("Segoe UI", 8, "bold"),
                 anchor="w").pack(fill="x", padx=12, pady=(0, 2))

        con_row = tk.Frame(p, bg=BG)
        con_row.pack(fill="x", padx=12)
        tk.Button(con_row, text="Browse existing…",
                  command=self._p1a_browse_constitution,
                  bg=BG2, fg=FG, font=("Segoe UI", 8),
                  relief="flat", cursor="hand2", padx=8, pady=2
                  ).pack(side="left", padx=(0, 4))
        tk.Button(con_row, text="Use SOC template",
                  command=self._p1a_copy_constitution_template,
                  bg=BG2, fg=ACCENT, font=("Segoe UI", 8),
                  relief="flat", cursor="hand2", padx=8, pady=2
                  ).pack(side="left")

        self._p1a_con_lbl = tk.Label(p, text="No constitution folder set",
                 bg=BG, fg="#666666", font=("Segoe UI", 7),
                 anchor="w", wraplength=230)
        self._p1a_con_lbl.pack(fill="x", padx=12, pady=(2, 0))

        tk.Frame(p, bg="#333333", height=1).pack(fill="x", padx=12, pady=6)

        # ── Step 3: Project summary ───────────────────────────────────
        tk.Label(p, text="3. Load project summary into Agent 1",
                 bg=BG, fg=FG, font=("Segoe UI", 8, "bold"),
                 anchor="w").pack(fill="x", padx=12, pady=(0, 2))

        sum_row = tk.Frame(p, bg=BG)
        sum_row.pack(fill="x", padx=12)
        tk.Button(sum_row, text="▶ Brainstorm", command=self._p1a_brainstorm,
                  bg=BG2, fg=ACCENT, font=("Segoe UI", 8),
                  relief="flat", cursor="hand2", padx=8, pady=2
                  ).pack(side="left", padx=(0, 4))
        tk.Button(sum_row, text="Browse…", command=self._p1a_browse,
                  bg=BG2, fg=FG, font=("Segoe UI", 8),
                  relief="flat", cursor="hand2", padx=8, pady=2
                  ).pack(side="left")

        self._p1a_file_lbl = tk.Label(p, text="No file selected",
                 bg=BG, fg="#666666", font=("Segoe UI", 7),
                 anchor="w", wraplength=230)
        self._p1a_file_lbl.pack(fill="x", padx=12, pady=(2, 0))

        self._p1a_inject_btn = tk.Button(
            p, text="→ Inject Summary into Agent 1",
            command=self._p1a_inject_summary,
            bg=BG2, fg=FG, font=("Segoe UI", 8),
            relief="flat", cursor="hand2", pady=3, state="disabled")
        self._p1a_inject_btn.pack(fill="x", padx=12, pady=(4, 0))

        # ── Step 3b: Claude improvement ───────────────────────────────
        tk.Frame(p, bg="#333333", height=1).pack(fill="x", padx=12, pady=(6, 2))
        tk.Label(p, text="3b. Improve summary with Claude (recommended)",
                 bg=BG, fg=FG, font=("Segoe UI", 8, "bold"),
                 anchor="w").pack(fill="x", padx=12, pady=(0, 2))
        tk.Label(p,
                 text="Paste Agent 1's summary into Claude via Agent 2's window.\n"
                      "Claude returns an improved version directly to Agent 1.",
                 bg=BG, fg="#888888", font=("Segoe UI", 7),
                 anchor="w", wraplength=240, justify="left"
                 ).pack(fill="x", padx=12)
        tk.Button(
            p, text="✨  Improve with Claude",
            command=self._p1a_improve_with_claude,
            bg="#1a2a1a", fg="#4ec9b0",
            font=("Segoe UI", 8, "bold"),
            relief="flat", cursor="hand2", pady=3
        ).pack(fill="x", padx=12, pady=(4, 0))

        self._p1a_sum_ready_btn = tk.Button(
            p, text="✓ Summary Ready",
            command=self._p1a_toggle_summary_ready,
            bg=BG2, fg="#666666", font=("Segoe UI", 8),
            relief="flat", cursor="hand2", pady=3)
        self._p1a_sum_ready_btn.pack(fill="x", padx=12, pady=(4, 0))

        tk.Frame(p, bg="#333333", height=1).pack(fill="x", padx=12, pady=6)

        # ── Step 4: Template ──────────────────────────────────────────
        tk.Label(p, text="4. Send module block template to agents",
                 bg=BG, fg=FG, font=("Segoe UI", 8, "bold"),
                 anchor="w").pack(fill="x", padx=12, pady=(0, 2))

        self._p1a_tmpl_btn = tk.Button(
            p, text="→ Send Template to Agent 1",
            command=self._p1a_send_template,
            bg=BG2, fg="#666666", font=("Segoe UI", 8),
            relief="flat", cursor="hand2", pady=3, state="disabled")
        self._p1a_tmpl_btn.pack(fill="x", padx=12, pady=(0, 6))

        # ── Advance ──────────────────────────────────────────────────
        tk.Frame(p, bg=BG, height=2).pack()
        self._p1a_advance_btn = tk.Button(
            p, text="→ Begin Workflow",
            command=lambda: self._show_phase(3),
            bg=BG2, fg="#666666", font=("Segoe UI", 10, "bold"),
            relief="flat", cursor="hand2", pady=6, state="disabled")
        self._p1a_advance_btn.pack(fill="x", padx=12, pady=(0, 8))

    def _p1a_browse_workspace(self):
        import tkinter.filedialog as fd
        path = fd.askdirectory(title="Select project workspace folder")
        if not path:
            return
        self._p1a_workspace = path
        short = os.path.basename(path) or path
        self._p1a_ws_lbl.config(text=short, fg=FG)
        self._p1a_src_btn.config(state="normal", fg=FG)
        self._log(f"[priming] workspace set: {path}")
        self._p1a_check_advance()

    def _p1a_create_source(self):
        name = self._p1a_src_var.get().strip()
        if not name:
            self._log("[priming] enter a source folder name first")
            return
        if not self._p1a_workspace:
            self._log("[priming] set workspace first")
            return
        full = os.path.join(self._p1a_workspace, name)
        try:
            os.makedirs(full, exist_ok=True)
        except Exception as e:
            self._log(f"[priming] could not create source folder: {e}")
            return
        self._p1a_source_name = name
        self._p1a_source_created = True
        self._p1a_src_status.config(text="✓")
        self._p1a_src_btn.config(bg=GREEN, fg="white")
        self._log(f"[priming] source folder created: {full}")
        self._p1a_check_advance()

    def _p1a_browse_constitution(self):
        import tkinter.filedialog as fd
        path = fd.askdirectory(title="Select existing constitution folder")
        if not path:
            return
        self._p1a_constitution = path
        self._p1a_con_lbl.config(text=os.path.basename(path) or path, fg=GREEN)
        self._log(f"[priming] constitution folder set: {path}")
        self._p1a_check_advance()

    def _p1a_copy_constitution_template(self):
        if not self._p1a_workspace:
            self._log("[priming] set workspace before copying constitution template")
            return
        import shutil
        src = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "templates", "constitution_template")
        dst = os.path.join(self._p1a_workspace, "CONSTITUTION")
        try:
            shutil.copytree(src, dst, dirs_exist_ok=True)
        except Exception as e:
            self._log(f"[priming] could not copy template: {e}")
            return
        self._p1a_constitution = dst
        self._p1a_con_lbl.config(text="CONSTITUTION (SOC template)", fg=GREEN)
        self._log(f"[priming] constitution template copied to {dst}")
        self._p1a_check_advance()

    def _p1a_browse(self):
        import tkinter.filedialog as fd
        path = fd.askopenfilename(
            title="Select project summary",
            filetypes=[("Text / Markdown", "*.md *.txt"), ("All files", "*.*")])
        if not path:
            return
        self._p1a_summary_file = path
        self._p1a_file_lbl.config(text=os.path.basename(path), fg=FG)
        self._p1a_inject_btn.config(state="normal", fg=ACCENT)

    def _p1a_brainstorm(self):
        starter = (
            "We are opening a project design session. The user will describe their idea "
            "first. Your job:\n\n"
            "1. Invite the user to describe what they want to build.\n"
            "2. Listen to their description.\n"
            "3. Identify which of the nine required areas below are still undefined, "
            "unclear, or need more detail after hearing the description.\n"
            "4. Ask targeted questions ONE AT A TIME — one gap, one question — until "
            "every area is fully defined. Do not ask about multiple gaps at once.\n"
            "5. When every area is covered and the user confirms they are satisfied, "
            "write the complete PROJECT SUMMARY document.\n\n"
            "THE NINE AREAS THAT MUST ALL BE DEFINED:\n\n"
            "1. PROJECT NAME — short name for file naming and tracking.\n"
            "2. PURPOSE — what it does, what problem it solves, why it exists.\n"
            "3. CORE FEATURES — major functional components, one per line, "
            "behaviour not code.\n"
            "4. TECHNICAL STACK — language, framework, key libraries, target "
            "platform(s), build system. Specific — version numbers where relevant.\n"
            "5. SECURITY REQUIREMENTS (mandatory — never skip):\n"
            "   - Authentication model (none / API key / OAuth / session token)\n"
            "   - External services and what credentials each needs\n"
            "   - Sensitive data handled (none / PII / financial / health / credentials)\n"
            "   - Input surfaces and required validation at each\n"
            "   - Behaviour on auth failure or invalid input\n"
            "   - Compliance requirements (none / GDPR / HIPAA / other)\n"
            "6. FOLDER / WORKSPACE LAYOUT — directory and package structure "
            "if known; flag for Module A if not.\n"
            "7. EXTERNAL DEPENDENCIES & INTEGRATION POINTS — other apps, "
            "services, or hardware; interface used; data flow in each direction.\n"
            "8. CONSTRAINTS AND DESIGN DECISIONS — hard limits the implementing "
            "agent must not deviate from.\n"
            "9. SAVE PATH FOR BLOCK FILES — where on this machine instruction "
            "block files will be saved.\n\n"
            "Start now: ask the user to tell you about the project they want to build."
        )
        threading.Thread(
            target=lambda: self._inject_to_agent("agent1", starter),
            daemon=True).start()
        self._p1a_sum_ready_btn.config(fg=FG)
        self._log("[priming] brainstorm prompt sent to Agent 1")

    def _p1a_inject_summary(self):
        if not self._p1a_summary_file:
            return
        try:
            with open(self._p1a_summary_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
        except Exception as e:
            self._log(f"[priming] could not read file: {e}")
            return
        msg = (
            "Here is an existing project summary. Read it fully, then check it "
            "against the nine required areas below. If any area is missing, "
            "unclear, or needs more detail, ask me about it — one gap, one "
            "question at a time. Only ask about what is actually missing; do not "
            "re-ask about areas that are already well defined. When every area is "
            "covered and I confirm I am satisfied, present the completed summary.\n\n"
            "THE NINE REQUIRED AREAS:\n"
            "1. Project name\n"
            "2. Purpose — what it does and why\n"
            "3. Core features — major components, behaviour not code\n"
            "4. Technical stack — language, framework, libraries, platform, build system\n"
            "5. Security requirements — auth model, external credentials, sensitive data, "
            "input surfaces, failure behaviour, compliance\n"
            "6. Folder / workspace layout\n"
            "7. External dependencies and integration points\n"
            "8. Constraints and design decisions\n"
            "9. Save path for block files\n\n"
            "PROJECT SUMMARY:\n\n"
            f"{content}"
        )
        threading.Thread(
            target=lambda: self._inject_to_agent("agent1", msg),
            daemon=True).start()
        self._p1a_sum_ready_btn.config(fg=FG)
        self._log(f"[priming] project summary injected to Agent 1 ({len(content)} chars)")

    def _p1a_improve_with_claude(self):
        """Open the Claude improvement dialog.
        User pastes Agent 1's completed summary; SOC prepends the improvement
        prompt + routing-format instruction and injects into Agent 2 (Claude).
        Claude responds 'To Agent1 / improved summary / end message now' and
        SOC routes it back to Agent 1 automatically via the normal OCR loop."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Improve Summary with Claude")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.grab_set()

        tk.Label(dlg, text="✨  Improve with Claude",
                 bg=BG, fg="#4ec9b0", font=("Segoe UI", 11, "bold"),
                 pady=8).pack(fill="x", padx=16)

        tk.Label(dlg,
                 text="Paste Agent 1's completed project summary below.\n"
                      "SOC will send it to Claude (Agent 2 window) with the\n"
                      "improvement prompt. Claude's improved version routes\n"
                      "back to Agent 1 automatically.",
                 bg=BG, fg=FG, font=("Segoe UI", 8), justify="left",
                 wraplength=360).pack(anchor="w", padx=16)

        txt = tk.Text(dlg, width=52, height=16,
                      bg=BG2, fg=FG, insertbackground=FG,
                      font=("Consolas", 8), relief="flat",
                      padx=6, pady=6, wrap="word")
        txt.pack(fill="both", padx=16, pady=(6, 0))
        txt.insert("1.0", "(paste Agent 1's project summary here)")
        txt.bind("<FocusIn>", lambda e: txt.delete("1.0", "end")
                 if txt.get("1.0", "end").strip().startswith("(paste") else None)
        txt.focus_set()

        status_lbl = tk.Label(dlg, text="", bg=BG, fg=GREEN,
                              font=("Segoe UI", 8, "italic"))
        status_lbl.pack(padx=16, pady=(4, 0))

        def _send():
            summary = txt.get("1.0", "end").strip()
            if not summary or summary.startswith("(paste"):
                status_lbl.config(
                    text="Paste Agent 1's summary first.", fg=ORANGE)
                return
            if not self.agents["agent2"].hwnd:
                status_lbl.config(
                    text="Agent 2 window not set — click Set Win first.", fg=ORANGE)
                return

            # Full prompt: improvement brief + routing format instruction + summary
            full_prompt = (
                CLAUDE_IMPROVEMENT_PROMPT
                + summary
                + "\n\n---\n"
                "Respond in EXACTLY this format — no text before or after:\n\n"
                "To Agent1\n"
                "[full improved project summary]\n\n"
                "CHANGES:\n"
                "- [change 1]: [reason]\n"
                "- [change 2]: [reason]\n"
                "end message now"
            )
            status_lbl.config(
                text="Sending to Claude... OCR will route reply to Agent 1.", fg=GREEN)
            self._log("[priming] improvement prompt sent to Agent 2 (Claude)")
            threading.Thread(
                target=lambda: self._inject_to_agent(
                    "agent2", full_prompt, bypass_mode_check=True),
                daemon=True).start()
            dlg.after(1500, dlg.destroy)

        btn_row = tk.Frame(dlg, bg=BG)
        btn_row.pack(fill="x", padx=16, pady=(8, 12))
        tk.Button(
            btn_row, text="Send to Claude",
            command=_send,
            bg="#1a2a1a", fg="#4ec9b0",
            font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2",
            padx=12, pady=5
        ).pack(side="left")
        tk.Button(
            btn_row, text="Cancel",
            command=dlg.destroy,
            bg=BG2, fg=FG,
            font=("Segoe UI", 8),
            relief="flat", cursor="hand2",
            padx=10, pady=5
        ).pack(side="right")

    def _p1a_toggle_summary_ready(self):
        self._p1a_summary_sent = not self._p1a_summary_sent
        if self._p1a_summary_sent:
            self._p1a_sum_ready_btn.config(bg=GREEN, fg="white", text="✓ Summary Ready")
            self._p1a_tmpl_btn.config(state="normal", fg=ACCENT)
            self._log("[priming] summary marked ready — send template when Agent 1 is set")
        else:
            self._p1a_sum_ready_btn.config(bg=BG2, fg="#666666", text="✓ Summary Ready")
            self._p1a_tmpl_btn.config(state="disabled", fg="#666666")
            self._p1a_template_sent = False
            self._p1a_advance_btn.config(state="disabled", fg="#666666")
        self._p1a_check_advance()

    def _p1a_send_template(self):
        tmpl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "templates", "GENERAL_MODULE_BLOCK_TEMPLATE.md")
        try:
            with open(tmpl_path, "r", encoding="utf-8") as f:
                tmpl = f.read().strip()
        except Exception as e:
            self._log(f"[priming] could not read template: {e}")
            return

        source_full = os.path.join(self._p1a_workspace, self._p1a_source_name) \
                      if (self._p1a_workspace and self._p1a_source_name) else "(not set)"

        workspace_block = ""
        if self._p1a_workspace:
            workspace_block = (
                f"PROJECT WORKSPACE:   {self._p1a_workspace}\n"
                f"SOURCE FOLDER:       {source_full}\n"
                f"CONSTITUTION FOLDER: {self._p1a_constitution or '(not set)'}\n\n"
                "ABSOLUTE RULES — CANNOT BE OVERRIDDEN:\n"
                f"1. ALL code, files, and project output MUST be created inside "
                f"'{self._p1a_source_name}' ONLY. No files outside this folder.\n"
                "2. Installing dependencies (cargo add, npm install, pip install, "
                "etc.) is NOT creating code and is permitted wherever required.\n"
                f"3. Module block files saved by Agent 2 go in: "
                f"{source_full}\\instruction_blocks\\\n"
                "4. State the source folder path in Module A (Scope) so the "
                "workspace layout is explicit in the block record.\n\n"
            )

        msg = (
            f"{workspace_block}"
            "Here is the module block format template. Use this structure to "
            "decompose the project summary into module blocks. Deliver each block "
            "to Agent 2 via the relay using exactly this format:\n\n"
            "To Agent2\n[full block content]\nend message now\n\n"
            "After sending each block, WAIT for Agent 2's confirmation reply "
            "before sending the next one. When ALL blocks are delivered and "
            "Agent 2 has confirmed each one, send this EXACT mode-switch command "
            "(copy it precisely — do not paraphrase):\n\n"
            f"To Agent2\n{IMPL_TRIGGER_CMD}\n"
            "All instruction blocks have been sent and confirmed by Agent 2.\n"
            "Begin implementation in alphanumeric order now.\n"
            "end message now\n\n"
            "IMPORTANT: Do not use the words 'implement' or 'execute' anywhere "
            f"in the blocks themselves. Only {IMPL_TRIGGER_CMD} triggers implementation mode.\n\n"
            f"TEMPLATE:\n\n{tmpl}"
        )

        targets = ["agent1"]
        if not self._bypass_agent3:
            cfg3 = self.agents.get("agent3")
            if cfg3 and cfg3.hwnd and cfg3.input_xy and cfg3.send_xy:
                targets.append("agent3")

        def _send():
            for aid in targets:
                self._inject_to_agent(aid, msg)
        threading.Thread(target=_send, daemon=True).start()

        self._p1a_template_sent = True
        label = "✓ Template Sent" + (" (A1 + A3)" if len(targets) > 1 else "")
        self._p1a_tmpl_btn.config(bg=GREEN, fg="white", text=label)
        self._log(f"[priming] module block template sent to {targets}")
        self._p1a_check_advance()

    def _p1a_check_advance(self):
        setup_ok = (self._p1a_workspace and self._p1a_source_created
                    and self._p1a_constitution)
        all_ok   = setup_ok and self._p1a_summary_sent and self._p1a_template_sent
        if all_ok:
            self._p1a_advance_btn.config(state="normal", fg=FG, bg=ACCENT,
                                         activebackground=ACCENT)
            if not getattr(self, "_p1a_auto_advanced", False):
                self._p1a_auto_advanced = True
                self.root.after(2000, self._auto_advance_to_phase2)
        else:
            self._p1a_advance_btn.config(state="disabled", fg="#666666",
                                         bg=BG2, activebackground=BG2)

    def _auto_advance_to_phase2(self):
        """Auto-slide to Phase 2 when all Phase 1a criteria are met, then
        stagger-send both SOPs so agents are briefed before user clicks Start OCR."""
        self._show_phase(3)
        self._log("[auto] All Phase 1a criteria met — advancing to Phase 2")
        self._set_status("Phase 2 ready — SOPs sending automatically…")
        # Agent 2 SOP first (executor needs rules before orchestrator starts)
        self.root.after(1500, self._auto_send_agent2_sop)

    def _auto_send_agent2_sop(self):
        if self.agents["agent2"].hwnd:
            self._start_agent2()
            self._log("[auto] Agent 2 SOP auto-sent")
        else:
            self._log("[auto] Agent 2 window not set — SOP not sent (click ▶ Agent 2 SOP manually)")
        self.root.after(6000, self._auto_send_agent1_sop)

    def _auto_send_agent1_sop(self):
        if self.agents["agent1"].hwnd:
            self._start_agent1()
            self._log("[auto] Agent 1 SOP auto-sent — click ▶ OCR Watch when agents are ready")
            self._set_status("SOPs sent — start OCR when agents are ready")
        else:
            self._log("[auto] Agent 1 window not set — SOP not sent (click ▶ Agent 1 SOP manually)")

    def _toggle_estop(self):
        """Master E-STOP toggle: freeze/resume every autonomous behavior.
        Frozen: OCR tick loop, agent hands (all pyautogui input from worker
        threads), outbox dispatch, auto-click, welfare (inside the OCR loop).
        NOT frozen: the operator's own input, this GUI, and in-flight local
        inference (the model finishes its thought; SOC just won't act on it
        until resume)."""
        self._estop = not self._estop
        _estop_set(self._estop)
        if self._estop:
            self._estop_btn.config(text="▶  RESUME  (PAUSED)",
                                   bg="#2e7d32", activebackground="#1b5e20")
            self._log("[pause] ⏸ ENGAGED — eyes, hands, outbox, auto-click frozen")
            self._set_status("⏸ PAUSED — all autonomous activity frozen")
        else:
            self._estop_btn.config(text="⏸  PAUSE",
                                   bg="#c62828", activebackground="#8e0000")
            self._log("[pause] ▶ released — resuming autonomous operation")
            self._set_status("▶ resumed")

    def _build_phase2_ui(self):
        p = self._p2_frame

        tk.Label(p, text='Protocol:  To agentX  →  body  →  end message now',
                 bg=BG2, fg=YELLOW, font=("Consolas", 7), anchor="w", pady=3,
                 wraplength=244).pack(fill="x")

        # ── Master PAUSE (styled after the Hot Rod Tuner e-stop button) ───────
        # One press freezes EVERYTHING autonomous: OCR eyes, agent hands
        # (pyautogui boundary), outbox dispatch, auto-click, welfare. Press
        # again to resume. The operator's own input always keeps working.
        pause_row = tk.Frame(p, bg=BG2, pady=4)
        pause_row.pack(fill="x", padx=10)
        self._estop_btn = tk.Button(
            pause_row, text="⏸  PAUSE",
            command=self._toggle_estop,
            bg="#c62828", fg="white", activebackground="#8e0000",
            activeforeground="white", font=("Segoe UI", 10, "bold"),
            relief="raised", bd=3, cursor="hand2", padx=18, pady=4)
        self._estop_btn.pack(fill="x")

        mode_row = tk.Frame(p, bg=BG2, pady=3)
        mode_row.pack(fill="x", padx=10, pady=(4, 0))
        self._mode_dot = tk.Label(mode_row, text="●",
                                   font=("Segoe UI", 10, "bold"), bg=BG2, fg=ACCENT,
                                   cursor="hand2")
        self._mode_dot.pack(side="left", padx=(4, 0))
        self._mode_lbl = tk.Label(mode_row, text="MODULE BLOCK MODE",
                                   font=("Segoe UI", 8, "bold"), bg=BG2, fg=ACCENT,
                                   cursor="hand2")
        self._mode_lbl.pack(side="left", padx=(4, 0))
        self._mode_dot.bind("<Double-Button-1>", lambda e: self._manual_engage_impl_mode())
        self._mode_lbl.bind("<Double-Button-1>", lambda e: self._manual_engage_impl_mode())
        self._disengage_btn = tk.Button(
            mode_row, text="Disengage", command=self._disengage_impl_mode,
            bg=BG2, fg=ORANGE, relief="flat", font=("Segoe UI", 7, "bold"),
            cursor="hand2", padx=4, bd=0)
        self._disengage_btn.pack(side="right", padx=(0, 4))
        self._mode_sub = tk.Label(mode_row, text="Storing blocks only.",
                                   font=("Segoe UI", 7, "italic"), bg=BG2, fg=FG)
        self._mode_sub.pack(side="left", padx=(6, 0))

        sop_row = tk.Frame(p, bg=BG, pady=2)
        sop_row.pack(fill="x", padx=12)
        tk.Button(
            sop_row, text="▶ Agent 1 SOP", command=self._start_agent1,
            bg=BG2, fg=ACCENT, font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2", padx=8, pady=4
        ).pack(side="left")
        tk.Button(
            sop_row, text="▶ Agent 2 SOP", command=self._start_agent2,
            bg=BG2, fg=GREEN, font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2", padx=8, pady=4
        ).pack(side="left", padx=(4, 0))
        tk.Button(
            sop_row, text="⌂", command=self._log_scroll_top,
            bg=BG2, fg=FG, font=("Segoe UI", 9),
            relief="flat", cursor="hand2", padx=6, pady=4
        ).pack(side="right")

        coach_row = tk.Frame(p, bg=BG, pady=1)
        coach_row.pack(fill="x", padx=12)
        tk.Button(
            coach_row, text="⟳ Coach A1",
            command=self._send_coaching_message,
            bg=BG2, fg=YELLOW, font=("Segoe UI", 8),
            relief="flat", cursor="hand2", padx=8, pady=2
        ).pack(side="left", padx=(0, 4))
        tk.Button(
            coach_row, text="? Quiz A1",
            command=self._send_quiz_message,
            bg=BG2, fg=ORANGE, font=("Segoe UI", 8),
            relief="flat", cursor="hand2", padx=8, pady=2
        ).pack(side="left")
        # Start V button removed: the V-plugin auto-loads with SOC (see
        # _load_plugins in __init__) and is brought to front from the master
        # widget's "Show A4" control. No standalone vision server is launched
        # here anymore — vision is served by the main model (see _resolve_vlm_url,
        # which auto-detects the main :8080 or a dedicated vision :8082).

        fmt_row = tk.Frame(p, bg=BG, pady=1)
        fmt_row.pack(fill="x", padx=12)
        tk.Button(
            fmt_row, text="📋 Fmt A2",
            command=lambda: threading.Thread(
                target=self._inject_format_reminder, args=("agent2",), daemon=True).start(),
            bg=BG2, fg="#c586c0", font=("Segoe UI", 8),
            relief="flat", cursor="hand2", padx=6, pady=2
        ).pack(side="left", padx=(0, 4))
        tk.Button(
            fmt_row, text="📋 Fmt A3",
            command=lambda: threading.Thread(
                target=self._inject_format_reminder, args=("agent3",), daemon=True).start(),
            bg=BG2, fg="#c586c0", font=("Segoe UI", 8),
            relief="flat", cursor="hand2", padx=6, pady=2
        ).pack(side="left", padx=(0, 4))
        tk.Label(
            fmt_row, text="remind Claude to use To AgentX envelope",
            bg=BG, fg="#444444", font=("Segoe UI", 7, "italic")
        ).pack(side="left", padx=(4, 0))

        ctrl1 = tk.Frame(p, bg=BG, pady=2)
        ctrl1.pack(fill="x", padx=12)
        self.ocr_btn = tk.Button(
            ctrl1, text="▶ OCR Watch", command=self._toggle_ocr,
            bg=GREEN, fg="#1e1e1e", font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2", activebackground="#3aaf7a",
            padx=10, pady=4)
        self.ocr_btn.pack(side="left")
        self.ocr_lbl = tk.Label(ctrl1, text="OCR: OFF",
                                 bg=BG, fg=FG, font=("Segoe UI", 8, "italic"))
        self.ocr_lbl.pack(side="left", padx=6)
        self._ocr_release_btn = tk.Button(
            ctrl1, text="↺ Release", command=self._ocr_release_hold,
            bg=BG2, fg=YELLOW, font=("Segoe UI", 8),
            relief="flat", cursor="hand2", padx=6, pady=2)
        self._ocr_release_btn.pack(side="left", padx=(0, 4))

        hold_row = tk.Frame(p, bg=BG, pady=1)
        hold_row.pack(fill="x", padx=12)
        # ⊘ A3 packed first so it anchors to the right before left-side buttons consume space
        _a3_lbl = "⊘ A3" if self._bypass_agent3 else "● A3"
        _a3_fg  = "#666666" if self._bypass_agent3 else GREEN
        self._p2_bypass_a3_btn = tk.Button(
            hold_row, text=_a3_lbl,
            command=self._toggle_bypass_agent3,
            bg=BG2, fg=_a3_fg, font=("Segoe UI", 8),
            relief="flat", cursor="hand2", padx=6, pady=2)
        self._p2_bypass_a3_btn.pack(side="right")
        self._hold_btns: dict[str, tk.Button] = {}
        for _aid, _short in [("agent1", "A1"), ("agent2", "A2"), ("agent3", "A3")]:
            _btn = tk.Button(
                hold_row, text=f"⏸ Hold {_short}",
                command=lambda a=_aid: self._toggle_manual_hold(a),
                bg=BG2, fg=FG, font=("Segoe UI", 8),
                relief="flat", cursor="hand2", padx=8, pady=2)
            # Hold A3 is only shown when agent3 is active
            if _aid != "agent3":
                _btn.pack(side="left", padx=(0, 4))
            self._hold_btns[_aid] = _btn
        self._pause_btn = tk.Button(
            hold_row, text="⏸ Pause",
            command=self._toggle_pause,
            bg=BG2, fg=FG, font=("Segoe UI", 8),
            relief="flat", cursor="hand2", padx=8, pady=2)
        self._pause_btn.pack(side="left", padx=(0, 4))

        # Nudge row: per-agent force-scan buttons + pending indicators
        nudge_row = tk.Frame(p, bg=BG, pady=2)
        nudge_row.pack(fill="x", padx=12)
        for _aid, _short in [("agent1", "A1"), ("agent2", "A2"), ("agent3", "A3")]:
            _cfg = self.agents[_aid]
            _cell = tk.Frame(nudge_row, bg=BG)
            _cell.pack(side="left", padx=(0, 6))
            tk.Button(
                _cell, text=f"⚡ {_short}",
                command=lambda a=_aid: threading.Thread(
                    target=self._ocr_force_scan, args=(a,), daemon=True).start(),
                bg=BG2, fg=ACCENT, relief="flat", font=("Segoe UI", 8),
                cursor="hand2", padx=6, pady=2
            ).pack(side="left")
            _cfg.lbl_pending_dot = tk.Label(_cell, text="●", bg=BG, fg="#444444",
                                            font=("Segoe UI", 9))
            _cfg.lbl_pending_dot.pack(side="left", padx=(3, 0))
            _cfg.lbl_pending = tk.Label(_cell, text="", bg=BG, fg="#555555",
                                        font=("Segoe UI", 7, "italic"))
            _cfg.lbl_pending.pack(side="left", padx=(2, 0))

        # Manual override row: bypass hover/template when SOC stalls at a UI step
        manual_row = tk.Frame(p, bg=BG, pady=1)
        manual_row.pack(fill="x", padx=12)
        tk.Button(
            manual_row, text="📋 Read Clip",
            command=lambda: threading.Thread(
                target=self._manual_clip_read, daemon=True).start(),
            bg=BG2, fg="#4ec9b0", relief="flat", font=("Segoe UI", 8),
            cursor="hand2", padx=6, pady=2
        ).pack(side="left")
        tk.Button(
            manual_row, text="📍 5s Nudge",
            command=lambda: threading.Thread(
                target=self._cursor_nudge, daemon=True).start(),
            bg=BG2, fg="#4ec9b0", relief="flat", font=("Segoe UI", 8),
            cursor="hand2", padx=6, pady=2
        ).pack(side="left", padx=(4, 0))
        tk.Label(
            manual_row, text="hover target → nudge clicks it + reads clip",
            bg=BG, fg="#444444", font=("Segoe UI", 7, "italic")
        ).pack(side="left", padx=(6, 0))

        welfare_row = tk.Frame(p, bg=BG, pady=2)
        welfare_row.pack(fill="x", padx=12)
        tk.Button(
            welfare_row, text="⟳  Reset Handshake  —  Replay Last Turn",
            command=self._welfare_check,
            bg=BG2, fg=ORANGE, font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2", pady=4, anchor="center"
        ).pack(fill="x")

        # Session manager — full-width row (tall/slender: never grows the width)
        session_row = tk.Frame(p, bg=BG, pady=2)
        session_row.pack(fill="x", padx=12)
        self._session_btn = tk.Button(
            session_row, text="↻ New Session", command=self._toggle_new_session,
            bg=BG2, fg=FG, font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2", pady=4, anchor="center")
        self._session_btn.pack(fill="x")

        # Control buttons split across two stacked sub-rows so they grow
        # downward and stay inside the slender 250px window — packing all five
        # side-by-side pushed VS Code/Bing/VDesk/A4 off the right edge.
        ctrl2 = tk.Frame(p, bg=BG, pady=2)
        ctrl2.pack(fill="x", padx=12)
        ctrl2a = tk.Frame(ctrl2, bg=BG)
        ctrl2a.pack(fill="x")
        ctrl2b = tk.Frame(ctrl2, bg=BG)
        ctrl2b.pack(fill="x", pady=(3, 0))
        self.fw_btn = tk.Button(
            ctrl2a, text="▶ Outbox", command=self._toggle_file_watcher,
            bg=BG2, fg=ACCENT, font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2", padx=8, pady=4)
        self.fw_btn.pack(side="left")
        self.vscode_btn = tk.Button(
            ctrl2a, text="⚡ VS Code", command=self._toggle_vscode_mode,
            bg=BG2, fg=GREEN, font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2", padx=8, pady=4)
        self.vscode_btn.pack(side="left", padx=(4, 0))
        self.bing_btn = tk.Button(
            ctrl2a, text="🔵 Bing", command=self._toggle_bing_mode,
            bg=BG2, fg=ACCENT, font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2", padx=8, pady=4)
        self.bing_btn.pack(side="left", padx=(4, 0))
        self._vdd_btn = tk.Button(
            ctrl2b, text="🖥 VDesk",
            command=self._toggle_virtual_desktop,
            bg=BG2, fg="#888888", font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2", padx=8, pady=4)
        self._vdd_btn.pack(side="left")
        # Agent 4 (Vision) toggle — only visible when V plugin loaded.
        self._a4_btn = tk.Button(
            ctrl2b, text="👁 A4",
            command=self._toggle_agent4_window,
            bg=BG2, fg="#888888", font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2", padx=8, pady=4)
        # packed later by _refresh_agent4_button after plugins load
        self.clicks_lbl = tk.Label(ctrl2b, text="sends: 0",
                                    bg=BG, fg=YELLOW, font=("Segoe UI", 8))
        self.clicks_lbl.pack(side="right")

        proj_row = tk.Frame(p, bg=BG)
        proj_row.pack(fill="x", padx=12, pady=(4, 2))
        self._project_label = tk.Label(proj_row, text="Project:", bg=BG, fg=FG,
                                       font=("Segoe UI", 8))
        self._project_label.pack(side="left")
        self.project_entry = tk.Entry(
            proj_row, textvariable=self._project_name_var,
            bg=BG2, fg=ACCENT, insertbackground=FG,
            relief="flat", font=("Segoe UI", 9))
        self.project_entry.pack(side="left", fill="x", expand=True, padx=(6, 4))
        self.project_entry.bind("<FocusOut>", lambda _: self._save_config())
        self.project_entry.bind("<Return>", lambda _: self.project_entry.master.focus_set())

        outbox_row = tk.Frame(p, bg=BG)
        outbox_row.pack(fill="x", padx=12, pady=(2, 2))
        tk.Label(outbox_row, text="A3 Outbox:", bg=BG, fg=FG,
                 font=("Segoe UI", 8)).pack(side="left")
        outbox_entry = tk.Entry(
            outbox_row, textvariable=self._agent3_outbox_var,
            bg=BG2, fg="#4ec9b0", insertbackground=FG,
            relief="flat", font=("Segoe UI", 8))
        outbox_entry.pack(side="left", fill="x", expand=True, padx=(6, 4))
        outbox_entry.bind("<FocusOut>", lambda _: self._on_outbox_path_change())
        outbox_entry.bind("<Return>",   lambda _: self._on_outbox_path_change())
        tk.Button(
            outbox_row, text="…", command=self._browse_agent3_outbox,
            bg=BG2, fg=FG, font=("Segoe UI", 8),
            relief="flat", cursor="hand2", padx=4
        ).pack(side="left")

        # ── Agent 3 independent workspace (post-Anthropic update) ─────────────
        # Anthropic split Agent 3 into its own default workspace; SOC needs the
        # path so it can inject "set your workspace to X" at session start.
        a3ws_row = tk.Frame(p, bg=BG)
        a3ws_row.pack(fill="x", padx=12, pady=(2, 2))
        tk.Label(a3ws_row, text="A3 Workspace:", bg=BG, fg=FG,
                 font=("Segoe UI", 8)).pack(side="left")
        a3ws_entry = tk.Entry(
            a3ws_row, textvariable=self._agent3_workspace_var,
            bg=BG2, fg="#dcdcaa", insertbackground=FG,
            relief="flat", font=("Segoe UI", 8))
        a3ws_entry.pack(side="left", fill="x", expand=True, padx=(6, 4))
        a3ws_entry.bind("<FocusOut>", lambda _: self._save_config())
        a3ws_entry.bind("<Return>",   lambda _: self._save_config())
        tk.Button(
            a3ws_row, text="…", command=self._browse_agent3_workspace,
            bg=BG2, fg=FG, font=("Segoe UI", 8),
            relief="flat", cursor="hand2", padx=4
        ).pack(side="left")

        # ── A4/A5 model-swap "CD changer" — disk magazine + loaded-disk beacon ─
        # Tall/slender: stacked rows, never grows the width. Disk = model-name
        # substring; empty disables gating for that agent.
        cd_status_row = tk.Frame(p, bg=BG)
        cd_status_row.pack(fill="x", padx=12, pady=(6, 0))
        tk.Label(cd_status_row, text="💿 Disk:", bg=BG, fg=FG,
                 font=("Segoe UI", 8)).pack(side="left")
        self._cd_status_lbl = tk.Label(
            cd_status_row, text="(no gating)", bg=BG, fg="#666666",
            font=("Segoe UI", 8, "italic"))
        self._cd_status_lbl.pack(side="left", padx=(6, 0))
        for _aid, _short, _hint in (("agent4", "A4 vision", "vision model name"),
                                    ("agent5", "A5 disk1", "empty = magazine MODEL 1"),
                                    ("agent6", "A6 disk2", "empty = magazine MODEL 2"),
                                    ("agent7", "A7 disk3", "empty = magazine MODEL 3")):
            _row = tk.Frame(p, bg=BG)
            _row.pack(fill="x", padx=12, pady=(1, 1))
            tk.Label(_row, text=f"{_short}:", bg=BG, fg="#888888",
                     font=("Segoe UI", 8), width=9, anchor="w").pack(side="left")
            _ent = tk.Entry(
                _row, textvariable=self._cd_disk_var[_aid],
                bg=BG2, fg="#c586c0", insertbackground=FG,
                relief="flat", font=("Segoe UI", 8))
            _ent.pack(side="left", fill="x", expand=True, padx=(4, 4))
            _ent.bind("<FocusOut>", lambda _e, a=_aid: self._on_cd_disk_change(a))
            _ent.bind("<Return>",   lambda _e, a=_aid: self._on_cd_disk_change(a))

        self._build_autoclick_panel(p)

        # ── Phase 2a + Phase 3 buttons ─────────────────────────────────────────
        tk.Frame(p, bg=BG2, height=1).pack(fill="x", padx=10, pady=(8, 0))
        p2a_row = tk.Frame(p, bg=BG, pady=4)
        p2a_row.pack(fill="x", padx=12)
        tk.Button(
            p2a_row,
            text="🛡  Phase 2a: Security Audit",
            command=self._launch_phase2a,
            bg="#1a2a3a", fg="#4ec9b0",
            font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2",
            padx=10, pady=5, anchor="center"
        ).pack(fill="x")

        tk.Frame(p, bg=BG2, height=1).pack(fill="x", padx=10, pady=(4, 0))
        p3_row = tk.Frame(p, bg=BG, pady=4)
        p3_row.pack(fill="x", padx=12)
        tk.Button(
            p3_row,
            text="🔬  Phase 3: Debug",
            command=self._launch_phase3,
            bg="#3a2a4a", fg="#c586c0",
            font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2",
            padx=10, pady=5, anchor="center"
        ).pack(fill="x")

    def _build_log_status(self):
        self._log_open = False
        log_hdr = tk.Frame(self._body, bg=BG2)
        log_hdr.pack(fill="x", padx=10, pady=(4, 0))
        self._log_toggle_btn = tk.Button(
            log_hdr, text="▶ Diagnostics", command=self._toggle_log,
            bg=BG2, fg=ACCENT, relief="flat", font=("Segoe UI", 8, "bold"),
            cursor="hand2", anchor="w", padx=4, bd=0)
        self._log_toggle_btn.pack(side="left")
        tk.Button(
            log_hdr, text="Copy All", command=self._copy_log,
            bg=BG2, fg=FG, relief="flat", font=("Segoe UI", 7),
            cursor="hand2", padx=6, bd=0).pack(side="right")
        tk.Button(
            log_hdr, text="📷 OCR", command=self._ocr_snapshot,
            bg=BG2, fg=YELLOW, relief="flat", font=("Segoe UI", 7, "bold"),
            cursor="hand2", padx=6, bd=0).pack(side="right")

        self.log = scrolledtext.ScrolledText(
            self._body, height=8, wrap="word",
            bg=BG2, fg=FG, insertbackground=FG,
            font=("Consolas", 8), relief="flat",
            borderwidth=0, padx=6, pady=6)
        self.log.config(state="disabled")
        self.log.bind("<Control-c>", self._copy_log_selection)

        self.status_var = tk.StringVar(
            value="Click Set Win for each agent, then ⌖ Auto-Calibrate")
        tk.Label(self._body, textvariable=self.status_var,
                 bg=BG, fg=ORANGE, font=("Segoe UI", 8, "italic"),
                 anchor="w", wraplength=234
                 ).pack(fill="x", padx=12, pady=(0, 4))

    def _show_phase(self, n: int):
        self._current_phase = n
        self._p1_frame.pack_forget()
        self._p1a_frame.pack_forget()
        self._p2_frame.pack_forget()
        if n == 1:
            self._p1_frame.pack(fill="x")
            self._setup_btn.config(state="disabled", fg=BG2, activeforeground=BG2)
        elif n == 2:
            self._p1a_frame.pack(fill="x")
            self._setup_btn.config(state="normal", fg=YELLOW, activeforeground=YELLOW)
        else:  # n == 3
            self._p2_frame.pack(fill="x")
            self._setup_btn.config(state="normal", fg=YELLOW, activeforeground=YELLOW)
        self.root.after(50, self._fit_window)

    def _calibration_complete(self) -> bool:
        """Calibration only — used for startup auto-advance. Does not require attendance."""
        required = ["agent1", "agent2"]
        if not self._bypass_agent3: required.append("agent3")
        if not self._bypass_agent5: required.append("agent5")
        return all(
            self.agents[aid].hwnd and self.agents[aid].input_xy and self.agents[aid].send_xy
            for aid in required)

    def _phase1_complete(self) -> bool:
        """Full Phase 1 gate — calibration + roll call attendance. Used by Launch button."""
        required = ["agent1", "agent2"]
        if not self._bypass_agent3: required.append("agent3")
        if not self._bypass_agent5: required.append("agent5")
        return (self._calibration_complete() and
                all(self._attendance.get(aid, False) for aid in required))

    def _check_phase1_complete(self):
        if not hasattr(self, "_launch_btn"):
            return
        required = ["agent1", "agent2"]
        if not self._bypass_agent3: required.append("agent3")
        if not self._bypass_agent5: required.append("agent5")
        total = len(required) * 3
        count = 0
        for aid in required:
            cfg = self.agents[aid]
            if cfg.hwnd:     count += 1
            if cfg.input_xy: count += 1
            if cfg.send_xy:  count += 1
        cal_done    = count >= total
        attend_done = all(self._attendance.get(aid, False) for aid in required)
        self._p1_progress_var.set(f"SETUP — {count}/{total} required")

        # Jump In: calibration only — no roll call needed for returning users
        if cal_done:
            self._jumpin_btn.config(
                text="⚡ Jump In  →  Phase 2 (no priming)", state="normal",
                bg=BG2, fg=YELLOW, activebackground=BG2)
        else:
            self._jumpin_btn.config(
                text=f"⚡ Jump In  (calibrate first — {count}/{total})", state="disabled",
                bg=BG2, fg="#666666")

        # Plan Project: requires calibration + roll call
        if cal_done and attend_done:
            self._p1_progress_lbl.config(fg=GREEN)
            self._launch_btn.config(
                text="→ Plan Project ▶", state="normal",
                bg=GREEN, fg="#1e1e1e", activebackground="#3aaf7a")
        elif cal_done:
            self._p1_progress_lbl.config(fg=GREEN)
            self._launch_btn.config(
                text="→ Plan Project  (roll call first)", state="disabled",
                bg=BG2, fg=ORANGE)
        else:
            self._p1_progress_lbl.config(fg=ORANGE)
            self._launch_btn.config(
                text=f"→ Plan Project  ({count}/{total} ready)", state="disabled",
                bg=BG2, fg="#666666")

    def _roll_call(self):
        """Send an attendance prompt to each active, configured agent.
        Resets all attendance flags first so stale confirmations don't carry over."""
        required = ["agent1", "agent2"]
        if not self._bypass_agent3: required.append("agent3")
        if not self._bypass_agent5: required.append("agent5")
        # Reset flags and update dots
        for aid in ("agent1", "agent2", "agent3", "agent4", "agent5"):
            self._attendance[aid] = False
        self._update_attendance_ui()
        # Only send to agents that are fully configured
        targets = [aid for aid in required
                   if self.agents[aid].hwnd and self.agents[aid].input_xy
                   and self.agents[aid].send_xy]
        if not targets:
            self._log("[roll call] no agents configured — complete Set Win + Cal first")
            return
        self._log(f"[roll call] sending attendance check to {targets}")
        nums = {"agent1": "1", "agent2": "2", "agent3": "3", "agent5": "5"}
        def _send_all():
            # Turn-taking: ping one agent, wait for ITS SOC-ACK, then ping the
            # next. Agents sharing a window (agent2 + agent3 in one VS Code) must
            # not write their acks into it at the same time — sequencing the pings
            # makes them take turns. Each wait is bounded so one silent agent never
            # blocks the roll call; a final sweep catches any late ack.
            for aid in targets:
                n = nums[aid]
                msg = (
                    f"[SOC CHANNEL CHECK — DO NOT SAVE ANYTHING]\n"
                    f"This is a connectivity ping only. Do not create files, "
                    f"save blocks, or take any action.\n"
                    f"Output the following code verbatim — no other text:\n"
                    f"SOC-ACK-{n}"
                )
                self._inject_to_agent(aid, msg)
                if self._await_ack(aid, ROLL_CALL_TURN_TIMEOUT):
                    self._log(f"[roll call] {aid} acked — pinging next")
                else:
                    self._log(
                        f"[roll call] {aid} no ack in {ROLL_CALL_TURN_TIMEOUT}s "
                        "— moving on (final sweep will retry)")
            # Agent 4 is the V-plugin (HTTP, no OCR window) — confirm it by pinging
            # its VLM endpoint rather than watching a window for SOC-ACK.
            self._roll_call_check_agent4()
            # Final sweep — catch any agent that acked just after we moved on.
            self._roll_call_watch(targets)

        threading.Thread(target=_send_all, daemon=True).start()

    def _await_ack(self, aid: str, timeout: float) -> bool:
        """Poll ONE agent's OCR region for its SOC-ACK until seen or timeout.
        Marks attendance on success, returns True. Lets the roll call take turns:
        ping an agent, wait for its ack here, then ping the next — so two agents
        sharing a window (e.g. agent2 + agent3 in one VS Code) never write their
        acks into it simultaneously."""
        cfg = self.agents[aid]
        if not cfg.ocr_region:
            return False
        rx0, ry0, rx1, ry1 = cfg.ocr_region
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                img = ImageGrab.grab(bbox=(rx0, ry0, rx1, ry1), all_screens=True)
                text = _preprocess_ocr(pytesseract.image_to_string(
                    _prepare_img_for_ocr(img), config="--psm 6"))
            except Exception:
                text = ""
            for m in ROLL_CALL_RE.finditer(text):
                digit = _OCR_DIGIT_NORM.get(m.group(1), m.group(1))
                if f"agent{digit}" == aid:
                    self._mark_attendance(aid)
                    return True
            time.sleep(1.5)
        return False

    def _roll_call_watch(self, targets: list):
        """Poll OCR regions for SOC-ACK-N responses. Runs outside the main OCR
        loop so attendance can be detected from Phase 1 before workflow is started."""
        deadline = time.time() + 120   # give up after 2 minutes
        with _mss_ctor() as sct:
            while time.time() < deadline:
                pending = [aid for aid in targets
                           if not self._attendance.get(aid)]
                if not pending:
                    break
                for aid in pending:
                    cfg = self.agents[aid]
                    if not cfg.ocr_region:
                        continue
                    rx0, ry0, rx1, ry1 = cfg.ocr_region
                    try:
                        img = ImageGrab.grab(bbox=(rx0, ry0, rx1, ry1), all_screens=True)
                    except Exception:
                        grab_box = {"left": rx0, "top": ry0,
                                    "width": rx1 - rx0, "height": ry1 - ry0}
                        raw = sct.grab(grab_box)
                        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
                    text = pytesseract.image_to_string(
                        _prepare_img_for_ocr(img), config="--psm 6")
                    text = _preprocess_ocr(text)
                    for m in ROLL_CALL_RE.finditer(text):
                        digit   = _OCR_DIGIT_NORM.get(m.group(1), m.group(1))
                        ack_aid = f"agent{digit}"
                        if ack_aid == aid:
                            self._mark_attendance(aid)
                time.sleep(2)

    def _roll_call_check_agent4(self):
        """Agent 4 is the V-plugin (HTTP, no OCR window). Confirm presence with ONE
        lightweight ack inference — text-only (no screenshot), no action execution
        and no auto-routing — shown in the Agent 4 window so the operator SEES A4
        respond like the other agents. The reply itself is the liveness proof (a
        dead endpoint raises), so there is a single path, not a ping + a reply.
        Informational only — A4 does not gate launch (SOC often runs without vision)."""
        if getattr(self, "_vplugin", None) is None or getattr(self, "_disable_vplugin", False):
            self._log("[roll call] agent4 (V-plugin) not loaded — skipping")
            return
        win = getattr(self._vplugin, "agent4_window", None)
        if win is None:
            return
        prompt = ("[SOC CHANNEL CHECK] Connectivity ping only. Take no action. "
                  "Output verbatim, nothing else: SOC-ACK-4")

        def _ack():
            try:
                resp = win._call_vlm(prompt, None)        # text-only; no side-effects (patient-wait; any reply = alive)
            except Exception as e:
                self._log(f"[roll call] agent4 (V-plugin) not responding — {e.__class__.__name__}")
                # Surface the failure IN the A4 window — a silent window with no
                # green check reads as a mystery; the actual reason (e.g. "server
                # not reachable — Start Server in GGUF Chatbox") must be visible.
                try:
                    win._append_history("mission", "[from SOC] roll call — channel check")
                    win._append_history("err", f"roll call failed: {e}")
                except Exception:
                    pass
                return
            resp = (resp or "").strip()
            try:
                win._append_history("mission", "[from SOC] roll call — channel check")
                win._append_history("agent4", resp or "(no text)")
            except Exception:
                pass
            try:
                self._write_transcript("soc", "agent4", resp or "(empty)", kind="rollcall")
            except Exception:
                pass
            if resp:
                self._mark_attendance("agent4")          # any reply = alive
            else:
                self._log("[roll call] agent4 replied empty — treating as absent")

        threading.Thread(target=_ack, daemon=True).start()

    def _mark_attendance(self, aid: str):
        """Record that aid has confirmed presence and refresh UI + phase check."""
        if self._attendance.get(aid):
            return   # already confirmed, ignore duplicate
        self._attendance[aid] = True
        self._log(f"[roll call] ✓ {aid} confirmed present (SOC-ACK detected)")
        self.root.after(0, self._update_attendance_ui)
        self.root.after(0, self._check_phase1_complete)

    def _update_attendance_ui(self):
        """Refresh per-agent dot labels and overall attendance status label."""
        if not hasattr(self, "_attendance_lbls"):
            return
        required = ["agent1", "agent2"]
        if not self._bypass_agent3: required.append("agent3")
        if not self._bypass_agent5: required.append("agent5")
        names = {"agent1": "A1", "agent2": "A2", "agent3": "A3", "agent4": "A4", "agent5": "A5"}
        for aid, lbl in self._attendance_lbls.items():
            present = self._attendance.get(aid, False)
            if aid == "agent3" and self._bypass_agent3:
                lbl.config(text=f"{names[aid]}:—", fg="#444444")
            elif aid == "agent5" and self._bypass_agent5:
                lbl.config(text=f"{names[aid]}:—", fg="#444444")
            elif aid == "agent4" and (getattr(self, "_vplugin", None) is None
                                      or getattr(self, "_disable_vplugin", False)):
                lbl.config(text=f"{names[aid]}:—", fg="#444444")
            elif present:
                lbl.config(text=f"{names[aid]}:✓", fg=GREEN)
            else:
                lbl.config(text=f"{names[aid]}:○", fg="#666666")
        all_present = all(self._attendance.get(a, False) for a in required)
        if all_present:
            n = len(required)
            self._attendance_status_lbl.config(
                text=f"✓ All {n} agents confirmed — ready to launch", fg=GREEN)
        else:
            confirmed = sum(1 for a in required if self._attendance.get(a, False))
            self._attendance_status_lbl.config(
                text=f"Attendance: {confirmed}/{len(required)} confirmed",
                fg=ORANGE if confirmed > 0 else "#666666")

    def _test_inject(self):
        targets = []
        for aid in ("agent1", "agent2"):
            cfg = self.agents[aid]
            if cfg.hwnd and cfg.input_xy and cfg.send_xy:
                targets.append(aid)
        if not targets:
            self._set_status("No agents fully configured — complete Set Win + Cal first")
            return
        self._set_status(f"Test inject sending to {len(targets)} agent(s)…")
        self._log(f"[test] injection test starting for {targets}")

        def _run_sequential():
            for aid in targets:
                self._inject_to_agent(
                    aid, f"[SOC test] hello from SOC — {aid} injection OK")

        threading.Thread(target=_run_sequential, daemon=True).start()

    def _test_roundtrip(self):
        cfg1 = self.agents["agent1"]
        if not (cfg1.hwnd and cfg1.input_xy and cfg1.send_xy):
            self._set_status("Agent 1 not fully configured — complete Set Win + Cal first")
            return
        msg = (
            "[SOC round-trip test]\n"
            "Please reply with exactly:\n"
            "To agent2\n"
            "Round-trip confirmed from agent1\n"
            "end message now"
        )
        threading.Thread(
            target=self._inject_to_agent,
            args=("agent1", msg),
            daemon=True).start()
        self._set_status("Round-trip test sent to Agent 1 — watch for Agent 2 injection")
        self._log("[test] round-trip test dispatched → agent1")

    def _build_agent_panel(self, parent, agent_id: str, label: str):
        cfg = self.agents[agent_id]
        outer = tk.Frame(parent, bg=BG, pady=2)
        outer.pack(fill="x", padx=12)

        r1 = tk.Frame(outer, bg=BG)
        r1.pack(fill="x")
        # Pack Set Win first so Tkinter reserves its space before lbl_window expands
        tk.Button(r1, text="Set Win",
                  command=lambda a=agent_id: self._set_window(a),
                  bg=BG2, fg=FG, relief="flat", font=("Segoe UI", 8),
                  cursor="hand2", padx=4).pack(side="right")
        tk.Label(r1, text=label, bg=BG, fg=ACCENT,
                 font=("Segoe UI", 9, "bold"), width=6, anchor="w"
                 ).pack(side="left")
        cfg.lbl_window = tk.Label(r1, text="window: (not set)",
                                   bg=BG, fg=RED, font=("Segoe UI", 8, "italic"),
                                   anchor="w")
        cfg.lbl_window.pack(side="left", fill="x", expand=True)

        r2 = tk.Frame(outer, bg=BG)
        r2.pack(fill="x")
        tk.Button(r2, text="⊙ Input",
                  command=lambda a=agent_id: self._capture_coord(a, "input"),
                  bg=BG2, fg=FG, relief="flat", font=("Segoe UI", 7),
                  cursor="hand2", padx=4).pack(side="right")
        tk.Label(r2, text="", bg=BG, width=3).pack(side="left")
        cfg.lbl_input = tk.Label(r2, text="input field: (not set)",
                                  bg=BG, fg=RED, font=("Segoe UI", 8, "italic"),
                                  anchor="w")
        cfg.lbl_input.pack(side="left", fill="x", expand=True)

        r3 = tk.Frame(outer, bg=BG)
        r3.pack(fill="x")
        tk.Button(r3, text="⊙ Send",
                  command=lambda a=agent_id: self._capture_coord(a, "send"),
                  bg=BG2, fg=FG, relief="flat", font=("Segoe UI", 7),
                  cursor="hand2", padx=4).pack(side="right")
        tk.Label(r3, text="", bg=BG, width=3).pack(side="left")
        cfg.lbl_send = tk.Label(r3, text="send button: (not set)",
                                 bg=BG, fg=RED, font=("Segoe UI", 8, "italic"),
                                 anchor="w")
        cfg.lbl_send.pack(side="left", fill="x", expand=True)

        # Row: Edge prefix (Agent 1 only — Bing/Edge browser noise filter)
        if agent_id == "agent1":
            r4 = tk.Frame(outer, bg=BG)
            r4.pack(fill="x", pady=(2, 0))
            tk.Label(r4, text="", bg=BG, width=3).pack(side="left")
            cfg.prefix_enabled = tk.BooleanVar(value=False)
            tk.Checkbutton(r4, variable=cfg.prefix_enabled, text="Prefix:",
                           bg=BG, fg=ACCENT, selectcolor=BG2,
                           activebackground=BG, activeforeground=ACCENT,
                           font=("Segoe UI", 7), cursor="hand2"
                           ).pack(side="left")
            cfg.prefix_var = tk.StringVar(value=BING_NOISE_PREFIX)
            tk.Entry(r4, textvariable=cfg.prefix_var,
                     bg=BG2, fg=YELLOW, insertbackground=FG,
                     relief="flat", font=("Segoe UI", 7)
                     ).pack(side="left", padx=(2, 0), fill="x", expand=True)

        # Row 5: scroll coords (set by ⌖ Calibrate or hover-capture)
        r5 = tk.Frame(outer, bg=BG)
        r5.pack(fill="x")
        tk.Button(r5, text="Read",
                  command=lambda a=agent_id: self._start_scroll_read(a),
                  bg=BG2, fg=FG, relief="flat", font=("Segoe UI", 8),
                  cursor="hand2", padx=4).pack(side="right")
        tk.Button(r5, text="⊙↓",
                  command=lambda a=agent_id: self._capture_coord(a, "scroll_dn"),
                  bg=BG2, fg=FG, relief="flat", font=("Segoe UI", 7),
                  cursor="hand2", padx=4).pack(side="right", padx=(0, 2))
        tk.Button(r5, text="⊙↑",
                  command=lambda a=agent_id: self._capture_coord(a, "scroll_up"),
                  bg=BG2, fg=FG, relief="flat", font=("Segoe UI", 7),
                  cursor="hand2", padx=4).pack(side="right", padx=(0, 2))
        tk.Label(r5, text="", bg=BG, width=3).pack(side="left")
        cfg.lbl_scroll = tk.Label(r5, text="scroll: (not set)",
                                   bg=BG, fg=RED, font=("Segoe UI", 8, "italic"),
                                   anchor="w")
        cfg.lbl_scroll.pack(side="left", fill="x", expand=True)

        # Row 6: OCR output region
        r6 = tk.Frame(outer, bg=BG)
        r6.pack(fill="x")
        tk.Button(r6, text="⎕ Region",
                  command=lambda a=agent_id: self._calibrate_ocr_region(a),
                  bg=BG2, fg=YELLOW, relief="flat", font=("Segoe UI", 7),
                  cursor="hand2", padx=4).pack(side="right")
        tk.Label(r6, text="", bg=BG, width=3).pack(side="left")
        cfg.lbl_region = tk.Label(r6, text="ocr region: (not set)",
                                   bg=BG, fg=RED, font=("Segoe UI", 8, "italic"),
                                   anchor="w")
        cfg.lbl_region.pack(side="left", fill="x", expand=True)

    # ── OCR region calibration overlay ───────────────────────────────────────────

    def _calibrate_ocr_region(self, agent_id: str):
        """Full-screen drag-to-select overlay spanning all monitors.
        User draws a rectangle over the agent's message output area.
        That bounding box is used for all subsequent OCR grabs."""
        # Use the platform's virtual-screen metrics so the overlay covers every
        # monitor (including virtual displays).
        vx, vy, vw, vh = PLATFORM.virtual_screen()

        overlay = tk.Toplevel(self.root)
        overlay.geometry(f"{vw}x{vh}+{vx}+{vy}")
        overlay.overrideredirect(True)
        overlay.attributes("-topmost", True)
        overlay.attributes("-alpha", 0.45)
        overlay.configure(bg="#050510")

        canvas = tk.Canvas(overlay, bg="#050510",
                           highlightthickness=0, cursor="crosshair")
        canvas.pack(fill="both", expand=True)

        label_name = ("Bing chat"         if agent_id == "agent1"
                      else "Claude Code"   if agent_id == "agent3"
                      else "GGUF Chatbox"  if agent_id == "agent5"
                      else "VS Code chat")
        canvas.create_text(
            vw // 2, 36,
            text=f"Drag to select the {label_name} message output area",
            fill="#ffffff", font=("Segoe UI", 15, "bold"))
        canvas.create_text(
            vw // 2, 64,
            text="Click-drag to draw box  •  release  •  click  ✓ Set Region  •  Esc to cancel",
            fill="#aaaaaa", font=("Segoe UI", 10))

        _rect     = [None]
        _size_lbl = [None]
        _start    = [0, 0]
        _box      = [0, 0, 0, 0]

        def on_press(evt):
            _start[:] = [evt.x, evt.y]
            if _rect[0]:     canvas.delete(_rect[0])
            if _size_lbl[0]: canvas.delete(_size_lbl[0])

        def on_drag(evt):
            if _rect[0]:     canvas.delete(_rect[0])
            if _size_lbl[0]: canvas.delete(_size_lbl[0])
            x1, y1 = _start
            x2, y2 = evt.x, evt.y
            _box[:] = [min(x1,x2), min(y1,y2), max(x1,x2), max(y1,y2)]
            _rect[0] = canvas.create_rectangle(
                x1, y1, x2, y2,
                outline=GREEN, width=2, dash=(6, 3))
            w, h = abs(x2-x1), abs(y2-y1)
            lx = (x1+x2)//2
            ly = min(y1,y2)-14 if min(y1,y2) > 20 else max(y1,y2)+14
            _size_lbl[0] = canvas.create_text(
                lx, ly, text=f"{w}x{h}px",
                fill=GREEN, font=("Consolas", 9))

        def on_set():
            bx1, by1, bx2, by2 = _box
            if bx2 - bx1 < 40 or by2 - by1 < 40:
                canvas.create_text(
                    vw//2, vh//2,
                    text="Selection too small — drag a larger area",
                    fill=RED, font=("Segoe UI", 13, "bold"))
                return
            # Convert canvas coords (relative to overlay top-left) to
            # absolute screen coordinates by adding the virtual screen origin.
            ax1, ay1 = bx1 + vx, by1 + vy
            ax2, ay2 = bx2 + vx, by2 + vy
            cfg = self.agents[agent_id]
            cfg.ocr_region = (ax1, ay1, ax2, ay2)
            w, h = ax2 - ax1, ay2 - ay1
            cfg.lbl_region.config(
                text=f"region: {w}x{h}px ({ax1},{ay1})", fg=GREEN)
            self._log(f"[{agent_id}] OCR region: ({ax1},{ay1})→({ax2},{ay2}) {w}x{h}px")
            self._save_config()
            overlay.destroy()

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>",     on_drag)
        overlay.bind("<Escape>",       lambda e: overlay.destroy())

        btn_y = vh - 52
        tk.Button(
            overlay, text="✓ Set Region", command=on_set,
            bg=GREEN, fg="#1e1e1e", font=("Segoe UI", 11, "bold"),
            relief="flat", padx=16, pady=6, cursor="hand2"
        ).place(x=vw//2 - 130, y=btn_y)
        tk.Button(
            overlay, text="✕ Cancel", command=overlay.destroy,
            bg=RED, fg="white", font=("Segoe UI", 11, "bold"),
            relief="flat", padx=16, pady=6, cursor="hand2"
        ).place(x=vw//2 + 30, y=btn_y)

    # ── Auto-Click settings panel ─────────────────────────────────────────────

    def _build_autoclick_panel(self, parent):
        """Collapsible panel showing all templates in buttons database/ as
        thumbnail rows with an ON/OFF toggle each.  When the auto-click scan
        is running it periodically screenshots the desktop and clicks any
        enabled template it finds."""

        tk.Frame(self.root, bg=BG2, height=1).pack(fill="x", padx=10, pady=(4, 0))

        # Header row
        hdr = tk.Frame(parent, bg=BG2)
        hdr.pack(fill="x", padx=10, pady=(2, 0))

        self._ac_toggle_btn = tk.Button(
            hdr, text="▶ Auto-Click", command=self._toggle_autoclick_panel,
            bg=BG2, fg=YELLOW, relief="flat",
            font=("Segoe UI", 8, "bold"), cursor="hand2", anchor="w", padx=4, bd=0)
        self._ac_toggle_btn.pack(side="left")

        tk.Button(hdr, text="↺", command=self._refresh_autoclick_list,
                  bg=BG2, fg=FG, relief="flat", font=("Segoe UI", 9),
                  cursor="hand2", padx=4, bd=0).pack(side="right")

        self._ac_scan_btn = tk.Button(
            hdr, text="▶ Scan", command=self._toggle_autoclick_scan,
            bg=BG2, fg=GREEN, relief="flat",
            font=("Segoe UI", 8, "bold"), cursor="hand2", padx=6, bd=0)
        self._ac_scan_btn.pack(side="right", padx=(0, 4))

        # Collapsible body — scrollable list of template rows
        self._ac_body = tk.Frame(parent, bg=BG)
        # _ac_body starts collapsed; opened via ▶ Auto-Click toggle

        # Scrollable canvas: fixed height so it never grows the window.
        # Scrollbar is shown only when content overflows the 150px view.
        AC_HEIGHT = 150
        self._ac_canvas = tk.Canvas(self._ac_body, bg=BG, highlightthickness=0,
                                    height=AC_HEIGHT, width=1)
        self._ac_scrollbar = tk.Scrollbar(self._ac_body, orient="vertical",
                                          command=self._ac_canvas.yview)
        self._ac_canvas.configure(yscrollcommand=self._ac_scrollbar.set)
        self._ac_list_frame = tk.Frame(self._ac_canvas, bg=BG)
        self._ac_window = self._ac_canvas.create_window((0, 0), window=self._ac_list_frame, anchor="nw")

        def _on_inner_configure(e):
            self._ac_canvas.configure(scrollregion=self._ac_canvas.bbox("all"))
            # Show scrollbar only when content is taller than the canvas
            if self._ac_list_frame.winfo_reqheight() > AC_HEIGHT:
                self._ac_scrollbar.pack(side="right", fill="y")
            else:
                self._ac_scrollbar.pack_forget()

        self._ac_list_frame.bind("<Configure>", _on_inner_configure)
        # Keep inner frame width pinned to canvas width to avoid horizontal clipping
        self._ac_canvas.bind("<Configure>",
            lambda e: self._ac_canvas.itemconfig(self._ac_window, width=e.width))
        # Mouse-wheel scrolling
        def _ac_mousewheel(e):
            self._ac_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        self._ac_canvas.bind_all("<MouseWheel>", _ac_mousewheel)
        self._ac_canvas.pack(side="left", fill="x", expand=True)

        self._refresh_autoclick_list()

    def _toggle_autoclick_panel(self):
        if self._autoclick_panel_open:
            self._ac_body.pack_forget()
            self._ac_toggle_btn.config(text="▶ Auto-Click")
        else:
            self._ac_body.pack(fill="x", padx=10, pady=(2, 4))
            self._ac_toggle_btn.config(text="▼ Auto-Click")
        self._autoclick_panel_open = not self._autoclick_panel_open
        self.root.after(20, self._fit_window)

    def _refresh_autoclick_list(self):
        """Re-scan buttons database/ and rebuild the thumbnail list."""
        # Clear existing rows and image refs
        for w in self._ac_list_frame.winfo_children():
            w.destroy()
        self._autoclick_images.clear()

        pngs = sorted(TEMPLATE_DIR.glob("*.png"))
        if not pngs:
            tk.Label(self._ac_list_frame,
                     text="No templates yet — hover-capture a button to add one",
                     bg=BG, fg=FG, font=("Segoe UI", 7, "italic"),
                     wraplength=220).pack(anchor="w", pady=4)
            return

        for png in pngs:
            stem = png.stem   # e.g. "agent1_send"

            # Permanent geo/visual landmarks are backend-only — never shown to user.
            if any(p in stem.lower() for p in AUTOCLICK_HIDDEN):
                continue

            row = tk.Frame(self._ac_list_frame, bg=BG, pady=2)
            row.pack(fill="x")

            # Thumbnail (32×32)
            try:
                img = Image.open(png).resize((32, 32), Image.LANCZOS)
                photo = ImageTk.PhotoImage(img)
                self._autoclick_images.append(photo)   # prevent GC
                tk.Label(row, image=photo, bg=BG, relief="flat",
                         bd=1).pack(side="left", padx=(0, 6))
            except Exception:
                tk.Label(row, text="?", bg=BG2, fg=FG,
                         width=4, height=2).pack(side="left", padx=(0, 6))

            # Template name (truncated)
            short = stem if len(stem) <= 18 else stem[:17] + "…"
            tk.Label(row, text=short, bg=BG, fg=FG,
                     font=("Segoe UI", 8), anchor="w").pack(side="left", fill="x", expand=True)

            # Toggle or lock indicator
            is_locked    = any(p in stem.lower() for p in AUTOCLICK_LOCKED)
            is_sequence  = any(p in stem.lower() for p in AUTOCLICK_SEQUENCE)
            if is_locked:
                tk.Label(row, text="🔒 routing", bg=BG, fg=BG2,
                         font=("Segoe UI", 7, "italic")).pack(side="right")
            elif is_sequence:
                tk.Label(row, text="🔒 sequence", bg=BG, fg=BG2,
                         font=("Segoe UI", 7, "italic")).pack(side="right")
            else:
                # Toggle — restore saved state if exists
                if stem not in self._autoclick_vars:
                    self._autoclick_vars[stem] = tk.BooleanVar(value=False)
                var = self._autoclick_vars[stem]

                # Keep thread-safe enabled set in sync with current var state
                if var.get():
                    self._autoclick_enabled.add(stem)
                else:
                    self._autoclick_enabled.discard(stem)

                def _make_cb(s=stem, v=var):
                    def on_toggle():
                        if v.get():
                            self._autoclick_enabled.add(s)
                        else:
                            self._autoclick_enabled.discard(s)
                        state = "ON" if v.get() else "OFF"
                        self._log(f"[auto-click] {s} → {state}")
                        self._save_config()
                    return on_toggle

                def _make_train_btn(s=stem):
                    return tk.Button(
                        row, text="Train", cursor="hand2",
                        bg=BG2, fg=ORANGE,
                        relief="flat", font=("Segoe UI", 7, "bold"),
                        padx=4, bd=0,
                        command=lambda: self._start_training(s))

                _make_train_btn().pack(side="right", padx=(0, 4))

                tk.Checkbutton(
                    row, variable=var, text="auto",
                    bg=BG, fg=ACCENT, selectcolor=BG2,
                    activebackground=BG, activeforeground=ACCENT,
                    font=("Segoe UI", 7), cursor="hand2",
                    command=_make_cb()
                ).pack(side="right")

    # ── Click-training ────────────────────────────────────────────────────────

    def _start_training(self, stem: str):
        """Enter training mode for the given template stem.
        Minimises SOC, then waits for the user to click the real button on screen.
        The region around that click is saved as stem.png in buttons database/."""
        if self._training_stem:
            self._log(f"[train] cancelled '{self._training_stem}' → switching to '{stem}'")
        self._training_stem = stem
        self._log(
            f"[train] Training '{stem}' — SOC will minimise.\n"
            f"        Click the button anywhere on screen within {TRAIN_TIMEOUT}s.\n"
            f"        SOC restores automatically when done.")
        self.root.withdraw()
        threading.Thread(
            target=self._training_capture_loop,
            args=(stem,), daemon=True).start()

    def _training_capture_loop(self, stem: str):
        """Background thread: waits for left-click, captures TRAIN_CAPTURE_W×H
        region centred on the click, saves as stem.png, then restores the window."""
        time.sleep(0.5)   # wait for SOC window to finish minimising

        # Wait for any lingering mouse-down from clicking the Train button to clear
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if not PLATFORM.left_button_down():
                break
            time.sleep(0.02)

        # Now wait for the user's deliberate click
        deadline = time.time() + TRAIN_TIMEOUT
        click_pos = None
        while time.time() < deadline:
            if self._training_stem != stem:
                return   # another stem took over — bail silently
            if PLATFORM.left_button_down():
                click_pos = PLATFORM.cursor_pos()   # record position on down
                # Wait for mouse-up so the screenshot shows the button at rest
                while PLATFORM.left_button_down():
                    time.sleep(0.01)
                time.sleep(0.05)  # tiny settle before screenshot
                break
            time.sleep(0.02)

        if click_pos is None:
            self._training_stem = None
            self._log(f"[train] ✗ timeout — no click detected for '{stem}'")
            self.root.after(0, self.root.deiconify)
            return

        x, y = click_pos
        x1 = max(0, x - TRAIN_CAPTURE_W // 2)
        y1 = max(0, y - TRAIN_CAPTURE_H // 2)
        try:
            with _mss_ctor() as sct:
                raw = sct.grab({"left": x1, "top": y1,
                                "width": TRAIN_CAPTURE_W, "height": TRAIN_CAPTURE_H})
                img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
            out_path = TEMPLATE_DIR / f"{stem}.png"
            img.save(str(out_path))
            self._training_stem = None
            self._log(
                f"[train] ✓ '{stem}.png' saved "
                f"({TRAIN_CAPTURE_W}×{TRAIN_CAPTURE_H}px @ {x},{y})\n"
                f"        Enable 'auto' checkbox to activate auto-clicking.")
        except Exception as e:
            self._training_stem = None
            self._log(f"[train] ✗ save error for '{stem}': {e}")

        self.root.after(0, self.root.deiconify)
        self.root.after(150, self._refresh_autoclick_list)

    # ── Auto-click scan loop ───────────────────────────────────────────────────

    def _toggle_autoclick_scan(self):
        if self._autoclick_running:
            self._autoclick_running = False
            self._ac_scan_btn.config(text="▶ Scan", fg=GREEN)
            self._log("[auto-click] scan stopped")
        else:
            if not _CV2_OK:
                self._set_status("opencv required for auto-click — pip install opencv-python")
                return
            self._autoclick_running = True
            self._ac_scan_btn.config(text="■ Scanning", fg=RED)
            self._log("[auto-click] scan started")
            self._autoclick_thread = threading.Thread(
                target=self._autoclick_loop, daemon=True)
            self._autoclick_thread.start()

    def _autoclick_loop(self):
        """Background thread: periodically screenshot the desktop, match all
        enabled templates, click any that appear, respecting per-template cooldown.
        Reads self._autoclick_enabled (plain set) instead of calling
        BooleanVar.get() to avoid Tcl thread-safety issues."""
        with _mss_ctor() as sct:
            while self._autoclick_running:
                if getattr(self, "_estop", False):      # E-STOP: no auto-clicks
                    time.sleep(0.3)
                    continue
                try:
                    # Snapshot the enabled set — no Tcl calls from this thread
                    enabled = set(self._autoclick_enabled)
                    if enabled:
                        mon = sct.monitors[0]   # full virtual desktop
                        raw = sct.grab(mon)
                        screen_bgr = cv2.cvtColor(
                            np.array(raw, dtype=np.uint8), cv2.COLOR_BGRA2BGR)

                        now = time.time()
                        for stem in enabled:
                            png = TEMPLATE_DIR / f"{stem}.png"
                            if not png.exists():
                                continue
                            # Per-template cooldown
                            if now - self._autoclick_last.get(stem, 0) < AUTOCLICK_COOLDOWN:
                                continue
                            # Load template from cache (disk read only when file changes)
                            tmpl = self._load_template_cached(stem, png)
                            if tmpl is None:
                                continue
                            res = cv2.matchTemplate(screen_bgr, tmpl, cv2.TM_CCOEFF_NORMED)
                            _, max_val, _, max_loc = cv2.minMaxLoc(res)
                            if max_val >= TEMPLATE_THRESH:
                                h_t, w_t = tmpl.shape[:2]
                                # Add monitor origin so coords are Windows screen coords,
                                # not pixel-offsets within the mss capture buffer.
                                # Matters when a monitor sits above/left of primary
                                # (mon['top'] or mon['left'] is negative).
                                cx = max_loc[0] + w_t // 2 + mon['left']
                                cy = max_loc[1] + h_t // 2 + mon['top']
                                pyautogui.click(cx, cy)
                                self._autoclick_last[stem] = time.time()
                                self._log(
                                    f"[auto-click] ✓ {stem}  conf={max_val:.2f}  "
                                    f"→ clicked ({cx},{cy})")
                except Exception as e:
                    self._log(f"[auto-click] scan error: {e}")
                time.sleep(AUTOCLICK_SCAN)

    # ── Agent window + coord capture ──────────────────────────────────────────

    def _set_window(self, agent_id: str):
        """Countdown capture: status bar counts down 5s while user hovers cursor
        over the target window — no key press required."""
        names = {"agent1": "Agent 1", "agent2": "Agent 2", "agent3": "Agent 3"}
        label = names.get(agent_id, agent_id)
        countdown = 5

        def _tick(remaining):
            if remaining > 0:
                self._set_status(
                    f"Hover cursor over the {label} window — capturing in {remaining}s …")
                self.root.after(1000, lambda: _tick(remaining - 1))
            else:
                self._set_status(f"Capturing {label} window …")
                threading.Thread(target=_capture, daemon=True).start()

        def _capture():
            try:
                px, py_ = PLATFORM.cursor_pos()
                got = PLATFORM.window_from_point(px, py_)
                if not got:
                    raise RuntimeError("no window under cursor")
                hwnd, title, _cls, rect = got
                title = title or "(unknown)"
                rx0, ry0, rx1, ry1 = rect
                cfg = self.agents[agent_id]
                cfg.hwnd       = hwnd
                cfg.title      = title
                cfg.ocr_region = (rx0, ry0, rx1, ry1)
                short = (title[:22] + "…") if len(title) > 22 else title
                w, h  = rx1 - rx0, ry1 - ry0

                def _ui():
                    cfg.lbl_window.config(text=f"window: {short} ✓", fg=GREEN)
                    if cfg.lbl_region:
                        cfg.lbl_region.config(
                            text=f"region: {w}x{h}px (auto)", fg=ACCENT)
                    self._log(f"[{agent_id}] window locked: {title}  "
                              f"({rx0},{ry0})→({rx1},{ry1})")
                    self._save_config()
                    self._check_phase1_complete()

                self.root.after(0, _ui)
            except Exception as ex:
                self.root.after(0, lambda: self._log(f"[set-win] error: {ex}"))

        _tick(countdown)

    def _capture_coord(self, agent_id: str, coord_type: str):
        labels = {
            "input":     "input field",
            "send":      "send button",
            "scroll_dn": "scroll-down arrow",
            "scroll_up": "scroll-up arrow",
        }
        self._set_status(
            f"Hover over {agent_id} {labels.get(coord_type, coord_type)}"
            f" — capturing in 3 s…")
        self.root.after(3000, lambda: self._do_capture(agent_id, coord_type))

    def _do_capture(self, agent_id: str, coord_type: str):
        x, y = pyautogui.position()
        cfg  = self.agents[agent_id]

        # Update the right config slot and label
        if coord_type == "input":
            cfg.input_xy = (x, y)
            cfg.lbl_input.config(text=f"input field: ({x},{y})", fg=GREEN)
        elif coord_type == "send":
            cfg.send_xy = (x, y)
            cfg.lbl_send.config(text=f"send button: ({x},{y})", fg=GREEN)
        elif coord_type == "scroll_dn":
            cfg.scroll_dn_xy = (x, y)
            dn_txt = f"({x},{y})"
            up_txt = f"{cfg.scroll_up_xy}" if cfg.scroll_up_xy else "?"
            cfg.lbl_scroll.config(text=f"scroll ↓{dn_txt} ↑{up_txt}", fg=GREEN)
        elif coord_type == "scroll_up":
            cfg.scroll_up_xy = (x, y)
            up_txt = f"({x},{y})"
            dn_txt = f"{cfg.scroll_dn_xy}" if cfg.scroll_dn_xy else "?"
            cfg.lbl_scroll.config(text=f"scroll ↓{dn_txt} ↑{up_txt}", fg=GREEN)

        # Save a PNG crop centred on the cursor → buttons database/
        # This template is used by ⌖ Calibrate for visual matching.
        self._save_template_crop(agent_id, coord_type, x, y)
        self._save_config()

        self._log(f"[{agent_id}] {coord_type} → ({x},{y})")
        self._set_status(f"{agent_id} {coord_type} captured at ({x},{y})")
        self.root.after(0, self._check_phase1_complete)

    def _save_template_crop(self, agent_id: str, slot: str, cx: int, cy: int):
        """Screenshot a TEMPLATE_CAPTURE×TEMPLATE_CAPTURE square centred on
        (cx, cy) and save it to 'buttons database/agent1_send.png' etc.
        Overwrites any existing file so re-hovering refreshes the template."""
        half = TEMPLATE_CAPTURE // 2
        region = {
            "left":   max(0, cx - half),
            "top":    max(0, cy - half),
            "width":  TEMPLATE_CAPTURE,
            "height": TEMPLATE_CAPTURE,
        }
        try:
            with _mss_ctor() as sct:
                raw = sct.grab(region)
                img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
            fname = f"{agent_id}_{slot}.png"
            out   = TEMPLATE_DIR / fname
            img.save(str(out))
            self._log(f"[{agent_id}] template saved → {fname} "
                      f"({TEMPLATE_CAPTURE}×{TEMPLATE_CAPTURE}px)")
        except Exception as e:
            self._log(f"[{agent_id}] template save error: {e}")

    # ── Injection ─────────────────────────────────────────────────────────────

    # Maximum characters injected in a single paste — prevents chat UI hangs
    MAX_INJECT_CHARS = 8000

    # Seconds to wait for user response in the click-assist dialog
    COORD_ASSIST_TIMEOUT = 25

    def _prompt_missing_coord(self, agent_id: str, slot: str) -> "tuple | None":
        """When template matching and stored coords both fail, show a small dialog
        asking the user to hover over the missing element and capture it (3-second
        countdown), or dismiss to skip the current send.

        Blocks the calling thread up to COORD_ASSIST_TIMEOUT seconds.
        Returns (x, y) if user captures, or None if dismissed/timed out."""
        cfg = self.agents[agent_id]
        # Re-check in case another thread set the coord while we were waiting
        current = cfg.input_xy if slot == "input" else cfg.send_xy
        if current:
            return current

        event  = threading.Event()
        result = [None]

        def _show():
            if event.is_set():
                return  # timed out before dialog rendered
            dlg = tk.Toplevel(self.root)
            dlg.title(f"Missing: {agent_id} {slot}")
            dlg.attributes("-topmost", True)
            dlg.resizable(False, False)
            dlg.configure(bg=BG2)
            sw = dlg.winfo_screenwidth()
            sh = dlg.winfo_screenheight()
            dlg.geometry(f"310x120+{(sw - 310)//2}+{(sh - 120)//2}")

            tk.Label(dlg,
                     text=f"⚠  {agent_id} — {slot} not found on screen",
                     bg=BG2, fg=ORANGE,
                     font=("Segoe UI", 9, "bold")).pack(pady=(12, 2))
            tk.Label(dlg,
                     text="Hover over the target then click ⊙ Capture, or Skip.",
                     bg=BG2, fg=FG,
                     font=("Segoe UI", 8)).pack(pady=(0, 10))

            row = tk.Frame(dlg, bg=BG2)
            row.pack()

            def _on_capture():
                dlg.destroy()
                self.root.withdraw()
                self._set_status(
                    f"Hover over {agent_id} {slot} — capturing in 3 s…")
                def _do():
                    time.sleep(3.0)
                    x, y = pyautogui.position()
                    result[0] = (x, y)
                    if slot == "input":
                        cfg.input_xy = (x, y)
                        if cfg.lbl_input:
                            self.root.after(0, lambda: cfg.lbl_input.config(
                                text=f"input field: ({x},{y})", fg=GREEN))
                    elif slot == "send":
                        cfg.send_xy = (x, y)
                        if cfg.lbl_send:
                            self.root.after(0, lambda: cfg.lbl_send.config(
                                text=f"send button: ({x},{y})", fg=GREEN))
                    self._save_config()
                    self._log(f"[coord-assist] {agent_id} {slot} → ({x},{y})")
                    self.root.after(0, self.root.deiconify)
                    event.set()
                threading.Thread(target=_do, daemon=True).start()

            def _on_skip():
                dlg.destroy()
                event.set()

            tk.Button(row, text="⊙ Capture (3s hover)",
                      command=_on_capture, bg=BG2, fg=GREEN,
                      relief="flat", font=("Segoe UI", 8, "bold"),
                      cursor="hand2", padx=8).pack(side="left", padx=(0, 8))
            tk.Button(row, text="Skip this send",
                      command=_on_skip, bg=BG2, fg=ORANGE,
                      relief="flat", font=("Segoe UI", 8),
                      cursor="hand2", padx=8).pack(side="left")

            dlg.protocol("WM_DELETE_WINDOW", _on_skip)

        self.root.after(0, _show)
        event.wait(self.COORD_ASSIST_TIMEOUT)
        return result[0]

    def _inject_to_agent(self, agent_id: str, text: str,
                         bypass_mode_check: bool = False,
                         suppress_reminder: bool = False):
        """Focus agent window, paste text into input field, click Send.
        bypass_mode_check=True skips IMPL_ATTEMPT_RE filtering — used for SOP sends
        so the SOP content (which mentions implementation) is never blocked.
        Serialised via _inject_lock to prevent clipboard clobber on concurrent calls."""
        cfg = self.agents.get(agent_id)
        if not cfg or not cfg.hwnd:
            self._log(f"[router] {agent_id} window not configured — skipped")
            return
        with self._inject_lock:
            prev_topmost = None
            try:
                # Temporarily clear SOC's topmost flag so it doesn't steal focus
                try:
                    prev_topmost = self.root.attributes("-topmost")
                    self.root.attributes("-topmost", False)
                except Exception:
                    prev_topmost = None

                # ── Mode system: Agent2 intercept + safety header ─────────────
                if agent_id == "agent2":
                    if self._agent2_hold:
                        self._log(
                            "[mode] Agent2 is in HOLD — message blocked. "
                            "Click Disengage to reset.")
                        return
                    if (not bypass_mode_check and self._mode == "module_block"
                            and IMPL_TRIGGER_CMD not in text
                            and IMPL_ATTEMPT_RE.search(text)):
                        self._agent2_impl_attempts += 1
                        self._log(
                            f"[mode] ⚠ impl attempt #{self._agent2_impl_attempts} "
                            "intercepted — blocked")
                        if self._agent2_impl_attempts >= IMPL_RUNAWAY_LIMIT:
                            self._agent2_hold = True
                            self.root.after(0, self._update_mode_indicator)
                            self._log("[mode] ⛔ Agent2 HOLD — runaway prevention active")
                            threading.Thread(
                                target=self._inject_to_agent,
                                args=("agent1",
                                      "Agent2 entered runaway prevention mode. "
                                      "Manual reset required."),
                                daemon=True).start()
                        else:
                            threading.Thread(
                                target=self._inject_to_agent,
                                args=("agent2",
                                      "Implementation is not permitted. "
                                      "Await authorization from Agent1."),
                                daemon=True).start()
                        return
                    if self._mode == "module_block" and not bypass_mode_check:
                        text = MODULE_BLOCK_HEADER + "\n" + text

                # ── Mode system: Agent1 anti-drift counters ───────────────────
                if agent_id == "agent1":
                    self._agent1_inbound_count += 1
                    # Overflow handler: arm the "awaiting reply" window. If no trigger
                    # appears in the OCR region within AGENT1_OVERFLOW_TIMEOUT, the tick
                    # loop blind scroll-to-bottoms + copies (reply rendered off-region).
                    self._agent1_expect_since = time.time()
                    self._agent1_overflow_tries = 0
                    # Session manager: count Agent-1 turns; recommend a New Session
                    # (archive + fresh chat) once the window gets heavy.
                    self._session_agent1_count += 1
                    if (self._session_agent1_count >= SESSION_MAX_AGENT1_MSGS
                            and not self._session_full_flagged):
                        self._session_full_flagged = True
                        self._log(
                            f"[session] Agent-1 at {self._session_agent1_count} messages — "
                            "recommend New Session (archive + fresh Copilot chat). Click ↻ New Session.")
                        self.root.after(0, self._flag_session_full_ui)
                    if BLOCK_SAVED_RE.search(text):
                        self._consecutive_saved_count += 1
                    else:
                        self._consecutive_saved_count = 0

                if len(text) > self.MAX_INJECT_CHARS:
                    self._log(f"[router] message truncated "
                              f"{len(text)} → {self.MAX_INJECT_CHARS} chars")
                    text = text[:self.MAX_INJECT_CHARS]

                if agent_id != "agent1" and cfg.prefix_enabled and cfg.prefix_enabled.get() and cfg.prefix_var:
                    prefix = cfg.prefix_var.get().strip()
                    if prefix:
                        text = prefix + text

                cfg.msg_count += 1
                _reminder_interval = (
                    REMINDER_EVERY_AGENT1 if agent_id == "agent1" else
                    REMINDER_EVERY_AGENT2 if agent_id == "agent2" else
                    REMINDER_EVERY_AGENT5 if agent_id == "agent5" else
                    REMINDER_EVERY)
                # In implementation mode skip the full GROUND_RULES for agent1 and agent2.
                # GROUND_RULES_AGENT1 is module-block guidance — wrong mode entirely.
                # GROUND_RULES_AGENT2 contains IMPL_COMPLETE_PHRASE which resets mode if echoed.
                # The compact IMPL_FORMAT_REMINDER handles drift for both in this mode.
                _impl_suppress = (self._mode == "implementation"
                                  and agent_id in ("agent1", "agent2"))
                if not suppress_reminder and not _impl_suppress and cfg.msg_count % _reminder_interval == 0:
                    if agent_id == "agent3":
                        rules = GROUND_RULES_VSCODE_BRIEF
                    elif agent_id == "agent1":
                        rules = GROUND_RULES_AGENT1
                    elif agent_id == "agent5":
                        rules = GROUND_RULES_AGENT5
                    else:
                        rules = GROUND_RULES_AGENT2
                    text = rules + "\n\n" + text
                    self._log(f"[recal] role reminder injected to {agent_id} "
                              f"(msg #{cfg.msg_count}, every {_reminder_interval})")
                proj = self._project_name_var.get().strip()
                if agent_id == "agent1" and proj:
                    text = f"[ACTIVE PROJECT: {proj}]\n\n" + text

                if agent_id == "agent1":
                    text = BING_NOISE_PREFIX + text

                # ── Format-envelope reinforcement (ALL modes) ─────────────────
                # A weak model drifts off the routing envelope within a few
                # messages, so reinforce it on a per-agent cadence tuned to the
                # occupant's capability (FORMAT_REINFORCE_EVERY). Rides along with
                # the message — does NOT replace it. Was previously gated to
                # implementation mode only, which left a low-level model unguarded
                # in every other mode → silent drift, broken loop.
                _fmt_every = FORMAT_REINFORCE_EVERY.get(agent_id, 0)
                if (_fmt_every > 0
                        and agent_id in self._impl_format_count
                        and not bypass_mode_check):
                    self._impl_format_count[agent_id] += 1
                    if self._impl_format_count[agent_id] % _fmt_every == 0:
                        reminder = (IMPL_FORMAT_REMINDER_AGENT1 if agent_id == "agent1"
                                    else IMPL_FORMAT_REMINDER_AGENT2)
                        text = text + reminder
                        self._log(
                            f"[fmt-reinforce] envelope reminder appended to {agent_id} "
                            f"(msg #{self._impl_format_count[agent_id]}, every {_fmt_every})")

                # Rewrite "To AgentN" → "To Claude" when the target window is
                # a Claude instance (claude.ai browser or VS Code Claude Code).
                # Slot-agnostic: detected by window title or panel landmark.
                # Fast-path guard: skip the expensive template scan entirely when
                # the text doesn't start with a routing header — normal routing
                # passes the body only (no "To AgentN" prefix), so this is a
                # no-op for the vast majority of inject calls.
                _stripped = text.lstrip()
                if (re.match(r"(?i)^To\s+Agent\s*\d+\b", _stripped)
                        and self._is_claude_window(cfg)):
                    text = re.sub(
                        r"(?i)^To\s+Agent\s*\d+\b",
                        "To Claude",
                        _stripped,
                        count=1,
                    )

                pyperclip.copy(text)

                # Restore and focus the target window (robust foreground set)
                PLATFORM.focus_window(cfg.hwnd)
                time.sleep(PASTE_DELAY)

                tmpl_input, tmpl_send = self._find_two_buttons(agent_id)
                # Prefer the user-calibrated input coordinate (the actual compose box,
                # set via ⊙ Input) over template matching — the template set can match
                # a misleading element like agent1_chat_input_field at the window top
                # instead of the bottom compose box, which makes Ctrl+A grab the page.
                input_xy = cfg.input_xy or tmpl_input

                if not input_xy:
                    # Auto-recalibrate: clear stale coords and try template matching
                    self._log(
                        f"[router] {agent_id}: input not found — auto-recalibrating…")
                    self._set_status(f"⚠ {agent_id}: input missing — recalibrating…")
                    cfg.input_xy = None
                    cfg.send_xy  = None
                    self._auto_calibrate()
                    tmpl_input2, tmpl_send2 = self._find_two_buttons(agent_id)
                    input_xy = tmpl_input2 or cfg.input_xy
                    if not input_xy:
                        input_xy = self._prompt_missing_coord(agent_id, "input")
                    if not input_xy:
                        self._log(
                            f"[router] {agent_id}: input field not located after "
                            "recalibration — send aborted. Use ⊙ Input to set it.")
                        self._set_status(
                            f"⚠ {agent_id}: input field missing — set via ⊙ Input")
                        return
                    PLATFORM.focus_window(cfg.hwnd)
                    time.sleep(PASTE_DELAY)

                # Click & paste sequence
                # Bing Copilot (agent1): contenteditable in Edge needs a double-click
                # to reliably capture focus, and the send button only renders after
                # Bing processes the pasted text — poll for it instead of fixed wait.
                _is_bing = (agent_id == "agent1")

                try:
                    pyautogui.click(*input_xy)
                    if _is_bing:
                        time.sleep(0.4)
                        pyautogui.click(*input_xy)   # second click ensures contenteditable focus
                except Exception:
                    pass

                _settle  = 0.5  if _is_bing else 0.15
                _between = 0.15 if _is_bing else 0.05
                time.sleep(_settle)
                if _is_bing:
                    # Focus-safe clear for Agent 1 (Copilot contenteditable in Edge).
                    # A blind Ctrl+A selects the entire CONVERSATION whenever the
                    # input click misses focus — that's what nuked the page / hit the
                    # mic button. End + Shift+Home only act inside the focused field;
                    # on a wrong-focus page they are harmless cursor keys, never a
                    # select-all. Clears the common empty/single-line draft case.
                    pyautogui.press("end")
                    time.sleep(0.05)
                    pyautogui.hotkey("shift", "home")
                else:
                    pyautogui.hotkey("ctrl", "a")
                time.sleep(_between)
                pyautogui.hotkey("ctrl", "v")

                # Bing/Copilot (agent1) sends on Enter — press it instead of hunting
                # the send arrow. The arrow template is unreliable and a miss clicks
                # the microphone → voice slide, which sabotages the whole sequence.
                if _is_bing:
                    time.sleep(0.4)   # let the paste settle in the contenteditable
                    pyautogui.press("enter")
                    send_xy = "__enter__"   # sentinel: already sent, skip button click
                    self._log(f"[→{agent_id}] sent via Enter key")
                else:
                    time.sleep(SEND_DELAY)
                    send_xy = tmpl_send or self._find_agent_button_xy(agent_id, "send") or cfg.send_xy

                if not send_xy:
                    send_xy = self._prompt_missing_coord(agent_id, "send")

                if send_xy:
                    try:
                        if send_xy != "__enter__":   # Bing already sent via Enter
                            pyautogui.click(*send_xy)
                    except Exception:
                        pass
                    self._click_count += 1
                    self.root.after(0, lambda: self.clicks_lbl.config(
                        text=f"sends: {self._click_count}"))
                    self._log(
                        f"[→{agent_id}] ✓  {text[:70]}{'…' if len(text) > 70 else ''}")
                    # After sending to agent1, suppress OCR copy for 3s so the
                    # previous message clears before the copy sequence starts.
                    if agent_id == "agent1":
                        self._inject_grace["agent1"] = time.time() + 1
                        self._agent1_lead_observed = 0.0   # reset so next generation gets full 45s wait
                        self._log("[→agent1] 1s copy-grace started — lead-time reset for next cycle")
                else:
                    self._log(
                        f"[→{agent_id}] pasted — send button not found "
                        f"(use ⊙ Send to set it)  {text[:60]}")
                    self._set_status(
                        f"⚠ {agent_id}: message pasted — set send button via ⊙ Send")
                    if self._vplugin:
                        _sr = cfg.ocr_region
                        _ctx = (
                            f"A message was just pasted into {agent_id}'s input box "
                            "but the send button was not found by template matching. "
                        )
                        if _sr:
                            _ctx += (
                                f"Input area is near the bottom of the OCR region "
                                f"(approx x={_sr[0]}-{_sr[2]}, y={_sr[3]}-{_sr[3] + 60}). "
                            )
                        _ctx += (
                            "The send button is: blue circle with right-pointing arrow "
                            "(Copilot/Edge), paper-plane icon (Claude Code / VS Code), "
                            "or upward arrow (Claude.ai). It sits at the right end of "
                            "the input bar. Click it to submit the pasted message."
                        )
                        self._vplugin.nudge_stall("send_button", agent_id, _ctx)
                self._set_status(f"→ {agent_id}")
            except ImportError:
                self._set_status("pywin32 missing — pip install pywin32")
            except Exception as e:
                err = str(e).lower()
                if "invalid window handle" in err or "access is denied" in err:
                    stale = cfg.hwnd
                    cfg.hwnd = None
                    self.root.after(0, lambda: cfg.lbl_window.config(
                        text="window: (lost — re-set)", fg=RED))
                    self._log(f"[router] hwnd {stale} gone — cleared. Re-run Set Win.")
                else:
                    self._log(f"[router] inject error: {e}")
            finally:
                try:
                    if prev_topmost is not None:
                        self.root.attributes("-topmost", prev_topmost)
                except Exception:
                    pass
    # ── Routing logic ─────────────────────────────────────────────────────────

    def _route_text(self, ocr_text: str, source_agent: str | None = None,
                    from_bridge: bool = False) -> int:
        """Extract and route messages. Returns number of messages routed.
        source_agent: if set, skip any message addressed TO that same agent —
        a window cannot legitimately route a message to itself (prevents SOP/reminder
        text displayed in the window from being re-injected back into it).
        from_bridge: True when the text came from the exact-reply bridge file (not
        OCR). While the bridge is live the OCR read of the shared local window is
        the lossy duplicate, so it is dropped here in favour of the file route."""
        # Bridge arbitration: the chatbox pushes exact local replies as files
        # (bypasses OCR digit misreads — "Agent7" → "Agent?"). Once that channel
        # is proven live, the OCR route for the shared window (source 'agent5') is
        # the corrupt duplicate — drop it. Redispatch (source 'cd_changer') and the
        # file route itself (from_bridge) are never suppressed.
        if source_agent == "agent5" and not from_bridge and self._bridge_active():
            return 0
        # Strip Edge browser prefix echoed back in Agent 1's output
        if BING_NOISE_PREFIX in ocr_text:
            ocr_text = ocr_text.replace(BING_NOISE_PREFIX, "")

        # Normalise Copilot @-mention prefix: "@  To Agent1" → "To Agent1".
        # Copilot prepends @ when addressing agents; OCR also sometimes adds it
        # as artefact. Strip any @-or-whitespace before "To Agent" on a line.
        ocr_text = re.sub(r"(?im)^[@\s]+(?=to\s+agent)", "", ocr_text)

        routed  = 0
        matched = 0   # blocks that SENTINEL_RE/INLINE_RE parsed (may be held/deduped)

        def _try_route(agent_id: str, body: str) -> bool:
            """Apply hold-state gate then dedup, then inject. Returns True if routed."""
            # Directional guard: a window cannot self-route.
            if source_agent and agent_id == source_agent:
                self._log(f"[ocr] directional skip — '{agent_id}' seen in its own window")
                # Apply copy cooldown so the force-scan loop backs off while the
                # agent composes a fresh outbound response (e.g. Copilot echoing
                # received pong context causes repeated directional skips otherwise).
                if agent_id == "agent1":
                    self._agent1_copy_fail_at = time.time()
                return False

            # Global pause: OCR keeps scanning but nothing injects.
            if self._paused:
                return False

            # Durable transcript — record the parsed message BEFORE the
            # bypass/hold/dedup gates, so even traffic to a bypassed or held
            # agent stays visible. (Delivery may still be blocked below.)
            self._write_transcript(source_agent, agent_id, body, kind="msg")

            # Overflow handler: a reply parsed FROM agent1 satisfies the
            # awaiting-reply expectation — clear it so the bounded blind-scan
            # retry doesn't re-fire after a successful (often off-region) capture.
            if source_agent == "agent1":
                self._agent1_expect_since = 0.0

            # Agent 3 bypass: when active, ignore all traffic to/from agent3.
            if self._bypass_agent3 and (agent_id == "agent3" or source_agent == "agent3"):
                return False

            # Agent 5 bypass: when active, ignore all traffic to/from agent5.
            if self._bypass_agent5 and (agent_id == "agent5" or source_agent == "agent5"):
                return False

            # Manual per-agent hold: blocks routing FROM the held agent's window.
            # Hold A1 — persistent: stays held until user clicks Resume.
            # Hold A2 — one-shot: blocks this one route then auto-releases so
            #            the sequence can flow without manual intervention.
            if source_agent and self._manual_hold.get(source_agent):
                if source_agent == "agent2":
                    # One-shot: clear hold immediately after blocking this route
                    self._manual_hold["agent2"] = False
                    _btn = self._hold_btns.get("agent2")
                    if _btn:
                        self.root.after(0, lambda b=_btn: b.config(
                            text="⏸ Hold A2", bg=BG2, fg=FG, activebackground=BG2))
                    self._log("[hold] agent2 one-shot hold — blocked 1 action, auto-released")
                return False

            if self._waiting_reply == agent_id:
                elapsed = time.time() - self._waiting_since

                # Check if agent is rate-limited — use dynamic timeout until quota resets
                if agent_id in self._rate_limited:
                    timeout = self._rate_limited[agent_id] - time.time()
                    timeout = max(timeout, 0)  # clamp to 0 if already past reset time
                    if timeout > 0:
                        now = time.time()
                        if now - self._last_hold_log >= HOLD_LOG_INTERVAL:
                            self._last_hold_log = now
                            self._log(
                                f"[ocr] ⏸ holding — {agent_id} rate-limited  "
                                f"({int(timeout)}s remaining until quota resets)  "
                                f"— click ↺ Release to skip")
                        return False
                    else:
                        # Rate limit has expired — clear the flag and fall through to normal hold
                        del self._rate_limited[agent_id]
                        self._log(f"[rate-limit] {agent_id} quota reset — resuming normal operations")

                if elapsed < WAIT_REPLY_TIMEOUT:
                    now = time.time()
                    if now - self._last_hold_log >= HOLD_LOG_INTERVAL:
                        self._last_hold_log = now
                        self._log(
                            f"[ocr] ⏸ holding — waiting for {agent_id} reply  "
                            f"({int(elapsed)}s / {int(WAIT_REPLY_TIMEOUT)}s timeout)  "
                            f"— click ↺ Release to skip")
                    return False
                else:
                    # Timeout: release hold but suppress re-inject.
                    # _last_routed_body[agent_id] stays set so the same body
                    # is dismissed if OCR sees it again. Click ↺ Release to force retry.
                    # _waiting_body_hash is intentionally kept so ↺ Release can clear
                    # the dedup ring even after _waiting_reply is gone.
                    # Continue scrolling agent_id's window for SCROLL_GRACE seconds so
                    # a late reply that arrives after timeout stays in the OCR region.
                    self._scroll_grace[agent_id] = time.time() + SCROLL_GRACE
                    self._log(
                        f"[ocr] hold timeout ({int(elapsed)}s) — "
                        f"re-inject suppressed to prevent duplicate; click ↺ Release to force")
                    self._waiting_reply = None
                    self.root.after(0, self._update_ocr_hold_label)
                    return False

            # Body-match guard: dismiss if this is the same body we last routed to
            # this agent. Guard persists until new content naturally replaces it.
            # Checked BEFORE hold-release so stale in-window content cannot trigger
            # a premature hold-release.
            # EXCEPTION: a CD-changer redispatch (source "cd_changer") is an
            # authoritative (re)delivery to a freshly-loaded disk. A premature
            # fail-open dispatch (before the swap, e.g. the chatbox server wasn't
            # up yet at startup) may already have recorded this body — dedup then
            # blocks the CORRECT post-swap delivery ("sentinel in view but not
            # routing"). Let the redispatch through; plain OCR re-reads still dedup.
            body_h = self._msg_hash(body)
            if (body_h == self._last_routed_body.get(agent_id)
                    and source_agent != "cd_changer"):
                self._log(f"[dedup] body matches last sent to {agent_id} — dismissed (↺ to override)")
                return False

            # Hold-release: fires AFTER body-match confirms this is genuinely new content.
            # Old in-window messages (e.g. A-65 still visible) are caught above and never
            # reach this point, so they cannot prematurely release the hold.
            if self._waiting_reply and self._waiting_reply != agent_id:
                self._log(
                    f"[ocr] ✓ reply received from {self._waiting_reply} "
                    f"— hold released")
                self._waiting_reply = None
                self._waiting_body_hash = None
                self.root.after(0, self._update_ocr_hold_label)

            # Same exception as the body-match guard: the CD-changer redispatch
            # is authoritative, so it is not gated by the ring dedup either (the
            # body-match guard above is the primary blocker, but a premature send
            # also seeds this ring — bypass both or the redispatch silently drops).
            if source_agent != "cd_changer" and not self._dedup(body):
                return False

            # Pre-claim the destination hold and body-match guard BEFORE inject.
            # _inject_to_agent can be slow (template matching, window focus) and
            # the OCR tick fires every ~0.5s — without this, _waiting_reply passes
            # through None during inject and the tick re-triggers the source agent.
            self._last_routed_body[agent_id] = body_h
            self._waiting_reply      = agent_id
            self._waiting_since      = time.time()
            self._waiting_body_hash  = self._msg_hash(body)
            self.root.after(0, self._update_ocr_hold_label)

            self._inject_to_agent(agent_id, body)

            # NOTE: do NOT clear _last_routed_body[source_agent] here.
            # Keeping the guard means OCR can't re-route stale in-window content
            # from the source after we've moved on. When source genuinely replies,
            # line 3416 above overwrites the destination's guard with the new hash.
            # If an exact-duplicate resend is ever needed, ↺ Release handles it.

            # Flash source agent's pending indicator green — message routed
            if source_agent:
                self._set_pending_routed(source_agent)

            # Clear pending trigger for source window — message successfully routed
            if source_agent and source_agent in self._pending_trigger:
                self._pending_trigger[source_agent] = None

            # Store first line of body for welfare check context (block ID or reply preview).
            self._last_routed_text[agent_id] = body.splitlines()[0][:120] if body else ""
            # Record the full handoff for the replay reset handshake: the last
            # source→dest message, re-deliverable to dest so a stalled sequence
            # can resume from the last real turn.
            if source_agent:
                self._last_sent_by[source_agent] = body
            self._last_handoff = (source_agent, agent_id, body)
            # Routing is healthy — reset auto-welfare state.
            self._last_route_time = time.time()
            self._welfare_fired   = False

            # Mode trigger: fires only when Agent 1 sends the deliberate [SOC:EXECUTE]
            # command token. Natural-language phrases no longer trigger this — only
            # the exact token does, preventing accidental mode shifts from block content.
            body_low = body.lower()
            if agent_id == "agent2" and self._mode == "module_block":
                if IMPL_TRIGGER_CMD in body:
                    self._mode = "implementation"
                    self.root.after(0, self._update_mode_indicator)
                    self._log("[mode] ✓ IMPLEMENTATION MODE — [SOC:EXECUTE] command received")
            if agent_id == "agent1" and self._mode == "implementation":
                if IMPL_COMPLETE_PHRASE in body_low:
                    self._mode = "module_block"
                    self.root.after(0, self._update_mode_indicator)
                    self._log("[mode] ✓ Implementation complete — MODULE BLOCK MODE restored")
                    self.root.after(0, lambda: self._set_status(
                        "✅ Implementation complete — run Phase 2a Security Audit next"))

            return True

        # Primary: sentinel-delimited protocol
        #   To agent1
        #   "body"
        #   end message now
        for m in SENTINEL_RE.finditer(ocr_text):
            raw_ch  = m.group(1)
            digit   = _OCR_DIGIT_NORM.get(raw_ch, raw_ch)
            if digit not in ("1", "2", "3", "4", "5", "6", "7"):
                continue
            matched += 1
            agent_id = f"agent{digit}"
            body = m.group(2).strip().strip('"\'').strip()
            if not body:
                continue
            # Strip embedded routing artifacts: LLMs sometimes echo prior
            # routing blocks inside their response body (e.g. Copilot shows
            # conversation history). Anything from a "To AgentN" line onwards
            # is echoed context, not new content — split there and keep only
            # the part before the first embedded routing header.
            _body_trimmed = re.split(r"(?im)^to\s+agent\s*\d", body)[0].strip()
            if _body_trimmed:
                body = _body_trimmed
            # Pure-ack bodies are attendance echoes, never content. Routing one
            # feeds the ack loop: A2 saves it as a "module block", confirms
            # "module block SOC-ACK-1 saved…" back to A1, and a weak A1 parrots
            # the ack forever (observed live 2026-07-13). Drop at the source.
            if _PURE_ACK_RE.match(body):
                self._log(f"[route] ack echo dropped (not content): {body[:40]!r}")
                continue
            # Self-modification gate — hard boundary before any routing.
            gate = getattr(self, "_self_mod_gate", None)
            if gate is not None and not gate.check_and_prompt(
                    source_agent or "unknown", agent_id, body):
                self._log(f"[gate] ⛔ self-mod request denied "
                          f"({source_agent} → {agent_id})")
                continue
            # Agent 4 — direct dispatch to V plugin (no OCR window needed)
            if digit == "4":
                # CD changer: ensure the vision disk is loaded before dispatch
                _cd_ok, _cd_why = self._cd_disk_ready("agent4")
                if not _cd_ok:
                    # Automatic CD change: swap the disk and park the message for
                    # redispatch when it's up — never dropped while the CD changes.
                    if self._cd_auto_swap("agent4", body, source_agent):
                        self._log("[route] 💿 agent4 disk swapping — message parked")
                    else:
                        self._log(f"[route] ⏸ agent4 deferred — {_cd_why}")
                    continue
                # Unified local-GPU lock: A4 and A5 share the GPU — only one infers.
                if not self._gpu_try_acquire("agent4"):
                    self._log("[route] ⏸ agent4 deferred — local GPU busy (agent5 inferring)")
                    continue
                if self._vplugin is None:
                    self._log("[route] ⚠ To Agent4 seen but V plugin not loaded")
                    self._gpu_release("agent4")
                    continue
                if self._vplugin.route_to_agent4(body, source_agent):
                    routed += 1
                else:
                    self._gpu_release("agent4")     # dispatch failed — free the slot
                continue
            # Agents 5/6/7 — local CD-changer disk agents. All ride the SAME
            # physical channel (the agent5-calibrated GGUF Chatbox chat window +
            # the agent5 GPU lock); the digit only selects WHICH disk must be
            # loaded (A5→MODEL 1, A6→MODEL 2, A7→MODEL 3, token-overridable).
            if digit in ("5", "6", "7"):
                lid = f"agent{digit}"           # logical disk identity
                # CD changer: ensure this agent's disk is loaded before dispatch
                _cd_ok, _cd_why = self._cd_disk_ready(lid)
                if not _cd_ok:
                    if self._cd_auto_swap(lid, body, source_agent):
                        self._cd_release_shared_hold(source_agent)
                        self._log(f"[route] 💿 {lid} disk swapping — message parked")
                    else:
                        self._log(f"[route] ⏸ {lid} deferred — {_cd_why}")
                    continue
                # Unified local-GPU lock: only one local agent may infer at a time.
                # Lock + injection + reply-detection all run as the agent5 channel.
                if not self._gpu_try_acquire("agent5"):
                    self._log(f"[route] ⏸ {lid} deferred — local GPU busy")
                    continue
                # Head-guidance: tell the disk who it is this turn + the envelope,
                # ADAPTED to the loaded model (weak models also get relay-fidelity).
                _tc = self._agent_tool_capable(lid)
                if _try_route("agent5", _local_agent_header(digit, _tc) + body):
                    routed += 1
                else:
                    self._gpu_release("agent5")     # dispatch failed — free the slot
                continue
            if _try_route(agent_id, body):
                routed += 1

        # Fallback: inline single-line  "to agent1: message".
        # Gate on `matched` (was any sentinel-delimited header PARSED?), NOT
        # `routed` (was a message DELIVERED?). A block header that triggers a
        # CD-changer swap PARKS its message and leaves routed==0 — but the header
        # WAS recognized and is being handled, so the top "To AgentX" must take
        # priority and suppress this fallback. Otherwise an inline "to Agent6:"
        # inside the body (e.g. "Relay to Agent6: …") forks the message to a
        # second agent while the real envelope is still swapping.
        if matched == 0:
            for m in INLINE_RE.finditer(ocr_text):
                raw_ch  = m.group(1)
                digit   = _OCR_DIGIT_NORM.get(raw_ch, raw_ch)
                if digit not in ("1", "2", "3", "4", "5", "6", "7"):
                    continue
                matched += 1
                agent_id = f"agent{digit}"
                body = m.group(2).strip().strip('"\'').strip()
                if not body:
                    continue
                if _PURE_ACK_RE.match(body):
                    self._log(f"[route] ack echo dropped (inline, not content): {body[:40]!r}")
                    continue
                gate = getattr(self, "_self_mod_gate", None)
                if gate is not None and not gate.check_and_prompt(
                        source_agent or "unknown", agent_id, body):
                    self._log(f"[gate] ⛔ self-mod request denied "
                              f"({source_agent} → {agent_id})")
                    continue
                if digit == "4":
                    _cd_ok, _cd_why = self._cd_disk_ready("agent4")
                    if not _cd_ok:
                        if self._cd_auto_swap("agent4", body, source_agent):
                            self._log("[route] 💿 agent4 (inline) disk swapping — message parked")
                        else:
                            self._log(f"[route] ⏸ agent4 (inline) deferred — {_cd_why}")
                        continue
                    if not self._gpu_try_acquire("agent4"):
                        self._log("[route] ⏸ agent4 (inline) deferred — local GPU busy (agent5)")
                        continue
                    if self._vplugin is None:
                        self._log("[route] ⚠ to agent4 (inline) but V plugin not loaded")
                        self._gpu_release("agent4")
                        continue
                    if self._vplugin.route_to_agent4(body, source_agent):
                        routed += 1
                    else:
                        self._gpu_release("agent4")
                    continue
                if digit in ("5", "6", "7"):
                    lid = f"agent{digit}"       # logical disk identity (A5/A6/A7)
                    _cd_ok, _cd_why = self._cd_disk_ready(lid)
                    if not _cd_ok:
                        if self._cd_auto_swap(lid, body, source_agent):
                            self._cd_release_shared_hold(source_agent)
                            self._log(f"[route] 💿 {lid} (inline) disk swapping — message parked")
                        else:
                            self._log(f"[route] ⏸ {lid} (inline) deferred — {_cd_why}")
                        continue
                    if not self._gpu_try_acquire("agent5"):
                        self._log(f"[route] ⏸ {lid} (inline) deferred — local GPU busy")
                        continue
                    _tc = self._agent_tool_capable(lid)
                    if _try_route("agent5", _local_agent_header(digit, _tc) + body):
                        routed += 1
                    else:
                        self._gpu_release("agent5")
                    continue
                if _try_route(agent_id, body):
                    routed += 1

        # Sentinel-only fallback: "To AgentN" header scrolled above OCR region top
        # but "end message now" is visible. SOC knows from _waiting_reply who should
        # be replying — use that context to route without needing the header in view.
        # This handles growing message bodies that push the header out of frame.
        if routed == 0 and matched == 0 and source_agent:
            _reply_dest = {"agent2": "agent1", "agent1": "agent2"}.get(source_agent)
            _sent_m = re.search(r"(?im)^end\s+message\s+now", ocr_text)
            if _sent_m and _reply_dest and self._waiting_reply == source_agent:
                _fallback_body = ocr_text[:_sent_m.start()].strip()
                if _fallback_body:
                    self._log(
                        f"[route] sentinel-only fallback: header out of OCR scope "
                        f"({len(_fallback_body)} chars) — routing {source_agent}→{_reply_dest}")
                    if _try_route(_reply_dest, _fallback_body):
                        routed += 1

        if routed == 0 and matched == 0:
            self._log(
                f"[route] ⚠ no routing block matched in {len(ocr_text)} chars — "
                "ensure format is exactly:  To AgentX  /  body  /  end message now  "
                "(trigger header and sentinel each on their own line)")
            # Diagnostic: show header and sentinel context so format issues are visible
            m_diag = re.search(r"(?i)to\s+agent\s*.{0,60}", ocr_text)
            if m_diag:
                ctx_start = max(0, m_diag.start() - 10)
                self._log(f"[route] ⚠ header context: {repr(ocr_text[ctx_start:ctx_start+120])}")
            m_sent = re.search(r"(?i)end\s+message\s+now", ocr_text)
            if m_sent:
                ctx_start = max(0, m_sent.start() - 60)
                self._log(f"[route] ⚠ sentinel context: {repr(ocr_text[ctx_start:m_sent.end()+20])}")
        return routed

    @staticmethod
    def _msg_hash(text: str) -> str:
        """Stable hash of a message body — normalises whitespace so OCR
        variation (extra spaces, different line endings) hashes identically."""
        normalised = " ".join(text.lower().split())
        return hashlib.md5(normalised.encode()).hexdigest()

    def _auto_hunt_suppressed(self, aid: str) -> bool:
        """True while a recent failed sentinel-only trigger-hunt is cooling down.
        Stops the tick from re-launching the hunt every cycle on the same stale
        sentinel — the hunt scrolls, which changes the OCR hash and defeats the
        tick's dedup, so without a cooldown it churns forever (observed live
        2026-07-15 when the bridge went quiet and A7 never replied)."""
        return time.time() < self._auto_hunt_cool.get(aid, 0.0)

    def _dedup(self, text: str) -> bool:
        """Return True if text is new (not seen before). Thread-safe.
        Uses OrderedDict so oldest hashes are evicted first at MAX_SEEN_HASHES.
        Call _dedup_clear(hash) before this to allow a one-time re-injection."""
        h = self._msg_hash(text)
        with self._dedup_lock:
            if h in self._seen_hashes:
                return False
            self._seen_hashes[h] = None
            while len(self._seen_hashes) > MAX_SEEN_HASHES:
                self._seen_hashes.popitem(last=False)
        return True

    def _dedup_clear(self, h: str) -> None:
        """Remove a hash from the seen-hashes set so the next _dedup call passes."""
        with self._dedup_lock:
            self._seen_hashes.pop(h, None)

    # ── OCR watcher ───────────────────────────────────────────────────────────

    def _update_ocr_hold_label(self):
        """Refresh the OCR status label and ↺ Release button to reflect hold state."""
        if not self._ocr_running:
            return
        if self._waiting_reply:
            self.ocr_lbl.config(
                text=f"OCR: ⏸ waiting {self._waiting_reply}…", fg=YELLOW)
            self._ocr_release_btn.config(bg=RED, fg="white")
        elif time.time() < self._rapid_until:
            self.ocr_lbl.config(text="OCR: RAPID ⚡", fg=YELLOW)
            self._ocr_release_btn.config(bg=BG2, fg=YELLOW)
        else:
            self.ocr_lbl.config(text="OCR: scanning…", fg=GREEN)
            self._ocr_release_btn.config(bg=BG2, fg=YELLOW)

    def _scroll_agent_down(self, agent_id: str) -> None:
        """Scroll the agent's chat window down so the tail of its reply is visible.
        Saves and restores the cursor position to avoid disrupting the user."""
        cfg = self.agents.get(agent_id)
        if not cfg:
            return
        # Pick scroll target: prefer OCR-region midpoint (always inside the chat body)
        if cfg.ocr_region:
            rx0, ry0, rx1, ry1 = cfg.ocr_region
            x, y = (rx0 + rx1) // 2, (ry0 + ry1) // 2
        elif cfg.scroll_dn_xy:
            x, y = cfg.scroll_dn_xy
        else:
            return
        # NOTE: pre-S8 this read win32api with NO import in scope — a silent
        # NameError made the whole scroll a no-op. The seam fixed that.
        try:
            orig = PLATFORM.cursor_pos()
            pyautogui.scroll(-5, x, y)   # negative = scroll down
            PLATFORM.set_cursor_pos(*orig)
        except Exception:
            pass

    # ── A1 stall-breaker ──────────────────────────────────────────────────────
    def _agent1_should_stall_scroll(self, now: float | None = None) -> bool:
        """Decision for the A1 stall-breaker (extracted for testability).

        Scroll A1 to the bottom ONLY when the run is genuinely stuck on it:
          • OCR running (a run is in progress),
          • not PAUSED/E-STOPPED,
          • the operator isn't the one mousing (hands yield to priority 0),
          • no local agent is mid-inference (GPU lock free — an A5/6/7 generation
            is legitimate slow work, NOT a stall, so leave it alone),
          • A1 has an OCR region, and
          • nothing has routed for A1_STALL_SCROLL_AFTER seconds.
        This keeps it silent during a healthy ping-pong and only fires it when
        A1 is the thing wedged (almost always the copy button out of view)."""
        now = now if now is not None else time.time()
        if not getattr(self, "_ocr_running", False):
            return False
        if getattr(self, "_estop", False):
            return False
        if _hands_operator_active():
            return False
        if getattr(self, "_gpu_holder", None):
            return False
        if now - getattr(self, "_last_route_time", now) < A1_STALL_SCROLL_AFTER:
            return False
        cfg = self.agents.get("agent1")
        return bool(cfg and cfg.ocr_region)

    def _agent1_scroll_to_bottom(self) -> None:
        """Strong scroll to the bottom of A1's window (several bursts), cursor
        saved/restored. The scroll goes through the hands-guarded pyautogui.scroll,
        so it still yields to the operator and freezes under PAUSE mid-burst."""
        cfg = self.agents.get("agent1")
        if not cfg or not cfg.ocr_region:
            return
        rx0, ry0, rx1, ry1 = cfg.ocr_region
        x, y = (rx0 + rx1) // 2, (ry0 + ry1) // 2
        try:
            orig = PLATFORM.cursor_pos()
        except Exception:
            orig = None
        try:
            for _ in range(A1_STALL_SCROLL_BURSTS):
                pyautogui.scroll(-15, x, y)   # negative = scroll down
                time.sleep(0.05)
        except Exception:
            pass
        finally:
            if orig is not None:
                try:
                    PLATFORM.set_cursor_pos(*orig)
                except Exception:
                    pass

    def _agent1_stall_scroll_loop(self) -> None:
        """Background watchdog (daemon): every A1_STALL_SCROLL_INTERVAL seconds,
        break A1 out of a copy-button stall by scrolling it to the bottom — but
        only when `_agent1_should_stall_scroll` says the run is truly wedged on
        A1. Runs for the app's life; no-ops while OCR is off."""
        while True:
            time.sleep(A1_STALL_SCROLL_INTERVAL)
            try:
                if not self._agent1_should_stall_scroll():
                    continue
                gap = int(time.time() - self._last_route_time)
                self._log(f"[stall-scroll] A1 no route in {gap}s, GPU idle — "
                          f"scrolling A1 to bottom to reveal copy button")
                self._agent1_scroll_to_bottom()
            except Exception as e:
                self._log(f"[stall-scroll] error: {e}")

    def _scroll_agent_up(self, agent_id: str, n: int = 3) -> None:
        """Scroll the agent's chat window up by n scroll clicks to reveal earlier content."""
        cfg = self.agents.get(agent_id)
        if not cfg or not cfg.ocr_region:
            return
        rx0, ry0, rx1, ry1 = cfg.ocr_region
        x, y = (rx0 + rx1) // 2, (ry0 + ry1) // 2
        # NOTE: pre-S8 this read win32api with NO import in scope — a silent
        # NameError made the whole scroll a no-op. The seam fixed that.
        try:
            orig = PLATFORM.cursor_pos()
            pyautogui.scroll(n * 5, x, y)   # positive = scroll up
            PLATFORM.set_cursor_pos(*orig)
        except Exception:
            pass

    def _ocr_grab(self, agent_id: str) -> str:
        """Grab and OCR the current content of agent_id's configured region."""
        cfg = self.agents.get(agent_id)
        if not cfg or not cfg.ocr_region:
            return ""
        rx0, ry0, rx1, ry1 = cfg.ocr_region
        try:
            img = ImageGrab.grab(bbox=(rx0, ry0, rx1, ry1), all_screens=True)
        except Exception:
            return ""
        img = self._apply_blindzone(img, rx0, ry0)
        return pytesseract.image_to_string(_prepare_img_for_ocr(img), config="--psm 6")

    def _ocr_release_hold(self):
        """Manually clear the hold state — bound to the ↺ button.
        Works whether hold is active OR already timed out (post-timeout dedup block).
        After timeout _waiting_reply is None but _waiting_body_hash and
        _last_routed_body may still be blocking — this clears them both."""
        held      = self._waiting_reply
        body_hash = self._waiting_body_hash

        self._waiting_reply     = None
        self._waiting_since     = 0.0
        self._waiting_body_hash = None

        if held:
            self._log(f"[ocr] hold manually released (was waiting for {held}) — body-match block cleared")
            self._last_routed_body.pop(held, None)
        elif self._last_routed_body or body_hash:
            # Post-timeout: _waiting_reply already cleared at timeout but blocks remain.
            self._log("[ocr] ↺ — post-timeout body-match blocks cleared, ready to resend")
            self._last_routed_body.clear()

        if body_hash:
            self._dedup_clear(body_hash)

        self._update_ocr_hold_label()

    def _toggle_manual_hold(self, agent_id: str):
        """Toggle the per-agent manual hold. While held, OCR will not route FROM that agent.
        Stays held until the user clicks Resume."""
        held = not self._manual_hold[agent_id]
        self._manual_hold[agent_id] = held
        _short_map = {"agent1": "A1", "agent2": "A2", "agent3": "A3", "agent5": "A5"}
        short = _short_map.get(agent_id, agent_id)
        btn = self._hold_btns[agent_id]
        if held:
            btn.config(text=f"▶ Resume {short}", bg=RED, fg="white",
                       activebackground="#c04040")
            self._log(f"[hold] {agent_id} paused — outgoing messages from {agent_id} blocked; auto-releases after next send")
        else:
            btn.config(text=f"⏸ Hold {short}", bg=BG2, fg=FG,
                       activebackground=BG2)
            self._log(f"[hold] {agent_id} resumed")

    def _launch_autoaccept_mode(self):
        """Skip Phase 1 calibration and jump straight to Phase 2 auto-accept scan.
        Autoclick template matching works without window calibration — OCR routing
        will be inactive until windows are calibrated, but VS Code auto-accept works."""
        self._log("[auto-accept] skipping calibration → Phase 2 autoclick-only mode")
        self._show_phase(3)

    def _toggle_disable_vplugin(self):
        """Toggle V plugin on/off. When disabled, v_plugin is not loaded even if the
        file is present — lets SOCU start cleanly when no vision server is available."""
        self._disable_vplugin = not self._disable_vplugin
        if self._disable_vplugin:
            self._vplugin = None
            self.root.after(0, self._refresh_agent4_button)
            self.root.after(0, self._refresh_start_v_button)
            self.root.after(0, self._refresh_smart_cal_button)
            if hasattr(self, "_disable_v_btn"):
                self._disable_v_btn.config(text="V:⊘", fg="#666666")
            self._log("[v_plugin] disabled — toggle V:on to re-enable")
        else:
            if hasattr(self, "_disable_v_btn"):
                self._disable_v_btn.config(text="V:on", fg="#4ec9b0")
            if self._vplugin is None:
                self._log("[v_plugin] re-enabled — loading…")
                self._load_plugins()
        self._save_config()

    def _refresh_a2_mode_button(self):
        """Sync the Agent 2 read-mode button label/colour to current state.
        file  → green  'A2:file'   (the agent writes to agent2_outbox/)
        ocr   → orange 'A2:ocr'    (legacy screen-scrape path)"""
        btn = getattr(self, "_a2_mode_btn", None)
        if btn is None:
            return
        if self._agent2_read_mode == "file":
            btn.config(text="A2:file", fg="#4ec9b0")
        else:
            btn.config(text="A2:ocr", fg=ORANGE)

    def _toggle_a2_read_mode(self):
        """Switch Agent 2 between file-relay and OCR reading.
        file: read the reply from agent2_outbox/ (robust, no screen).
        ocr:  legacy scroll-accumulate screen scrape (fallback)."""
        self._agent2_read_mode = "ocr" if self._agent2_read_mode == "file" else "file"
        self._refresh_a2_mode_button()
        if self._agent2_read_mode == "file":
            # Drop any stale OCR-side state for agent2 so the switch is clean.
            self._scroll_accum_active["agent2"] = False
            self._scroll_accum["agent2"] = ""
            self._log(f"[a2-mode] file-relay — Agent 2 writes to {AGENT2_OUTBOX_DIR.name}/")
        else:
            self._log("[a2-mode] OCR — legacy screen-scrape path for Agent 2")
        self._save_config()

    def _toggle_bypass_agent3(self):
        """Toggle Agent 3 bypass. When bypassed, agent3 OCR region is not scanned and
        no traffic is routed to or from agent3. Shows/hides the agent3 panel and
        Hold A3 button accordingly."""
        self._bypass_agent3 = not self._bypass_agent3
        if self._bypass_agent3:
            self._a3_bypass_btn.config(text="⊘ Agent 3  [bypassed]", fg="#666666")
            self._a3_panel_frame.pack_forget()
            if "agent3" in self._hold_btns:
                self._hold_btns["agent3"].pack_forget()
            if hasattr(self, "_p2_bypass_a3_btn"):
                self._p2_bypass_a3_btn.config(text="⊘ A3", fg="#666666")
            self._log("[agent3] bypassed — agent3 OCR and routing disabled")
        else:
            self._a3_bypass_btn.config(text="● Agent 3  [active]", fg=GREEN)
            self._a3_panel_frame.pack(fill="x")
            if "agent3" in self._hold_btns:
                self._hold_btns["agent3"].pack(side="left", padx=(0, 4),
                                               before=self._pause_btn)
            if hasattr(self, "_p2_bypass_a3_btn"):
                self._p2_bypass_a3_btn.config(text="● A3", fg=GREEN)
            self._log("[agent3] active — agent3 OCR and routing enabled")
        self.root.after(0, self._update_attendance_ui)
        self.root.after(0, self._check_phase1_complete)
        self.root.after(50, self._fit_window)
        self._save_config()

    def _toggle_bypass_agent5(self):
        """Toggle Agent 5 bypass. When bypassed, agent5 (GGUF Chatbox) OCR region is not
        scanned and no traffic is routed to or from agent5. Shows/hides the agent5 panel."""
        self._bypass_agent5 = not self._bypass_agent5
        if self._bypass_agent5:
            self._a5_bypass_btn.config(
                text="⊘ Agent 5  [bypassed]  (GGUF Chatbox)", fg="#666666")
            self._a5_panel_frame.pack_forget()
            self._log("[agent5] bypassed — agent5 GGUF Chatbox OCR and routing disabled")
        else:
            self._a5_bypass_btn.config(
                text="● Agent 5  [active]  (GGUF Chatbox)", fg=GREEN)
            self._a5_panel_frame.pack(fill="x")
            self._log("[agent5] active — agent5 GGUF Chatbox OCR and routing enabled")
        self.root.after(0, self._update_attendance_ui)
        self.root.after(0, self._check_phase1_complete)
        self.root.after(50, self._fit_window)
        self._save_config()

    def _toggle_pause(self):
        """Pause/resume all routing. While paused OCR keeps scanning but nothing injects.
        On resume, body-match guards are cleared so current window content routes fresh."""
        self._paused = not self._paused
        if self._paused:
            self._pause_btn.config(text="▶ Resume", bg=RED, fg="white",
                                   activebackground="#c04040")
            self._log("[pause] ⏸ workflow paused — coach your agents, then click ▶ Resume")
        else:
            self._welfare_fired   = False
            self._last_route_time = time.time()
            self._pause_btn.config(text="⏸ Pause", bg=BG2, fg=FG,
                                   activebackground=BG2)
            self._log("[pause] ▶ workflow resumed — routing live")

    def _reset_hold_buttons(self):
        """Reset all manual hold buttons to idle state."""
        _short_map = {"agent1": "A1", "agent2": "A2", "agent3": "A3", "agent5": "A5"}
        for aid, btn in self._hold_btns.items():
            short = _short_map.get(aid, aid)
            btn.config(text=f"⏸ Hold {short}", bg=BG2, fg=FG, activebackground=BG2)
        self._log("[hold] holds reset")

    def _send_coaching_message(self):
        """Inject a module/block structure reminder to Agent 1.
        Uses 'execute' instead of 'implement' to avoid triggering implementation mode."""
        project = self._project_name_var.get().strip()
        project_line = f"Active project: {project}\n\n" if project else ""
        msg = (
            f"[SOC COACHING — MODULE BLOCK REMINDER]\n"
            f"{project_line}"
            "Modules are lettered crates (A, B, C...). Each module contains numbered blocks.\n"
            "Blocks are self-contained chunks that Agent 2 will write and save in order.\n"
            "When all blocks are delivered and authorized, Agent 2 will execute the saved "
            "blocks in alphanumeric sequence.\n\n"
            "Deliver one block at a time via the relay format:\n"
            "To Agent2\n[block content]\nend message now\n\n"
            "Wait for Agent 2's confirmation before sending the next block."
        )
        threading.Thread(
            target=lambda: self._inject_to_agent("agent1", msg),
            daemon=True).start()
        self._log("[coach] module block reminder sent to Agent 1")

    def _send_quiz_message(self):
        """Ask Agent 1 to confirm its awareness of project scope and remaining work."""
        project = self._project_name_var.get().strip()
        project_line = f"Active project: {project}\n\n" if project else ""
        msg = (
            f"[SOC QUIZ — PROJECT STATUS CHECK]\n"
            f"{project_line}"
            "Answer the following in plain text for the user only. "
            "Do NOT use the To AgentX relay format in this response.\n\n"
            "1. How many lettered modules (crates) does this project have? "
            "List each letter and its crate name.\n"
            "2. How many blocks have been delivered to Agent 2 so far?\n"
            "3. Approximately how many blocks remain before all are saved "
            "and the project can be executed?\n"
            "4. What is the coordinate of the next block to be sent?"
        )
        threading.Thread(
            target=lambda: self._inject_to_agent("agent1", msg),
            daemon=True).start()
        self._log("[quiz] project status check sent to Agent 1")

    def _inject_format_reminder(self, agent_id: str):
        """Inject a routing-envelope format reminder into a Claude agent slot.

        Reminds the model to wrap responses in:
            To Agent1 / [body] / end message now
        The 'To AgentN' header is rewritten to 'To Claude' by _is_claude_window
        before it reaches the model, so Claude sees the correct framing.
        """
        digit = agent_id.replace("agent", "")
        msg = (
            f"To Agent{digit}\n"
            "[FORMAT REMINDER]\n"
            "Please wrap every response you send in this routing envelope:\n\n"
            "To Agent1\n"
            "[your response here]\n"
            "end message now\n\n"
            "Use To Agent1, To Agent2, or To Agent3 depending on who the "
            "response is addressed to. "
            "The line 'end message now' must always be the last line.\n"
            "end message now"
        )
        self._inject_to_agent(agent_id, msg)
        self._log(f"[fmt-reminder] envelope format reminder sent to {agent_id}")

    def _welfare_check(self):
        """Replay reset handshake — re-deliver the last routed handoff to its
        destination so a stalled agent receives the previous turn again and emits
        the natural next message in the sequence.

        Replaces the old 'where am I' questionnaire: instead of asking the agents
        to self-report, we replay the last real message (source→dest, verbatim)
        into dest. Injected directly, bypassing OCR routing. Dedup/hold guards are
        cleared first so the replay is not dismissed as a duplicate.

        Fires from the manual button and from the auto-stall detector."""
        handoff = self._last_handoff
        if not handoff:
            self._log("[reset] no prior handoff to replay — nothing to resync "
                      "(send the first message to seed the sequence)")
            self._welfare_fired = True
            return

        source, dest, body = handoff
        if not body or not body.strip():
            self._log("[reset] last handoff body empty — nothing to replay")
            self._welfare_fired = True
            return

        src_lbl = source or "?"
        self._log(f"[reset] replay handshake — re-delivering last {src_lbl}→{dest} "
                  f"message ({len(body)} chars) into {dest}")
        self._log(f"[reset] replay body: {body.splitlines()[0][:80]}…")

        # Clear dedup/body-match + hold so the replay isn't dismissed as a duplicate.
        self._last_routed_body.clear()
        if self._waiting_body_hash:
            self._dedup_clear(self._waiting_body_hash)
        # Re-establish the hold on dest so SOC waits for dest's fresh reply, exactly
        # as a normal route would. dest emits the next turn → routing resumes and
        # resets _welfare_fired on the next successful handoff.
        self._waiting_reply     = dest
        self._waiting_since     = time.time()
        self._waiting_body_hash = self._msg_hash(body)
        self._welfare_fired     = True   # don't auto-repeat until routing resumes
        self._last_route_time   = time.time()
        self.root.after(0, self._update_ocr_hold_label)

        # Inject the body verbatim — _inject_to_agent adds any per-agent prefixes
        # (noise prefix, project tag, mode header) just as a normal route does.
        threading.Thread(
            target=lambda: self._inject_to_agent(dest, body),
            daemon=True).start()

    def _toggle_ocr(self):
        if self._ocr_running:
            self._ocr_running = False
            self._waiting_reply = None
            self._waiting_since = 0.0
            self._scroll_accum_active.clear()
            self._scroll_accum.clear()
            self._pending_trigger.clear()
            self._last_strip_state.clear()
            self._last_ocr_text.clear()
            if self._autoclick_running:
                self._autoclick_running = False
                self._ac_scan_btn.config(text="▶ Scan", fg=GREEN)
            self.ocr_btn.config(text="▶ Start OCR", bg=GREEN, fg="#1e1e1e",
                                 activebackground="#3aaf7a")
            self.ocr_lbl.config(text="OCR: OFF", fg=FG)
            self._log("[ocr] stopped")
            # Drop the bridge presence marker so the chatbox returns to dormant
            # promptly instead of waiting out the TTL.
            self._bridge_clear_marker()
        else:
            self._ocr_running    = True
            self._waiting_reply  = None
            self._waiting_since  = 0.0
            self._last_route_time = time.time()   # reset stall clock on fresh start
            self._welfare_fired  = False
            # NOTE: do NOT clear _last_routed_body on restart.
            # Keeping per-agent inbox guards prevents stale in-window content
            # from re-routing after every restart (e.g. agent2's old 9-pong).
            # New content always has a different hash and flows normally.
            # Use ↺ Release if a specific guard needs to be force-cleared.
            self.ocr_btn.config(text="■ Stop OCR", bg=RED, fg="white",
                                 activebackground="#c04040")
            self.ocr_lbl.config(text="OCR: scanning…", fg=GREEN)
            self._ocr_thread = threading.Thread(
                target=self._ocr_loop, daemon=True)
            self._ocr_thread.start()
            # A1 stall-breaker: start the watchdog once (it self-gates on
            # _ocr_running, so it simply no-ops whenever OCR is off).
            if (self._agent1_scroll_thread is None
                    or not self._agent1_scroll_thread.is_alive()):
                self._agent1_scroll_thread = threading.Thread(
                    target=self._agent1_stall_scroll_loop, daemon=True)
                self._agent1_scroll_thread.start()
            # SOC bridge watcher: start once, self-gates on _ocr_running. Write
            # the presence marker immediately so the chatbox can begin pushing
            # exact replies without waiting for the first refresh tick.
            self._bridge_write_marker()
            if (self._bridge_thread is None
                    or not self._bridge_thread.is_alive()):
                self._bridge_thread = threading.Thread(
                    target=self._bridge_loop, daemon=True)
                self._bridge_thread.start()
            self._log(f"[ocr] started — {SCAN_NORMAL}s normal / "
                      f"{SCAN_RAPID}s rapid (triggers on 'to agent' spotted)")
            self._log("[ocr] watching for:  To agentX  →  body  →  "
                      "end message now")

    def _ocr_loop(self):
        # Open one mss context for the lifetime of the scan loop — avoids
        # per-tick OS-level context creation/destruction overhead.
        with _mss_ctor() as sct:
            while self._ocr_running:
                if getattr(self, "_estop", False):      # E-STOP: eyes idle too
                    time.sleep(0.3)
                    continue
                try:
                    self._ocr_tick(sct)
                    # Auto-welfare: region pixel-static for 2 min AND routing quiet
                    # for 2 min → fire once. Region still changing = agent working,
                    # welfare suppressed regardless of routing silence.
                    # File-relay agents (e.g. agent2 in file mode) reply via a
                    # watched file, not screen activity. Their OCR region is never
                    # scanned, so _region_last_change stays 0 and the staticness
                    # check would fire welfare every cycle. Skip it for them — a
                    # genuine stall there shows up as no file arriving, not a
                    # frozen screen.
                    _file_relay_wait = (self._waiting_reply == "agent2"
                                        and self._agent2_read_mode == "file")
                    if not self._welfare_fired and not _file_relay_wait:
                        check_aid   = self._waiting_reply or "agent2"
                        if check_aid == "agent2" and self._agent2_read_mode == "file":
                            check_aid = "agent1"   # never base staticness on a file-relay region
                        _last_chg   = self._region_last_change.get(check_aid, 0)
                        idle_secs   = time.time() - _last_chg
                        route_gap   = time.time() - self._last_route_time
                        _swap_active = bool(self._cd_watchers)   # a disk is loading
                        # A local agent mid-inference is SLOW, not stalled — the GPU
                        # lock being held is proof of work in progress. Injecting a
                        # welfare message into its chat mid-generation pollutes the
                        # answer (observed live: welfare text interleaved into A6's
                        # reply). Defer welfare while the local channel is inferring.
                        if check_aid == "agent5" and self._gpu_holder:
                            pass       # patient-wait: inference is the heartbeat
                        elif _welfare_due(_last_chg, time.time(), route_gap,
                                          HEARTBEAT_IDLE, _swap_active):
                            self._welfare_fired = True
                            self._log(
                                f"[welfare] ⟳ auto — {check_aid} region static "
                                f"{int(idle_secs)}s, no routing {int(route_gap)}s → sending welfare check")
                            self.root.after(0, self._welfare_check)
                        elif (route_gap >= HEARTBEAT_IDLE
                                and _last_chg > 0 and not _swap_active):
                            now = time.time()
                            if now - self._last_heartbeat_log >= HOLD_LOG_INTERVAL:
                                self._last_heartbeat_log = now
                                self._log(
                                    f"[heartbeat] {check_aid} still moving "
                                    f"(changed {int(idle_secs)}s ago) — welfare suppressed")
                except OSError as e:
                    if "tesseract" in str(e).lower():
                        self._log(
                            "[ocr] Tesseract binary not found.\n"
                            "      Install from: "
                            "https://github.com/UB-Mannheim/tesseract/wiki\n"
                            "      Default path: "
                            r"C:\Program Files\Tesseract-OCR\tesseract.exe")
                        self._ocr_running = False
                        self.root.after(0, lambda: (
                            self.ocr_btn.config(text="▶ Start OCR", bg=GREEN,
                                                fg="#1e1e1e"),
                            self.ocr_lbl.config(text="OCR: ERROR", fg=RED)))
                        break
                    self._log(f"[ocr] OS error: {e}")
                except Exception as e:
                    self._log(f"[ocr] error: {e}")

                self.root.after(0, self._update_ocr_hold_label)
                in_rapid = time.time() < self._rapid_until
                time.sleep(SCAN_RAPID if in_rapid else SCAN_NORMAL)

    def _ocr_force_scan(self, agent_id: str, skip_lead: bool = False):
        """Proactive scroll-bracket-read-route for one agent, bypassing all dedup.

        Agent1 (Copilot/Edge) uses a dedicated clipboard path — jump to bottom,
        hover to reveal the copy button, click it, parse clipboard. No scroll loops.

        All other agents use the scroll-bracket approach:
          both visible     → route immediately (no scrolling needed)
          sentinel only    → find top: scroll UP until trigger found (detection only)
                             read down: accumulate from trigger to sentinel
          trigger only     → find bottom: scroll DOWN until sentinel confirmed (detection only)
                             go back to top: scroll UP same steps to return to trigger
                             read down: accumulate from trigger to sentinel
          neither visible  → find bottom: scroll DOWN to confirm sentinel exists (stale if not)
                             find top: scroll UP until trigger found (detection only)
                             read down: accumulate from trigger to sentinel"""
        cfg = self.agents.get(agent_id)
        if not cfg or not cfg.ocr_region:
            self._log(f"[nudge:{agent_id}] no OCR region configured")
            return

        # Agent1 (Copilot) gets its own clipboard-based read path.
        # Set the flag HERE (not inside _ocr_force_scan_copilot) so the tick loop
        # sees it immediately and cannot spawn a concurrent thread before the flag is set.
        if agent_id == "agent1":
            # Set flag before any check so concurrent callers see it immediately.
            if not self._force_scan_active.get(agent_id):
                self._force_scan_active[agent_id] = True
            already_upstream = (self._waiting_reply and
                                self._waiting_reply != "agent1")
            if already_upstream:
                self._log(f"[nudge:{agent_id}] upstream hold active ({self._waiting_reply}) — skipping copy")
                self._force_scan_active[agent_id] = False
                return
            try:
                self._ocr_force_scan_copilot(skip_lead)
            except Exception as e:
                # NEVER die silently — SOC runs windowless, an unhandled thread
                # exception is invisible and looks like a mystery stall.
                self._log(f"[nudge:{agent_id}] copy path error: "
                          f"{e.__class__.__name__}: {e}")
            finally:
                self._force_scan_active[agent_id] = False
            return

        self._force_scan_active[agent_id] = True
        try:
            self._last_ocr_text.pop(agent_id, None)
            self._last_strip_state.pop(agent_id, None)

            # ── Phase 0: initial scan ────────────────────────────────────────
            frame = self._ocr_grab(agent_id)
            has_trigger  = bool(TRIGGER_RE.search(frame))
            has_sentinel = any(v in frame.lower() for v in _SENTINEL_VARIANTS)
            self._log(f"[nudge:{agent_id}] initial — trigger={has_trigger} sentinel={has_sentinel}")

            if has_trigger and has_sentinel:
                self._log(f"[nudge:{agent_id}] full message visible — routing {len(frame.strip())} chars")
                self._ocr_process(frame, source_agent=agent_id)
                return

            # ── Phase 1b / 1c: confirm the missing anchor (detection only) ───
            # For sentinel-only: nothing to confirm — sentinel already seen, go to Phase 1.
            # For trigger-only or neither: scroll DOWN to confirm sentinel exists.
            # This is detection only — no accumulation.  If sentinel never appears,
            # the agent is off-format or still typing (STALE).
            verify_steps = 0
            if not has_sentinel:
                _reason = "trigger-only" if has_trigger else "neither visible"
                self._log(f"[nudge:{agent_id}] {_reason} — scrolling down to confirm sentinel")
                probe     = frame
                no_growth = 0
                for step in range(30):
                    if not self._force_scan_active.get(agent_id):
                        return
                    self._scroll_agent_down(agent_id)
                    verify_steps += 1
                    time.sleep(SCROLL_ACCUM_MIN_INTERVAL)
                    frame = self._ocr_grab(agent_id)
                    if any(v in frame.lower() for v in _SENTINEL_VARIANTS):
                        has_sentinel = True
                        has_trigger  = bool(TRIGGER_RE.search(frame))
                        self._log(f"[nudge:{agent_id}] sentinel confirmed at step {step + 1}")
                        break
                    new_probe = self._merge_scroll_text(probe, frame)
                    if new_probe == probe:
                        no_growth += 1
                        if no_growth >= 2:
                            self._log(f"[nudge:{agent_id}] STALE — bottom reached, no sentinel")
                            self._mark_pending_stale(agent_id)
                            return
                    else:
                        no_growth = 0
                    probe = new_probe
                else:
                    if not has_sentinel:
                        self._log(f"[nudge:{agent_id}] STALE — 30 down-scrolls, no sentinel found")
                        self._mark_pending_stale(agent_id)
                        return

                # Return to trigger position: scroll up the same number of steps.
                # (each _scroll_agent_down = 5 units; _scroll_agent_up(n=1) = 5 units up)
                if not has_trigger and verify_steps:
                    self._log(f"[nudge:{agent_id}] returning to top ({verify_steps} up-scrolls)")
                    for _ in range(verify_steps):
                        if not self._force_scan_active.get(agent_id):
                            return
                        self._scroll_agent_up(agent_id, n=1)
                        time.sleep(0.15)
                    frame = self._ocr_grab(agent_id)

            # ── Phase 1: scroll UP to find trigger (detection only) ──────────
            # Covers: sentinel-only initial state, and the returned-to-top path above.
            # No content is accumulated here — frames are checked for trigger only.
            if not has_trigger:
                self._log(f"[nudge:{agent_id}] scrolling up — hunting for trigger")
                for step in range(15):
                    if not self._force_scan_active.get(agent_id):
                        return
                    self._scroll_agent_up(agent_id, n=5)
                    time.sleep(0.25)
                    frame = self._ocr_grab(agent_id)
                    if TRIGGER_RE.search(frame):
                        has_trigger = True
                        self._log(f"[nudge:{agent_id}] trigger found after {step + 1} up-scroll(s)")
                        break

            if not has_trigger:
                self._log(f"[nudge:{agent_id}] trigger not found after scrolling up — aborting")
                # Cool down so the tick's sentinel-only branch can't immediately
                # re-launch this hunt: the up-scroll above changed the OCR hash,
                # which defeats the tick's own dedup, so without this the hunt
                # re-fires every tick forever on the same stale sentinel.
                self._auto_hunt_cool[agent_id] = time.time() + AUTO_HUNT_COOLDOWN
                return

            # ── Phase 2: read DOWN — unidirectional accumulation ─────────────
            # Both anchors confirmed. Start fresh buffer from the trigger-visible frame
            # and scroll down, merging frames with dedup until sentinel lands in buffer.
            accum    = frame
            deadline = time.time() + SCROLL_ACCUM_TIMEOUT
            self._log(f"[nudge:{agent_id}] read-down — accumulating from trigger to sentinel")

            for _step in range(40):
                if any(v in accum.lower() for v in _SENTINEL_VARIANTS):
                    self._log(f"[nudge:{agent_id}] sentinel in buffer — routing {len(accum.strip())} chars")
                    self._last_ocr_text.pop(agent_id, None)
                    self._ocr_process(accum, source_agent=agent_id)
                    return
                if time.time() > deadline or not self._force_scan_active.get(agent_id):
                    break
                self._scroll_agent_down(agent_id)
                time.sleep(SCROLL_ACCUM_MIN_INTERVAL)
                frame = self._ocr_grab(agent_id)
                accum = self._merge_scroll_text(accum, frame)

            # Final sentinel check after loop exhaustion
            if any(v in accum.lower() for v in _SENTINEL_VARIANTS):
                self._log(f"[nudge:{agent_id}] sentinel found (late) — routing {len(accum.strip())} chars")
                self._last_ocr_text.pop(agent_id, None)
                self._ocr_process(accum, source_agent=agent_id)
            elif accum.strip() and TRIGGER_RE.search(accum):
                self._log(f"[nudge:{agent_id}] timeout — routing partial ({len(accum.strip())} chars)")
                self._last_ocr_text.pop(agent_id, None)
                self._ocr_process(accum, source_agent=agent_id)
            else:
                self._log(f"[nudge:{agent_id}] nothing valid to route — scan failed")

        finally:
            self._force_scan_active[agent_id] = False

    @staticmethod
    def _copilot_copy_candidates(copy_xy, fb_x, sentinel_hover_y, ry0, ry1):
        """Ordered copy-button click targets, best first, clamped to the window —
        using ONLY confident, OUTPUT-anchored positions so we never trip Copilot's
        OTHER copy icon. Copilot shows two copy buttons at DIFFERENT vertical
        positions: one beside the user's INPUT bubble and one beside Copilot's
        OUTPUT. If the cursor drifts over an input bubble, the INPUT icon reveals;
        clicking it copies the wrong text and poisons the relay. So we only offer:
          1. a template match on the output copy button (if one was found), then
          2. the OUTPUT sentinel line ('end message now') nudged a few px.
        If NEITHER anchor is known, returns [] — the caller must SCROLL to reveal
        the real button rather than blind-click near the window bottom (which is
        exactly how the wrong icon gets hit). Pure — unit-tested."""
        cands: list[tuple[int, int]] = []
        if copy_xy:
            pt = (int(copy_xy[0]), int(copy_xy[1]))
            if ry0 <= pt[1] <= ry1:
                cands.append(pt)
        if sentinel_hover_y is not None:
            for dy in AGENT1_COPY_NUDGES:
                y = sentinel_hover_y + dy
                if ry0 <= y <= ry1:
                    pt = (fb_x, y)
                    if pt not in cands:
                        cands.append(pt)
        return cands

    def _copilot_copy_grab(self, agent_id, copy_xy, fb_x, sentinel_hover_y, ry0, ry1):
        """Robustly copy Copilot's last response and return it (stripped), or ''
        if every attempt left the clipboard empty.

        Copilot's copy icon is HOVER-REVEALED and needs a beat to arm, so the old
        single click (fast, and on a template miss blind) frequently fired before
        the icon existed → empty clipboard → the recurring A1 stall. This does, per
        candidate point:  re-hover (move AWAY then back, so the toolbar re-reveals
        fresh) → DWELL (let the icon paint + arm) → click → VERIFY the clipboard
        actually changed. On failure it advances to the next candidate (small
        vertical offsets), so a few-px miss or a late paint no longer dead-ends.

        SAFETY: it only clicks confident OUTPUT-anchored points (template match or
        the 'end message now' line). With no such anchor it clicks NOTHING and
        returns '' — a blind click near the bottom could hit Copilot's INPUT copy
        icon and copy the wrong text; scrolling to reveal the real button (done by
        the tick loop / the A1 stall-breaker) is the correct recovery instead."""
        candidates = self._copilot_copy_candidates(
            copy_xy, fb_x, sentinel_hover_y, ry0, ry1)
        if not candidates:
            self._log(
                f"[nudge:{agent_id}] no confident copy anchor (no template, no "
                "sentinel in view) — NOT blind-clicking (would risk the input "
                "copy icon); will scroll + retry")
            return ""
        for attempt, (cx, cy) in enumerate(candidates):
            # Re-hover: park the cursor away, then glide onto the target, so
            # Copilot re-reveals the icon fresh (a cursor sitting still can let
            # the hover toolbar fade back out before we click).
            pyautogui.moveTo(cx, max(ry0, cy - 90), duration=0.08)
            time.sleep(0.10)
            pyautogui.moveTo(cx, cy, duration=0.12)
            time.sleep(AGENT1_COPY_DWELL)      # DWELL — let the icon paint + arm
            pyperclip.copy("")                 # sentinel so we detect a REAL copy
            pyautogui.click(cx, cy)
            for _ in range(AGENT1_COPY_POLLS):  # confirm the copy actually landed
                time.sleep(0.2)
                t = pyperclip.paste()
                if t and t.strip():
                    self._log(
                        f"[nudge:{agent_id}] copy landed on attempt {attempt + 1}/"
                        f"{len(candidates)} at ({cx},{cy}) — {len(t.strip())} chars")
                    return t.strip()
        self._log(
            f"[nudge:{agent_id}] copy missed after {len(candidates)} hover-dwell "
            "attempt(s) — clipboard still empty")
        return ""

    def _ocr_force_scan_copilot(self, skip_lead: bool = False):
        """Clipboard-based read path for agent1 (Copilot/Edge) only.

        Sequence:
          1. Focus Copilot window
          2. Click down arrow at known fixed location (1347,904) to jump to bottom;
             fall back to Ctrl+End if the arrow is not visible (already at bottom)
          3. Hover over the response body — Copilot reveals its action icons
          4. Template-match Copilot_copy_button.PNG — click to copy last response
          5. Read clipboard → parse trigger+body+sentinel → route

        _waiting_reply and _force_scan_active are managed by _ocr_force_scan (caller)."""
        agent_id = "agent1"
        cfg = self.agents.get(agent_id)
        if not cfg or not cfg.ocr_region:
            return

        self._last_ocr_text.pop(agent_id, None)
        self._last_strip_state.pop(agent_id, None)

        rx0, ry0, rx1, ry1 = cfg.ocr_region
        # Copy button sits ~153px from OCR left edge, ~41px above OCR bottom.
        # Define up front so hover sweep uses the correct x (NOT centre).
        fb_x = rx0 + 153
        fb_y = ry1 - 41

        # ── Focus Copilot window ──────────────────────────────────────────────
        try:
            PLATFORM.focus_window(cfg.hwnd)
            time.sleep(0.25)
        except Exception as e:
            self._log(f"[nudge:{agent_id}] focus error: {e}")

        # ── Step 1: scroll to bottom, using the sentinel as the "there yet?" signal ──
        # The "scroll to latest" chevron can't be reliably template-matched
        # (extensively tried) — but that's fine, because it isn't the only signal.
        # The chevron's real job was to say "you're not at the bottom yet." The
        # sentinel phrase "end message now" says the SAME thing and, unlike the
        # chevron icon, OCR can READ it. So: hover the chat centre (inside the OCR
        # region, well above the input field — Copilot scrolls from anywhere over
        # the window EXCEPT the input box) and wheel-scroll a step at a time,
        # checking after each step whether the sentinel is visible. Stop the moment
        # it is (= at bottom, reply complete); short replies that already show the
        # sentinel take zero scrolls. Bounded so a still-streaming or empty reply
        # never spins.
        chat_x = (rx0 + rx1) // 2
        chat_y = (ry0 + ry1) // 2
        if not skip_lead:
            _lead_age = time.time() - self._agent1_lead_observed
            if _lead_age >= 120.0:
                # First auto-trigger this generation — let the reply finish streaming.
                self._log(
                    f"[nudge:{agent_id}] waiting {AGENT1_COPY_LEAD}s "
                    "for message to finish populating…")
                for _i in range(AGENT1_COPY_LEAD):
                    time.sleep(1)
                    if self._manual_hold.get("agent1"):
                        self._log("[nudge:agent1] hold set during lead-time — aborting copy")
                        self._force_scan_active[agent_id] = False
                        return
                self._agent1_lead_observed = time.time()
        pyautogui.moveTo(chat_x, chat_y, duration=0.12)
        time.sleep(0.15)

        def _sentinel_in_view() -> bool:
            """True when 'end message now' is readable in the bottom strip — our
            OCR-visible proxy for 'at the bottom, reply complete'."""
            try:
                _img = ImageGrab.grab(
                    bbox=(rx0, max(ry0, ry1 - 300), rx1, ry1), all_screens=True)
                _txt = pytesseract.image_to_string(
                    _prepare_img_for_ocr(_img), config="--psm 6").lower()
            except Exception:
                return False
            return any(v in _txt for v in _SENTINEL_VARIANTS)

        _at_bottom = False
        _steps = 0
        for _steps in range(12):
            if _sentinel_in_view():
                _at_bottom = True
                break
            pyautogui.scroll(-20, x=chat_x, y=chat_y)
            time.sleep(0.18)
        if _at_bottom:
            self._log(
                f"[nudge:{agent_id}] sentinel in view after {_steps} scroll(s) — at bottom")
        else:
            self._log(
                f"[nudge:{agent_id}] scrolled {_steps + 1}× — sentinel not visible "
                "(still streaming or no reply); will retry next tick")

        # ── Step 2: find copy button anchored to "end message now" ──────────
        # The copy button always appears just below the sentinel phrase.
        # Strategy:
        #   1. OCR the bottom 250px of the window to find "end message now"
        #   2. Hover just below that text to reveal the copy button
        #   3. Template match → click
        #   Fallback: sweep fb_x column bottom→top if OCR anchor fails.
        fb_x = rx0 + 153   # copy button x column in Copilot panel (~1498)
        copy_xy = None

        # 2a. Locate "end message now" via bounding-box OCR
        sentinel_hover_y = None
        try:
            scan_top = max(ry0, ry1 - 600)   # scan bottom 600px — long messages push sentinel high
            _sent_img = ImageGrab.grab(
                bbox=(rx0, scan_top, rx1, ry1), all_screens=True)
            _sent_data = pytesseract.image_to_data(
                _prepare_img_for_ocr(_sent_img),
                config="--psm 6",
                output_type=pytesseract.Output.DICT)
            _texts = [t.lower().strip() for t in _sent_data["text"]]
            for _i, _w in enumerate(_texts):
                if _w in ("now", "ncw", "n0w") and _i > 0:
                    _prev = _texts[_i - 1]
                    if _prev in ("message", "rnessage", "messaqe", "messace"):
                        _bot = _sent_data["top"][_i] + _sent_data["height"][_i]
                        sentinel_hover_y = scan_top + _bot // 2 + 18  # // 2: undo _prepare_img_for_ocr 2× upscale
                        self._log(
                            f"[nudge:{agent_id}] sentinel found at screen_y≈{sentinel_hover_y}")
                        break
        except Exception as _e:
            self._log(f"[nudge:{agent_id}] sentinel OCR error: {_e}")

        # 2b. Hover at sentinel anchor (primary) or sweep (fallback)
        _hover_ys = (
            [sentinel_hover_y] if sentinel_hover_y else []
        ) + list(range(ry1 - 30, ry0 + 40, -40))   # full window height fallback

        for hover_y in _hover_ys:
            pyautogui.moveTo(fb_x, hover_y, duration=0.12)
            time.sleep(0.4)
            # Search only near the hover point — prevents false matches on the
            # window maximize button (same black-square icon) elsewhere on screen.
            found = (self._find_template_at("Copilot_copy_button.PNG", fb_x, hover_y, margin=80)
                     or self._find_template_at("copilot_copy_button.png", fb_x, hover_y, margin=80))
            if found and rx0 <= found[0] <= rx1 and ry0 <= found[1] <= ry1:
                copy_xy = found
                self._log(f"[nudge:{agent_id}] copy button at {copy_xy} (hover_y={hover_y})")
                break

        # ── Step 3+4: hover-dwell → click → VERIFY (robust copy) ──────────────
        # Copilot's copy icon is hover-revealed and needs a beat to arm; the old
        # single fast/blind click fired before it existed and left the clipboard
        # empty (the recurring A1 stall). _copilot_copy_grab dwells, clicks,
        # confirms the clipboard changed, and retries with a re-hover across a
        # few vertical offsets before giving up.
        text = self._copilot_copy_grab(
            agent_id, copy_xy, fb_x, sentinel_hover_y, ry0, ry1)
        if not text or not text.strip():
            self._agent1_copy_fail_at = time.time()
            self._log(f"[nudge:{agent_id}] clipboard empty — cooling {AGENT1_COPY_COOL}s")
            if self._vplugin:
                _ctx = (
                    f"Copilot panel is open in Edge/Chrome. "
                    f"A click was attempted at approx ({fb_x}, {fb_y}) but clipboard "
                    "is still empty. The copy icon (clipboard / overlapping pages) "
                    "appears on hover near the bottom of the last response, just below "
                    "the 'end message now' line. "
                    f"OCR region bottom edge is at y={ry1}. "
                    "Try hovering across the bottom 80px of the response and click the "
                    "copy icon precisely when it appears."
                )
                self._vplugin.nudge_stall("clipboard_empty", agent_id, _ctx)
                self._log(f"[nudge:{agent_id}] Agent4 dispatched — polling clipboard (25s)")
                pyperclip.copy("")
                for _ in range(50):
                    time.sleep(0.5)
                    _recovered = pyperclip.paste()
                    if _recovered and _recovered.strip():
                        self._log(
                            f"[nudge:{agent_id}] Agent4 clipboard recovery — "
                            f"{len(_recovered.strip())} chars — routing")
                        self._last_ocr_text.pop(agent_id, None)
                        self._ocr_process(_recovered, source_agent=agent_id)
                        return
                self._log(f"[nudge:{agent_id}] Agent4 clipboard timeout — cooling")
            return

        has_trigger  = bool(TRIGGER_RE.search(text))
        has_sentinel = any(v in text.lower() for v in _SENTINEL_VARIANTS)
        self._log(
            f"[nudge:{agent_id}] clipboard: {len(text.strip())} chars — "
            f"trigger={has_trigger} sentinel={has_sentinel}")

        if has_trigger and not has_sentinel:
            # Agent wrote the routing header but dropped 'end message now'.
            # We already scrolled to the absolute bottom before copying, so
            # the sentinel is genuinely absent — append it and route anyway.
            self._log(f"[nudge:{agent_id}] sentinel missing — appending and routing")
            text = text.rstrip() + "\nend message now"

        # Clipboard confirmed agent has a complete response — release sequence hold
        # if we were waiting for THIS agent to reply. The copy IS the reply signal.
        if self._waiting_reply == agent_id:
            self._waiting_reply     = None
            self._waiting_body_hash = None
            self.root.after(0, self._update_ocr_hold_label)
            self._log(f"[nudge:{agent_id}] clipboard confirmed reply — sequence hold released")

        # Release the sequence hold if it would block the destination — but ONLY
        # if the clipboard body is genuinely new content. If it matches the last
        # body we sent to that agent, keep the hold and return early: dedup would
        # block it anyway, and releasing the hold creates a tight spin loop where
        # force_scan fires every tick, releases hold, dedup fires, hold is gone,
        # repeat — keeping both agents stuck.
        _dm = re.search(rf"to\s+agent\s*({_D})", text, re.IGNORECASE)
        if _dm:
            _ddigt = _OCR_DIGIT_NORM.get(_dm.group(1), _dm.group(1))
            _dest  = f"agent{_ddigt}"
            if self._waiting_reply == _dest:
                # Extract body to check against what we last sent
                _clip_body = re.sub(r"(?im)^to\s+agent[^\n]*\n", "", text, count=1)
                _clip_body = re.sub(r"(?im)\bend\s+message\s+now.*", "", _clip_body).strip()
                if self._msg_hash(_clip_body) == self._last_routed_body.get(_dest):
                    self._log(
                        f"[nudge:{agent_id}] hold on {_dest} kept — same body as last sent"
                        f" (waiting for {_dest} reply)")
                    return  # nothing new to route
                self._waiting_reply     = None
                self._waiting_body_hash = None
                self.root.after(0, self._update_ocr_hold_label)
                self._log(f"[nudge:{agent_id}] released sequence hold on {_dest} — new content")

        self._last_ocr_text.pop(agent_id, None)
        self._ocr_process(text, source_agent=agent_id)
        # Suppress re-nudge for 30s regardless of route outcome (success or dedup).
        # Without this, OCR immediately sees trigger again and relaunches the nudge
        # in a tight copy-dedup spin loop.
        self._inject_grace[agent_id] = time.time() + 30.0

    def _is_claude_window(self, cfg) -> bool:
        """Return True if cfg's target window is an active Claude instance.

        Uses template matching only — window title is unreliable because the
        VS Code Claude Code extension always reads 'Claude Code' regardless of
        whether Claude or Copilot is the active LLM.

        Any landmark image in 'buttons database/' whose filename contains
        'claude' or 'cld' (case-insensitive) is tried against the top quarter
        of the OCR region. Drop new landmarks in and they are picked up
        automatically — no code change needed.
        """
        if not cfg.ocr_region or not _CV2_OK:
            return False
        rx0, ry0, rx1, ry1 = cfg.ocr_region
        search_x = (rx0 + rx1) // 2
        # Search top quarter of region — that's where header landmarks live
        search_y = ry0 + (ry1 - ry0) // 4
        btn_dir = BASE_DIR / "buttons database"
        try:
            landmarks = [
                p.name for p in btn_dir.iterdir()
                if p.suffix.lower() in (".png", ".jpg")
                and ("claude" in p.stem.lower() or "cld" in p.stem.lower())
            ]
        except Exception:
            return False
        for name in landmarks:
            if self._find_template_at(name, search_x, search_y, margin=300):
                return True
        return False

    def _nudge_active_agent(self) -> str:
        """Return the agent that SOC is currently working on / waiting for.
        Used by clipboard read and cursor nudge to auto-target the right source."""
        # If we're waiting for an agent to reply, that's the active one
        if self._waiting_reply:
            return self._waiting_reply
        # Otherwise default to agent1 (most common stall point)
        return "agent1"

    def _manual_clip_read(self):
        """General clipboard injector — routes whatever is in the clipboard right now
        as output from the currently active agent.  Works for any stall point: user
        manually clicks whatever copy/export button SOC couldn't find, then hits Read Clip.
        SOC injects the content into the routing pipeline exactly as if it read it itself."""
        agent_id = self._nudge_active_agent()
        text = pyperclip.paste()
        if not text or not text.strip():
            self._log("[clip-read] clipboard empty — copy content from the agent window first")
            self.root.after(0, lambda: self._set_status("📋 Clipboard empty — copy first"))
            return
        self._log(f"[clip-read] {len(text.strip())} chars — injecting as {agent_id}")
        self.root.after(0, lambda: self._set_status(
            f"📋 Injecting {len(text.strip())} chars as {agent_id}…"))
        self._last_ocr_text.pop(agent_id, None)
        self._ocr_process(text, source_agent=agent_id)

    # Templates to check per agent when identifying a hover position
    _NUDGE_TEMPLATES: dict[str, list[tuple[str, str]]] = {
        "agent1": [
            ("Copilot_copy_button.PNG",    "copy-btn"),
            ("copilot_copy_button.png",    "copy-btn"),
            ("agent1_scroll_indicator.png","scroll-indicator"),
            ("copilot_down_arrow.PNG",     "down-arrow"),
            ("agent1_send.png",            "send-btn"),
        ],
        "agent2": [
            ("Copilot_copy_button.PNG",    "copy-btn"),   # VS Code Copilot panel (same icon as agent1)
            ("copilot_copy_button.png",    "copy-btn"),
            ("Agent2_copy_center.PNG",     "copy-btn"),
            ("agent2_copy_center.png",     "copy-btn"),
            ("agent2_scroll_dn.png",       "scroll-dn"),
            ("send_message_to_agent2.png", "send-btn"),
            ("agent2_send_CLD.png",         "send-btn"),
        ],
        # Agent3 (Claude CLI) — Anthropic UI update removed the old direct send/input
        # templates. Replacement strategy: the copy button is now hidden behind a
        # hover overlay. User hovers over the visible geo-point marker, the system
        # then offsets 25px UP to reveal and click the copy button.
        #   Broken (removed): send_message_to_claude.png, Claude_chat_input_field.PNG
        "agent3": [
            ("agent3_scroll_dn.png",                 "scroll-dn"),
            ("agent3_send.png",                      "send-btn"),
            ("Agent3_send_input_button.PNG",         "send-btn"),
            ("agent3_input.png",                     "input-field"),
            ("agent3_input_field.png",               "input-field"),
            ("claude_geo_point.PNG",                 "geo-point"),
            ("Agent3_geo_hover_copy_button.png",     "copy-btn"),
        ],
    }
    # Pixel offset for agent3 geo-point → reveal hidden copy button (hover above).
    AGENT3_GEO_REVEAL_DY = -25
    # What comes next after each identified element
    _NUDGE_NEXT_STEP: dict[str, str] = {
        "down-arrow":        "scroll done → hover sweep → reveal copy button",
        "scroll-indicator":  "scroll done → hover sweep → reveal copy button",
        "scroll-dn":         "scroll done → hover sweep → reveal copy button",
        "copy-btn":          "copy click → read clipboard → route to target agent",
        "send-btn":          "send click → message delivered → wait for reply",
        "input-field":       "input focused → paste + send will follow",
        "geo-point":         "hover 25px up → reveal hidden copy button → click → clipboard",
    }

    def _identify_nudge_element(self, x: int, y: int, agent_id: str) -> tuple[str, str]:
        """Identify what UI element the cursor is hovering over.
        Checks three sources:
          1. Calibrated button positions for this agent (send_xy, input_xy, scroll positions)
          2. Recent nudge_log.json historical click positions (geographic memory)
          3. Template match in a ±80px crop around the cursor (visual recognition)
        Returns (identification_string, next_step_hint)."""
        import json as _json
        from math import sqrt as _sqrt
        findings: list[str] = []
        element_label: str  = ""

        # 1. Calibrated positions
        cfg = self.agents.get(agent_id)
        if cfg:
            for pos, label in [
                (cfg.send_xy,      "send-btn"),
                (cfg.input_xy,     "input-field"),
                (cfg.scroll_dn_xy, "scroll-dn"),
                (cfg.scroll_up_xy, "scroll-up"),
            ]:
                if pos:
                    d = _sqrt((pos[0] - x) ** 2 + (pos[1] - y) ** 2)
                    if d < 30:
                        findings.append(f"cal:{label}({int(d)}px)")
                        if not element_label:
                            element_label = label

        # 2. Historical nudge positions from nudge_log.json
        try:
            log_path = BASE_DIR / "nudge_log.json"
            if log_path.exists():
                entries = _json.loads(log_path.read_text(encoding="utf-8"))
                nearby = [
                    (e, _sqrt((e["click_xy"][0] - x) ** 2 + (e["click_xy"][1] - y) ** 2))
                    for e in entries
                    if e.get("agent") == agent_id and isinstance(e.get("click_xy"), list)
                ]
                close = [(e, d) for e, d in nearby if d < 25]
                if close:
                    nearest_e, nearest_d = min(close, key=lambda t: t[1])
                    outcome = nearest_e.get("outcome", "?")
                    count   = len(close)
                    label   = nearest_e.get("identified_as", "").split("|")[0].strip()
                    findings.append(f"hist:{outcome}×{count}({int(nearest_d)}px)")
                    if not element_label and "copy" in outcome:
                        element_label = "copy-btn"
        except Exception:
            pass

        # 3. Visual: template match in ±80px crop around cursor
        try:
            margin = 80
            with _mss_ctor() as sct:
                bbox = {"left": x - margin, "top": y - margin,
                        "width": margin * 2, "height": margin * 2}
                raw = sct.grab(bbox)
                crop_gray = cv2.cvtColor(
                    np.array(Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")),
                    cv2.COLOR_RGB2GRAY)
            best_name, best_conf, best_label = None, 0.0, ""
            for tname, tlabel in self._NUDGE_TEMPLATES.get(agent_id, []):
                tpl_path = TEMPLATE_DIR / tname
                if not tpl_path.exists():
                    continue
                tpl = self._safe_imread(tpl_path, cv2.IMREAD_GRAYSCALE)
                if tpl is None:
                    continue
                th, tw = tpl.shape
                if crop_gray.shape[0] < th or crop_gray.shape[1] < tw:
                    continue
                res = cv2.matchTemplate(crop_gray, tpl, cv2.TM_CCOEFF_NORMED)
                _, conf, _, _ = cv2.minMaxLoc(res)
                if conf > 0.70 and conf > best_conf:
                    best_conf, best_name, best_label = conf, tname, tlabel
            if best_name:
                findings.append(f"visual:{best_label}({best_conf:.2f})")
                if not element_label:
                    element_label = best_label
        except Exception:
            pass

        id_str   = " | ".join(findings) if findings else "no-match (new position)"
        next_str = self._NUDGE_NEXT_STEP.get(element_label, "→ outcome will determine next step")
        return id_str, next_str

    def _click_copy_at_anchor(self, ax: int, ay: int,
                              agent_id: str, is_geo: bool) -> tuple[int, int]:
        """Click the copy element associated with anchor (ax, ay) — clears clipboard first.

        Standard agents (1/2): click directly at (ax, ay).
        Agent3 geo-point: hover AGENT3_GEO_REVEAL_DY px from the anchor to
        reveal the hidden copy button, find its precise template centre, click it.
        Falls back to positional click at the revealed location if template misses.

        Returns the (x, y) where the click actually landed so the calling sequence
        can re-use that for subsequent re-clicks (eg. after scroll-to-bottom
        sentinel recovery). The learning loop still feeds nudge_log.json via
        the caller's standard outcome path — this helper just performs the click.
        """
        pyperclip.copy("")
        if not is_geo:
            pyautogui.click(ax, ay)
            return (ax, ay)

        # Geo-point reveal sweep: hover above the anchor to fire Anthropic's
        # hover overlay, then template-match the now-visible copy button.
        sweep_offsets = (self.AGENT3_GEO_REVEAL_DY,
                         self.AGENT3_GEO_REVEAL_DY - 10,
                         self.AGENT3_GEO_REVEAL_DY - 20,
                         self.AGENT3_GEO_REVEAL_DY + 10)
        copy_xy = None
        last_hover = (ax, ay)
        for dy in sweep_offsets:
            hx, hy = ax, ay + dy
            pyautogui.moveTo(hx, hy, duration=0.22)
            time.sleep(0.5)
            last_hover = (hx, hy)
            copy_xy = self._find_template_at(
                "Agent3_geo_hover_copy_button.png", hx, hy, margin=80)
            if copy_xy:
                self._log(f"[geo-reveal] copy found at {copy_xy} (hover dy={dy})")
                break
        if copy_xy:
            pyautogui.click(*copy_xy)
            return copy_xy
        self._log(f"[geo-reveal] template miss — positional click at {last_hover}")
        pyautogui.click(*last_hover)
        return last_hover

    def _cursor_nudge(self, delay: int = 5):
        """General sequence nudge — 5s countdown then clicks wherever the user's mouse is.
        After clicking, detects what kind of element was hit:
          • Clipboard filled  → copy button — route clipboard as active agent output
          • Clipboard empty   → nav element (scroll arrow, button, etc.) — fire force_scan
                                to continue the sequence from the next step
        This works at ANY stall point: down arrow, copy button, send button, etc.
        Position + outcome are logged to nudge_log.json for future healing."""
        agent_id = self._nudge_active_agent()  # initial guess from sequence state
        for i in range(delay, 0, -1):
            self.root.after(0, lambda n=i: self._set_status(
                f"📍 [{agent_id}] move mouse to stuck element — clicking in {n}s…"))
            time.sleep(1)
        x, y = pyautogui.position()

        # ── Override agent_id by cursor position ─────────────────────────────
        # _nudge_active_agent uses _waiting_reply which may point to the WRONG
        # agent when the user is hovering over the OTHER agent's window.
        # Physical cursor position is the ground truth — use it.
        for _aid, _acfg in self.agents.items():
            if _acfg.ocr_region:
                _rx0, _ry0, _rx1, _ry1 = _acfg.ocr_region
                if _rx0 <= x <= _rx1 and _ry0 <= y <= _ry1:
                    if _aid != agent_id:
                        self._log(
                            f"[cursor-nudge] cursor in {_aid} window "
                            f"— overriding active agent {agent_id}→{_aid}")
                    agent_id = _aid
                    break

        # ── Intelligent hover: identify element before clicking ───────────────
        id_str, next_hint = self._identify_nudge_element(x, y, agent_id)
        self._log(f"[cursor-nudge] agent={agent_id} hover=({x},{y}) → {id_str}")
        self._log(f"[cursor-nudge] sequence position: {next_hint}")
        self.root.after(0, lambda: self._set_status(f"📍 Clicking ({x},{y}) — {id_str}"))

        # ── Focus agent window before clicking ───────────────────────────────
        # Always focus the host window first — VS Code / Electron consume the
        # first click as window-activation and the button action never lands.
        # This applies whether or not the element was identified; for nav
        # elements the extra focus is harmless.
        cfg_focus = self.agents.get(agent_id)
        if cfg_focus and cfg_focus.hwnd:
            try:
                PLATFORM.focus_window(cfg_focus.hwnd)
                time.sleep(0.3)
                # Re-hover so Electron re-renders the hover overlay after focus change.
                pyautogui.moveTo(x, y, duration=0.15)
                time.sleep(0.35)
            except Exception as _fe:
                self._log(f"[cursor-nudge] focus pre-click error: {_fe}")

        # ── Click at anchor — geo-point aware ─────────────────────────────────
        # For agent3 (post-Anthropic-update) the visible "copy" anchor is a
        # geo-point marker; the real copy button is hidden behind a hover layer
        # and only appears when the cursor is ~25px above the geo-point.
        # _click_copy_at_anchor encapsulates the reveal+click so that:
        #   • initial click  (this call)
        #   • sentinel re-copy after scroll-to-bottom (below)
        #   • any future re-click in the existing sequence
        # all use the SAME proven sequence path. The geo-point logic is just a
        # pre-click hook, not a parallel branch.
        is_geo = (agent_id == "agent3" and "geo-point" in id_str)
        click_xy = self._click_copy_at_anchor(x, y, agent_id, is_geo)
        # Poll up to 2s instead of a fixed sleep — Electron clipboard writes
        # are sometimes delayed by 300–800 ms after the click.
        text = ""
        for _ in range(10):
            time.sleep(0.2)
            text = pyperclip.paste()
            if text and text.strip():
                break
        # click_xy is where the click actually landed — use it for re-clicks
        # below so the sequence's "re-click at same position" semantics still
        # work for the geo-point reveal flow.
        rex, rey = click_xy

        # ── Determine outcome and re-enter sequence at the right point ────────
        if text and text.strip():
            # Copy element clicked — clipboard filled
            outcome = "copy_routed"
            has_trigger  = bool(TRIGGER_RE.search(text))
            has_sentinel = any(v in text.lower() for v in _SENTINEL_VARIANTS)
            self._log(
                f"[cursor-nudge] copy confirmed — {len(text.strip())} chars — "
                f"trigger={has_trigger} sentinel={has_sentinel}")

            # Trigger present but sentinel missing: sentinel was below the fold.
            # Scroll source window to absolute bottom and re-copy once automatically.
            if has_trigger and not has_sentinel:
                self._log(
                    "[cursor-nudge] sentinel missing — scrolling to bottom and re-copying")
                cfg_src = self.agents.get(agent_id)
                if cfg_src and cfg_src.ocr_region:
                    rx0, ry0, rx1, ry1 = cfg_src.ocr_region
                    chat_x = (rx0 + rx1) // 2
                    chat_y = (ry0 + ry1) // 2
                    pyautogui.click(chat_x, chat_y)
                    time.sleep(0.2)
                    pyautogui.hotkey("ctrl", "end")
                    time.sleep(0.25)
                    pyautogui.press("end")
                    time.sleep(0.25)
                    pyautogui.scroll(-15, x=chat_x, y=chat_y)
                    time.sleep(0.5)
                pyperclip.copy("")
                # Re-click via the same anchor-aware helper so geo-point reveal
                # is repeated (hidden copy button vanishes when mouse leaves).
                self._click_copy_at_anchor(x, y, agent_id, is_geo)
                time.sleep(0.7)
                text2 = pyperclip.paste()
                if text2 and text2.strip():
                    has_sentinel = any(v in text2.lower() for v in _SENTINEL_VARIANTS)
                    self._log(
                        f"[cursor-nudge] re-copy — {len(text2.strip())} chars — "
                        f"sentinel={has_sentinel}")
                    text = text2

            if not has_trigger:
                self._log(
                    "[cursor-nudge] ⚠ no 'To AgentX' in clipboard — "
                    "check agent used routing format")
            elif not has_sentinel:
                # Sentinel still missing after scroll+re-copy.
                # User explicitly fired the nudge = confirmation message is complete.
                # Append sentinel so routing can proceed.
                self._log(
                    "[cursor-nudge] sentinel still absent — appending for manual nudge route")
                text = text.rstrip() + "\nend message now"
                has_sentinel = True

            # Manual nudge overrides Hold — user explicitly chose to route this message.
            if self._manual_hold.get(agent_id):
                self._manual_hold[agent_id] = False
                _short_map = {"agent1": "A1", "agent2": "A2", "agent3": "A3", "agent5": "A5"}
                _short = _short_map.get(agent_id, agent_id)
                _btn   = self._hold_btns.get(agent_id)
                if _btn:
                    self.root.after(0, lambda b=_btn, s=_short: b.config(
                        text=f"⏸ Hold {s}", bg=BG2, fg=FG, activebackground=BG2))
                self._log(f"[cursor-nudge] Hold {agent_id} released by manual nudge — routing")

            # Also release the sequence hold (_waiting_reply) if it would block
            # the destination we're about to route to — manual nudge = full override.
            if has_trigger and self._waiting_reply:
                _dm = re.search(rf"to\s+agent\s*({_D})", text, re.IGNORECASE)
                if _dm:
                    _ddigt = _OCR_DIGIT_NORM.get(_dm.group(1), _dm.group(1))
                    _dest  = f"agent{_ddigt}"
                    if self._waiting_reply == _dest:
                        self._waiting_reply      = None
                        self._waiting_body_hash  = None
                        self.root.after(0, self._update_ocr_hold_label)
                        self._log(
                            f"[cursor-nudge] sequence hold on {_dest} released "
                            "— manual nudge override")

            self._log("[cursor-nudge] spillway → inject body → target agent input → click send")
            self.root.after(0, lambda: self._set_status(
                f"📍 Copy nudge: {'routing' if has_trigger else '⚠ no trigger'} "
                f"— {len(text.strip())} chars as {agent_id}"))
            self._last_ocr_text.pop(agent_id, None)
            self._ocr_process(text, source_agent=agent_id)
        else:
            # Navigation element clicked (scroll arrow, down arrow, etc.)
            outcome = "nav_continue"
            self._log(
                f"[cursor-nudge] nav confirmed — clipboard empty — "
                "spillway → resuming sequence from next step")
            self._log(f"[cursor-nudge] next: {next_hint}")
            self.root.after(0, lambda: self._set_status(
                f"📍 Nav nudge: sequence continuing for {agent_id}…"))
            # Claim the scan slot BEFORE starting the thread so the OCR loop
            # cannot spawn a concurrent scan in the gap between here and when
            # _ocr_force_scan sets the flag itself.
            self._force_scan_active[agent_id] = True
            # For agent1 scroll/arrow clicks the user wants "scroll then copy now"
            # — skip the 45s lead-time so the copy follows immediately.
            _is_scroll = any(k in element_label for k in
                             ("scroll", "arrow", "down"))
            _skip = (agent_id == "agent1" and _is_scroll)
            # Auto-save as scroll_dn calibration — user aimed at the down arrow,
            # so teach SOC where it lives for future automatic use.
            if _is_scroll:
                _cfg_nav = self.agents.get(agent_id)
                if _cfg_nav and _cfg_nav.scroll_dn_xy != (rex, rey):
                    _cfg_nav.scroll_dn_xy = (rex, rey)
                    if _cfg_nav.lbl_scroll:
                        self.root.after(0, lambda _c=_cfg_nav, _x=rex, _y=rey:
                            _c.lbl_scroll.config(
                                text=f"scroll ↓({_x},{_y}) nudge-saved",
                                fg=YELLOW))
                    self._save_config()
                    self._log(f"[cursor-nudge] scroll_dn_xy ({rex},{rey}) saved for {agent_id}")
            threading.Thread(
                target=self._ocr_force_scan,
                args=(agent_id,), kwargs={"skip_lead": _skip},
                daemon=True).start()

        # ── Log position + outcome for future healing ─────────────────────────
        try:
            import json as _json
            log_path = BASE_DIR / "nudge_log.json"
            entries = []
            if log_path.exists():
                try:
                    entries = _json.loads(log_path.read_text(encoding="utf-8"))
                except Exception:
                    entries = []
            cfg = self.agents.get(agent_id)
            entries.append({
                "ts":            datetime.datetime.now().isoformat(),
                "agent":         agent_id,
                "click_xy":      [x, y],
                "ocr_region":    list(cfg.ocr_region) if cfg and cfg.ocr_region else None,
                "outcome":       outcome,
                "identified_as": id_str,
                "next_step":     next_hint,
            })
            log_path.write_text(_json.dumps(entries[-200:], indent=2), encoding="utf-8")
        except Exception:
            pass

    def _ocr_force_scan_vscode(self):
        """OCR-based read path for agent2 (VS Code / Claude Code) — long-message fallback.
        Triggered when OCR hash is stable with trigger visible but sentinel below the fold.
        No copy-button hunt — scrolls to bottom then re-reads the OCR region directly.
        _force_scan_active["agent2"] is set by the caller before this thread starts."""
        agent_id = "agent2"
        try:
            cfg = self.agents.get(agent_id)
            if not cfg or not cfg.ocr_region:
                return

            self._last_ocr_text.pop(agent_id, None)
            self._last_strip_state.pop(agent_id, None)

            rx0, ry0, rx1, ry1 = cfg.ocr_region
            sweep_x = (rx0 + rx1) // 2

            # Focus VS Code window
            try:
                if cfg.hwnd:
                    PLATFORM.focus_window(cfg.hwnd)
                    time.sleep(0.25)
            except Exception as e:
                self._log(f"[nudge:{agent_id}] focus error: {e}")

            # Scroll to bottom — try trained scroll_dn template first
            scroll_xy = self._find_template_at(
                "agent2_scroll_dn.png", sweep_x, ry1, margin=120)
            if scroll_xy:
                self._log(f"[nudge:{agent_id}] clicking scroll_dn at {scroll_xy}")
                pyautogui.click(*scroll_xy)
                time.sleep(0.5)
            else:
                pyautogui.click(sweep_x, (ry0 + ry1) // 2)
                time.sleep(0.2)
                pyautogui.hotkey("ctrl", "end")
                time.sleep(0.5)
            self._log(f"[nudge:{agent_id}] at bottom — re-scanning OCR")

            # Re-read OCR directly — no hover, no copy button, no clipboard.
            # Claude Code has no hover-reveal copy button; the trigger text is
            # already on screen and OCR can read it.
            text = self._ocr_grab(agent_id)
            if text and text.strip():
                self._log(f"[nudge:{agent_id}] OCR re-scan: {len(text.strip())} chars — routing")
                self._ocr_process(text, source_agent=agent_id)
            else:
                self._log(f"[nudge:{agent_id}] OCR re-scan empty — aborting")
                self._agent2_copy_fail_at = time.time()
        finally:
            self._force_scan_active[agent_id] = False

    def _update_pending_indicator(self, agent_id: str, sig: tuple):
        """Update the per-agent pending dot and label.
        sig = (has_trigger, has_sentinel).  Call with (False, False) to clear."""
        cfg = self.agents.get(agent_id)
        if not cfg or cfg.lbl_pending is None:
            return
        has_trigger, has_sentinel = sig
        if has_trigger and has_sentinel:
            dot_color, txt, txt_color = YELLOW, "trigger + sentinel", YELLOW
        elif has_trigger:
            dot_color, txt, txt_color = ORANGE, "trigger visible", ORANGE
        elif has_sentinel:
            dot_color, txt, txt_color = ORANGE, "sentinel visible", ORANGE
        else:
            dot_color, txt, txt_color = "#444444", "idle", "#555555"
        def _do():
            cfg.lbl_pending_dot.config(fg=dot_color)
            cfg.lbl_pending.config(text=txt, fg=txt_color)
        self.root.after(0, _do)

    def _set_pending_routed(self, agent_id: str):
        """Flash the pending indicator green briefly after a successful route."""
        cfg = self.agents.get(agent_id)
        if not cfg or cfg.lbl_pending is None:
            return
        def _flash():
            cfg.lbl_pending_dot.config(fg=GREEN)
            cfg.lbl_pending.config(text="routed ✓", fg=GREEN)
            self.root.after(3000, lambda: self._update_pending_indicator(
                agent_id, (False, False)))
        self.root.after(0, _flash)

    def _mark_pending_stale(self, agent_id: str):
        """Mark the pending indicator grey/stale — agent is off-format or in
        conversational mode.  Stays until the next strip signal clears it."""
        cfg = self.agents.get(agent_id)
        if not cfg or cfg.lbl_pending is None:
            return
        def _do():
            cfg.lbl_pending_dot.config(fg="#888888")
            cfg.lbl_pending.config(text="stale — check agent", fg="#888888")
        self.root.after(0, _do)

    def _ocr_snapshot(self):
        """On-demand OCR dump — grabs every configured region, runs Tesseract,
        and prints raw + preprocessed text to the diagnostics log."""
        configured = [(aid, cfg) for aid, cfg in self.agents.items() if cfg.ocr_region]
        if not configured:
            self._log("[snap] no OCR regions configured — calibrate first")
            return
        self._log("[snap] ── OCR SNAPSHOT ──────────────────────────")
        for aid, cfg in configured:
            if aid == "agent3" and self._bypass_agent3:
                continue
            rx0, ry0, rx1, ry1 = cfg.ocr_region
            try:
                img = ImageGrab.grab(bbox=(rx0, ry0, rx1, ry1), all_screens=True)
            except Exception as e:
                self._log(f"[snap:{aid}] grab failed: {e}")
                continue
            raw_text = pytesseract.image_to_string(
                _prepare_img_for_ocr(img), config="--psm 6")
            processed  = _preprocess_ocr(raw_text)
            raw_h      = hashlib.md5(raw_text.encode()).hexdigest()[:8]
            cached_h   = (self._last_ocr_text.get(aid) or "")[:8]
            dedup_hit  = raw_h == cached_h
            low        = processed.lower()
            has_trigger  = bool(TRIGGER_RE.search(processed))
            has_sentinel = any(v in low for v in _SENTINEL_VARIANTS)
            self._log(
                f"[snap:{aid}] hash={raw_h} cached={cached_h} "
                f"dedup={'HIT-skip' if dedup_hit else 'MISS-process'} "
                f"trigger={has_trigger} sentinel={has_sentinel}")
            for line in processed.splitlines():
                line = line.strip()
                if line:
                    self._log(f"  {line}")
        self._log("[snap] ────────────────────────────────────────────")

    def _agent1_overflow_check(self, now=None) -> str:
        """Overflow-watchdog decision for agent1 (context in _ocr_tick).
        Returns 'retry' when the armed expectation timed out and a blind
        scroll-to-bottom copy should launch (try counted, timer re-armed),
        'exhausted' when the retry bound is hit (expectation disarmed so the
        watchdog cannot spin), or 'none'. All state mutations happen here so
        the decision is testable without the OCR loop."""
        now = time.time() if now is None else now
        if (self._agent1_expect_since <= 0
                or now - self._agent1_expect_since < AGENT1_OVERFLOW_TIMEOUT
                or now - self._agent1_copy_fail_at < AGENT1_COPY_COOL
                or self._manual_hold.get("agent1")):
            return "none"
        if self._agent1_overflow_tries < AGENT1_OVERFLOW_MAX_TRIES:
            self._agent1_overflow_tries += 1
            self._agent1_expect_since = now   # reset timer for next retry
            return "retry"
        self._agent1_expect_since = 0.0       # give up; wait for nudge / next inject
        return "exhausted"

    def _ocr_tick(self, sct):
        # Scan each agent window separately — directional routing prevents a window's
        # own injected text (SOPs, reminders) from being re-routed back into itself.
        # Agent 1's window: only routes messages addressed TO Agent 2 (and Agent 3).
        # Agent 2's window: only routes messages addressed TO Agent 1.
        # Fall back to full-screen union scan if no regions are configured.
        configured = [(aid, cfg) for aid, cfg in self.agents.items() if cfg.ocr_region]

        if not configured:
            # No windows set — grab full primary monitor and route without filter
            raw  = sct.grab(sct.monitors[0])
            img  = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
            text = pytesseract.image_to_string(_prepare_img_for_ocr(img), config="--psm 6")
            self._ocr_process(text, source_agent=None)
            return

        for aid, cfg in configured:
            if aid == "agent3" and self._bypass_agent3:
                continue   # agent3 bypassed — skip its OCR region entirely
            if aid == "agent5" and self._bypass_agent5:
                continue   # agent5 bypassed — skip GGUF chatbox OCR region entirely
            if aid == "agent2" and self._agent2_read_mode == "file":
                continue   # agent2 read via file-relay outbox — OCR path disabled
            if self._bridge_owns_window(aid):
                continue   # bridge owns agent5 replies — no OCR route/scroll churn
            if self._force_scan_active.get(aid):
                continue   # nudge scan in progress — don't collide

            # ── Overflow handler (agent1) — MUST run BEFORE the fast-strip ──────
            # The fast-strip pre-check below SKIPS the full OCR when the region is
            # quiet — which is exactly the overflow case (Copilot's newest reply
            # rendered BELOW the fixed region, so no trigger is in view here). So the
            # recovery must live here, not after the full scan. The normal capture
            # path clears _agent1_expect_since the moment a trigger appears; if it is
            # still armed AGENT1_OVERFLOW_TIMEOUT after the inject, the reply never
            # showed in-region → blind-launch the copilot copy path (it jumps to
            # bottom and copies the off-region reply). Bounded; never aimless.
            if aid == "agent1":
                _ovf = self._agent1_overflow_check()
                if _ovf == "retry":
                    self._log(
                        f"[overflow:agent1] no in-region trigger "
                        f"{AGENT1_OVERFLOW_TIMEOUT}s after inject — blind scroll-to-bottom copy "
                        f"(try {self._agent1_overflow_tries}/{AGENT1_OVERFLOW_MAX_TRIES})")
                    threading.Thread(
                        target=self._ocr_force_scan, args=(aid,), daemon=True).start()
                    continue
                elif _ovf == "exhausted":
                    self._log("[overflow:agent1] blind scans exhausted — nudge to retry")

            rx0, ry0, rx1, ry1 = cfg.ocr_region

            # ── Fast-strip pre-check ──────────────────────────────────────────
            # When idle (not accumulating, not waiting for this agent's reply,
            # not in scroll-grace), OCR the bottom 85% of the region to detect
            # sentinel/trigger before committing to the full 1.59 s scan.
            # 85% (vs earlier 40%/8%) closes the upper-middle gap: a short reply's
            # "To AgentX" trigger can land ABOVE the bottom 40% (between the old
            # top-15% and bottom-40% strips), where it was silently missed — which
            # blocked autonomous detection entirely (copy button observed at ~61%
            # down the region). The remaining top 15% is still peeked below.
            _idle = (not self._scroll_accum_active.get(aid) and
                     self._waiting_reply != aid and
                     time.time() >= self._scroll_grace.get(aid, 0))
            if _idle:
                _sh = max(120, int((ry1 - ry0) * 0.85))
                try:
                    _simg = ImageGrab.grab(
                        bbox=(rx0, ry1 - _sh, rx1, ry1), all_screens=True)
                    _stxt = pytesseract.image_to_string(
                        _prepare_img_for_ocr(_simg), config="--psm 6")
                    _slow = _stxt.lower()
                    _has_trig = bool(TRIGGER_RE.search(_stxt))
                    _has_sent = any(v in _slow for v in _SENTINEL_VARIANTS)
                    if not _has_trig and not _has_sent:
                        # Bottom strip quiet — also peek at the top strip.
                        # Handles the trigger-only state: message is complete but long,
                        # so "To AgentX" is at the top of the visible window while
                        # "end message now" is still below the fold.
                        _top_h = max(120, int((ry1 - ry0) * 0.15))
                        try:
                            _timg = ImageGrab.grab(
                                bbox=(rx0, ry0, rx1, ry0 + _top_h), all_screens=True)
                            _ttxt = pytesseract.image_to_string(
                                _prepare_img_for_ocr(_timg), config="--psm 6")
                            _has_trig = bool(TRIGGER_RE.search(_ttxt))
                        except Exception:
                            pass
                        if not _has_trig:
                            continue   # both strips quiet — skip full OCR
                    _sig = (_has_trig, _has_sent)
                    if _sig == self._last_strip_state.get(aid):
                        # Same signal state — normally skip. But if trigger is
                        # visible and sentinel hasn't appeared, and the frame has
                        # been stable (generation done), scroll one notch toward
                        # the sentinel at most once every 0.5s.
                        if (_has_trig and not _has_sent
                                and self._waiting_reply == aid
                                and aid != "agent1"):  # agent1 uses copy-button path
                            _stable = time.time() - self._region_last_change.get(aid, 0)
                            _last_s = self._sentinel_scroll_at.get(aid, 0)
                            if _stable >= 2.5 and time.time() - _last_s >= 0.5:
                                _cfg2 = self.agents.get(aid)
                                if _cfg2 and _cfg2.ocr_region:
                                    _sx0, _sy0, _sx1, _sy1 = _cfg2.ocr_region
                                    _scx = (_sx0 + _sx1) // 2
                                    _scy = (_sy0 + _sy1) // 2
                                    try:
                                        with self._inject_lock:
                                            pyautogui.moveTo(_scx, _scy)
                                            pyautogui.scroll(-1)
                                    except Exception:
                                        pass
                                    self._sentinel_scroll_at[aid] = time.time()
                                    self._last_strip_state.pop(aid, None)
                                    self._log(
                                        f"[scroll-seek:{aid}] +1 notch toward sentinel "
                                        f"(stable {_stable:.1f}s)")
                        continue   # same signal state — stale, skip full OCR
                    self._last_strip_state[aid] = _sig
                    self._update_pending_indicator(aid, _sig)
                    self._log(f"[strip:{aid}] signal — full scan")
                except Exception as _se:
                    self._log(f"[strip:{aid}] err ({_se}) — full scan")
            # ── End fast-strip pre-check ──────────────────────────────────────

            # Use ImageGrab (GDI/BitBlt) instead of mss for per-window captures.
            # mss uses DXGI which cannot capture GPU-accelerated windows like VS Code.
            # ImageGrab works with all windows regardless of renderer.
            try:
                img = ImageGrab.grab(bbox=(rx0, ry0, rx1, ry1), all_screens=True)
            except Exception:
                # Fallback to mss if ImageGrab fails
                grab_box = {"left": rx0, "top": ry0,
                            "width": rx1 - rx0, "height": ry1 - ry0}
                raw = sct.grab(grab_box)
                img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
            img = self._apply_blindzone(img, rx0, ry0)

            # Heartbeat: compare 32×32 thumbnail pixel-hash to previous frame.
            # Any pixel change (new text, cursor blink, scroll) counts as activity.
            thumb_h = hashlib.md5(
                img.resize((32, 32), Image.NEAREST).tobytes()).hexdigest()
            if self._region_frame.get(aid) != thumb_h:
                self._region_frame[aid]       = thumb_h
                self._region_last_change[aid] = time.time()

            text = pytesseract.image_to_string(_prepare_img_for_ocr(img), config="--psm 6")

            # Text-hash dedup: if OCR text unchanged since last tick, skip processing.
            # This stops stale relay messages (still visible in chat scroll) from being
            # re-routed every scan cycle. Body-match dedup is the second-layer backstop.
            text_h = hashlib.md5(text.encode()).hexdigest()

            # Hash-stability tracker for agent1: runs BEFORE dedup so it fires even
            # when content has stopped changing (dedup would otherwise skip the tick).
            # While agent1 is typing its reply the hash changes every tick; once
            # generation is complete the hash freezes.  After 8 frozen seconds we
            # treat the response as done and launch force_scan via the clipboard path.
            if (aid == "agent1"
                    and self._waiting_reply == "agent1"
                    and bool(TRIGGER_RE.search(text))):
                if text_h != self._agent1_last_hash:
                    self._agent1_last_hash = text_h
                    self._agent1_hash_stable_since = time.time()
                else:
                    stable_secs = time.time() - self._agent1_hash_stable_since
                    if (stable_secs >= 8.0
                            and not self._force_scan_active.get(aid)
                            and time.time() - self._agent1_copy_fail_at >= AGENT1_COPY_COOL
                            and not self._manual_hold.get(aid)):   # respect Hold A1
                        self._log(
                            f"[ocr:{aid}] hash stable {stable_secs:.0f}s "
                            f"— generation complete, launching copy")
                        self._agent1_last_hash = ""       # reset for next cycle
                        self._agent1_hash_stable_since = time.time()
                        threading.Thread(
                            target=self._ocr_force_scan, args=(aid,), daemon=True).start()
                        continue

            # Hash-stability tracker for agent2: same principle as agent1 above.
            # When agent2 is generating a long response the OCR hash changes each tick;
            # once generation is done the hash freezes.  After 8 frozen seconds with the
            # trigger visible but sentinel NOT visible (message too long for the window)
            # we launch _ocr_force_scan_vscode() — the clipboard-based fallback.
            if (aid == "agent2"
                    and self._waiting_reply == "agent2"
                    and bool(TRIGGER_RE.search(text))
                    and not any(v in text.lower() for v in _SENTINEL_VARIANTS)):
                if text_h != self._agent2_last_hash:
                    self._agent2_last_hash = text_h
                    self._agent2_hash_stable_since = time.time()
                else:
                    stable_secs = time.time() - self._agent2_hash_stable_since
                    if (stable_secs >= 8.0
                            and not self._force_scan_active.get(aid)
                            and time.time() - self._agent2_copy_fail_at >= 15.0):
                        self._log(
                            f"[ocr:{aid}] hash stable {stable_secs:.0f}s "
                            f"— long message, launching vscode clipboard scan")
                        self._agent2_last_hash = ""
                        self._agent2_hash_stable_since = time.time()
                        self._force_scan_active[aid] = True
                        threading.Thread(
                            target=self._ocr_force_scan_vscode, daemon=True).start()
                        continue

            if text_h == self._last_ocr_text.get(aid):
                continue
            self._last_ocr_text[aid] = text_h

            # Debug: log every content change so test harness can verify what SOC reads
            low_snap  = text.lower()
            _has_trig = bool(TRIGGER_RE.search(text))
            _has_sent = any(v in low_snap for v in _SENTINEL_VARIANTS)
            self._log(
                f"[tick:{aid}] hash={text_h[:8]} chars={len(text.strip())} "
                f"trigger={'YES' if _has_trig else 'no'} "
                f"sentinel={'YES' if _has_sent else 'no'}")

            # Inject grace: suppress routing for a window after SOC sends the SOP to an
            # agent — prevents the SOP example relay lines from firing as live messages.
            if time.time() < self._inject_grace.get(aid, 0):
                continue

            low  = text.lower()
            has_trigger  = bool(TRIGGER_RE.search(text))
            has_sentinel = any(v in low for v in _SENTINEL_VARIANTS)
            if has_trigger:
                self._log(
                    f"[ocr:{aid}] trigger=YES sentinel={'YES' if has_sentinel else 'no'} "
                    f"hold={self._waiting_reply or 'none'}")
            # Agent1 always uses the clipboard path — never OCR fragments or scroll
            # accumulation.
            #   _waiting_reply == None     → fresh outbound block; launch immediately
            #   _waiting_reply == "agent1" → we sent to agent1 and are awaiting its reply.
            #                               If trigger+sentinel both visible: launch now
            #                               (short ack message is complete).
            #                               If trigger-only: wait 30s from _waiting_since
            #                               before launching (long message still generating).
            #   _waiting_reply == other    → already upstream; block entirely
            if has_trigger and aid == "agent1":
                self._agent1_expect_since = 0.0   # trigger visible — normal capture path handles it
                already_upstream = (self._waiting_reply and
                                    self._waiting_reply != "agent1")
                # Trigger-only while waiting for agent1's reply: block here.
                # The hash-stability tracker (above, before dedup) fires force_scan
                # once the hash freezes, signalling generation is complete.
                # Trigger+sentinel both visible: launch immediately (short message).
                still_waiting = (self._waiting_reply == "agent1" and not has_sentinel)
                copy_cooling  = (time.time() - self._agent1_copy_fail_at < AGENT1_COPY_COOL)
                if (not self._force_scan_active.get(aid)
                        and not already_upstream
                        and not still_waiting
                        and not copy_cooling
                        and not self._manual_hold.get(aid)):   # respect Hold A1
                    # Sentinel already on screen = message COMPLETE ('end message
                    # now' written), so skip the 45s populating lead-time and go
                    # straight to scroll-down + copy. Only trigger-only (still
                    # streaming) keeps the lead.
                    threading.Thread(
                        target=self._ocr_force_scan, args=(aid,),
                        kwargs={"skip_lead": has_sentinel}, daemon=True).start()
                continue
            if has_trigger and not has_sentinel:
                # Keep rapid mode alive while accumulating
                self._rapid_until = time.time() + RAPID_DURATION
                # Enter or continue scroll-accumulation mode: stitch OCR frames
                # top-to-bottom while scrolling until the sentinel appears.
                if not self._scroll_accum_active.get(aid):
                    self._scroll_accum_active[aid] = True
                    self._scroll_accum_since[aid]  = time.time()
                    self._scroll_accum[aid]        = text
                    self._log(f"[accum:{aid}] started — accumulating frames")
                else:
                    elapsed = time.time() - self._scroll_accum_since.get(aid, time.time())
                    if elapsed > SCROLL_ACCUM_TIMEOUT:
                        self._log(f"[accum:{aid}] timeout ({elapsed:.0f}s) — clearing")
                        self._scroll_accum_active[aid] = False
                        self._scroll_accum[aid] = ""
                    else:
                        self._scroll_accum[aid] = self._merge_scroll_text(
                            self._scroll_accum[aid], text)
                # Scroll down so next rapid tick can see more of the message
                now = time.time()
                if now - self._last_scroll.get(aid, 0) >= SCROLL_ACCUM_MIN_INTERVAL:
                    self._last_scroll[aid] = now
                    threading.Thread(
                        target=self._scroll_agent_down,
                        args=(aid,), daemon=True).start()
            elif has_sentinel and self._scroll_accum_active.get(aid):
                # Sentinel now visible — merge current frame into accumulated buffer
                # and route the complete message.
                merged = self._merge_scroll_text(self._scroll_accum[aid], text)
                # If the trigger header isn't at the start of accumulated content,
                # the window was mid-message when accumulation began. Scroll up to
                # capture the beginning, then prepend whatever extra text is revealed.
                if not TRIGGER_RE.search(merged.split("\n")[0] if merged else ""):
                    self._log(
                        f"[accum:{aid}] trigger not at start — back-scrolling to recover header")
                    for _ in range(6):
                        self._scroll_agent_up(aid, n=1)
                        time.sleep(0.25)
                        top_frame = self._ocr_grab(aid)
                        if top_frame.strip():
                            merged = self._merge_scroll_text(top_frame, merged)
                        if TRIGGER_RE.search(top_frame.split("\n")[0] if top_frame else ""):
                            self._log(f"[accum:{aid}] header recovered after back-scroll")
                            break
                self._log(
                    f"[accum:{aid}] sentinel found — routing "
                    f"{len(merged)} accumulated chars")
                n = self._route_text(merged, source_agent=aid)
                if n == 0:
                    # Fallback: route current frame alone (may have inline trigger)
                    self._route_text(text, source_agent=aid)
                self._scroll_accum_active[aid] = False
                self._scroll_accum[aid] = ""
            elif self._scroll_accum_active.get(aid):
                # Mid-scroll: neither trigger nor sentinel visible — just the body.
                # Keep merging and scrolling until sentinel appears.
                # Extend rapid mode so the scan rate stays fast throughout.
                self._rapid_until = time.time() + RAPID_DURATION
                elapsed = time.time() - self._scroll_accum_since.get(aid, time.time())
                if elapsed > SCROLL_ACCUM_TIMEOUT:
                    self._log(f"[accum:{aid}] timeout ({elapsed:.0f}s) — clearing")
                    self._scroll_accum_active[aid] = False
                    self._scroll_accum[aid] = ""
                else:
                    self._scroll_accum[aid] = self._merge_scroll_text(
                        self._scroll_accum[aid], text)
                now = time.time()
                if now - self._last_scroll.get(aid, 0) >= SCROLL_ACCUM_MIN_INTERVAL:
                    self._last_scroll[aid] = now
                    threading.Thread(
                        target=self._scroll_agent_down,
                        args=(aid,), daemon=True).start()
            elif has_sentinel and not has_trigger and not self._force_scan_active.get(aid):
                # Sentinel visible but trigger has scrolled above the fold.
                # Agent1 exception: if we're already holding for a reply, don't hunt —
                # the sentinel is from the previous message and will route on the next cycle.
                if aid == "agent1" and self._waiting_reply:
                    pass  # stale sentinel from already-routed message; wait for new trigger
                elif self._manual_hold.get(aid):
                    pass  # Hold active — don't auto-hunt; wait for user nudge
                elif self._auto_hunt_suppressed(aid):
                    pass  # a recent hunt failed — back off (avoid infinite scroll-churn)
                else:
                    self._log(f"[ocr:{aid}] sentinel-only — auto-hunt: scrolling up for trigger")
                    threading.Thread(
                        target=self._ocr_force_scan, args=(aid,), daemon=True).start()
            else:
                self._ocr_process(text, source_agent=aid)

            # Auto-scroll: if we're waiting for THIS agent to reply, scroll its
            # window down so the tail of a long response stays in the OCR region.
            if self._waiting_reply == aid or time.time() < self._scroll_grace.get(aid, 0):
                now = time.time()
                if now - self._last_scroll.get(aid, 0) >= HOLD_SCROLL_INTERVAL:
                    self._last_scroll[aid] = now
                    threading.Thread(
                        target=self._scroll_agent_down,
                        args=(aid,), daemon=True).start()

    def _detect_rate_limit(self, text: str, source_agent: str | None):
        """Check if OCR contains a rate-limit message. If so, parse reset time
        and set dynamic hold timeout until quota replenishes."""
        if not source_agent:
            return
        m = RATE_LIMIT_RE.search(text)
        if not m:
            return
        hour, minute, ampm, tz = m.groups()
        hour = int(hour)
        minute = int(minute)
        if ampm.lower() == 'pm' and hour != 12:
            hour += 12
        elif ampm.lower() == 'am' and hour == 12:
            hour = 0
        now = datetime.datetime.now()
        reset_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if reset_time <= now:
            reset_time += datetime.timedelta(days=1)
        reset_epoch = reset_time.timestamp()
        self._rate_limited[source_agent] = reset_epoch
        delay_sec = int(reset_epoch - time.time())
        tz_str = f" ({tz})" if tz else ""
        self._log(f"[rate-limit] {source_agent} quota exhausted — will retry at {reset_time.strftime('%I:%M%p')}{tz_str} "
                  f"({delay_sec}s delay)")
        if source_agent not in ("agent3",):
            self._log(f"[rate-limit] ⚠ rate limit for {source_agent} not yet handled; "
                      f"recommend extending hold until quota resets")

    def _ocr_process(self, text: str, source_agent: str | None):
        """Process OCR text from one window. source_agent filters routing direction:
        messages addressed to source_agent are skipped (a window cannot self-route)."""
        text = _preprocess_ocr(text)   # normalise multi-char garbles before regex
        low = text.lower()

        # Evict stale pending trigger before doing anything else
        if source_agent:
            pt = self._pending_trigger.get(source_agent)
            if pt and time.time() > pt[1]:
                self._pending_trigger[source_agent] = None
                self._log(f"[trigger] {source_agent} pending trigger expired (30s)")

        # Detect rate-limit messages (e.g., Claude.ai quota exhausted)
        self._detect_rate_limit(text, source_agent)

        # Step 1: "to agent" spotted → enter rapid mode + record pending trigger
        if TRIGGER_RE.search(text):
            self._rapid_until = time.time() + RAPID_DURATION
            if source_agent:
                digit_m = re.search(rf"to\s+agent\s*({_D})", text, re.IGNORECASE)
                if digit_m:
                    digit = _OCR_DIGIT_NORM.get(digit_m.group(1), digit_m.group(1))
                    if digit in ("1", "2", "3"):
                        self._pending_trigger[source_agent] = (
                            f"agent{digit}", time.time() + TRIGGER_PERSIST_SECS)

        # Attendance check: look for SOC-ACK-N in the source agent's window.
        # Only register if the ACK digit matches the window we're reading, so a
        # stray reflection in another window can't false-confirm a different agent.
        if source_agent:
            for m in ROLL_CALL_RE.finditer(text):
                digit    = _OCR_DIGIT_NORM.get(m.group(1), m.group(1))
                ack_aid  = f"agent{digit}"
                if ack_aid == source_agent and not self._attendance.get(ack_aid):
                    self._mark_attendance(ack_aid)

        # Step 2: full sentinel present → extract and route
        has_sentinel = any(v in low for v in _SENTINEL_VARIANTS)
        if has_sentinel and TRIGGER_RE.search(text):
            # Normal path: trigger + sentinel both visible in this frame
            self._route_text(text, source_agent=source_agent)
        elif has_sentinel and source_agent:
            # Sentinel visible but trigger scrolled off top — check pending trigger
            pt = self._pending_trigger.get(source_agent)
            if pt and time.time() < pt[1]:
                dest_agent, _ = pt
                self._log(
                    f"[trigger] sentinel only — using remembered "
                    f"{source_agent}→{dest_agent}")
                self._route_with_remembered_trigger(text, source_agent, dest_agent)
            else:
                # No pending trigger — try routing anyway (INLINE_RE fallback may match)
                self._route_text(text, source_agent=source_agent)

        # Mode triggers are now checked inside _try_route only — never on raw OCR text —
        # so SOP content displayed on screen cannot false-fire mode changes.

        # Step 3: [CMD: ...] hook for Bing disconnected-hand (disabled)
        self._parse_cmd_blocks(text)

    def _route_with_remembered_trigger(
            self, ocr_text: str, source_agent: str, dest_agent: str):
        """Route a message when the trigger was seen in a prior tick but has since
        scrolled off the top of the OCR region. Prepends the remembered routing
        header so SENTINEL_RE can parse it, then routes through the normal pipeline."""
        digit = dest_agent[-1]
        # Prepend remembered header; body is the current OCR frame up to sentinel
        synthetic = f"To Agent{digit}\n{ocr_text}\nend message now"
        n = self._route_text(synthetic, source_agent=source_agent)
        if n > 0:
            self._pending_trigger[source_agent] = None
            # Suppress source window for 8s — prevents the normal path from
            # double-routing the same message when the full frame renders next tick
            # (remembered-trigger body hash differs from cleanly-extracted body hash)
            self._inject_grace[source_agent] = max(
                self._inject_grace.get(source_agent, 0),
                time.time() + 8)
            self._log(
                f"[trigger] ✓ remembered trigger routed "
                f"({source_agent}→{dest_agent}) — 8s source grace set")
        else:
            self._log(
                f"[trigger] sentinel present but _route_text matched 0 "
                f"— body may be deduped or malformed")

    # ── Disconnected-hand CMD parser (Bing → OCR → local action) ─────────────
    # Set CMD_ENABLED = True to allow Bing chat to write files via OCR commands.
    # Bing types:  [CMD: write_file outbox/agent1/msg.md Hello agent1]
    # OCR sees it → executes whitelisted action locally.
    CMD_ENABLED  = False
    CMD_RE       = re.compile(r"\[CMD:\s*(\w+)\s+(.+?)\]", re.DOTALL)
    CMD_WHITELIST = {"write_file"}

    def _parse_cmd_blocks(self, text: str):
        if not self.CMD_ENABLED:
            return
        for m in self.CMD_RE.finditer(text):
            cmd, args = m.group(1).strip(), m.group(2).strip()
            if not self._dedup(m.group(0)):
                continue
            if cmd not in self.CMD_WHITELIST:
                self._log(f"[cmd] blocked (not whitelisted): {cmd}")
                continue
            if cmd == "write_file":
                parts = args.split(None, 1)
                if len(parts) == 2:
                    rel_path, content = parts
                    target = (BASE_DIR / rel_path).resolve()
                    # Security: must stay inside BASE_DIR
                    try:
                        target.relative_to(BASE_DIR)
                    except ValueError:
                        self._log(f"[cmd] path escape blocked: {rel_path}")
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content.strip("\"'"), encoding="utf-8")
                    self._log(f"[cmd] write_file → {target.name}")

    # ── File outbox watcher ───────────────────────────────────────────────────

    def _toggle_bing_mode(self):
        """Toggle Agent 1 (Bing/Edge) workflow: Edge prefix cadence + outbound noise stripping."""
        if self._bing_mode:
            self._bing_mode = False
            self.bing_btn.config(text="🔵 Bing", bg=BG2, fg=ACCENT)
            self._log("[Bing mode] OFF")
            self._set_status("Bing mode OFF")
        else:
            self._bing_mode = True
            # Reset Agent 1's message counter so cadence starts fresh
            cfg1 = self.agents.get("agent1")
            if cfg1:
                cfg1.msg_count = 0
            self.bing_btn.config(text="■ Bing", bg=ACCENT, fg="#1e1e1e")
            self._log(
                "[Bing mode] ON\n"
                "  • Messages 1-4: Edge prefix prepended to Agent 1 injections\n"
                f"  • Message 5 (every {REMINDER_EVERY}): full role recalibration\n"
                "  • Agent 1 outbound: Edge prefix stripped before routing")
            self._set_status("Bing mode ON — Agent 1 Edge cadence active")

    def _toggle_vscode_mode(self):
        """One-click mode: starts Outbox watcher + Auto-click scan together.
        Designed for Copilot ↔ Claude Code workflows where agents communicate
        by writing .md files to outbox/agent1/ or outbox/agent2/."""
        if self._vscode_mode:
            # ── Deactivate ─────────────────────────────────────────────────────────────
            self._vscode_mode = False
            if self._fw_running:
                self._fw_running = False
                self.fw_btn.config(text="▶ Outbox", bg=BG2, fg=ACCENT)
            if self._autoclick_running:
                self._autoclick_running = False
                self._ac_scan_btn.config(text="▶ Scan", fg=GREEN)
            self.vscode_btn.config(text="⚡ VS Code", bg=BG2, fg=GREEN)
            self._log("[VS Code mode] OFF — outbox + auto-click stopped")
            self._set_status("VS Code mode OFF")
        else:
            # ── Activate ─────────────────────────────────────────────────────────────
            if not _CV2_OK:
                self._set_status("opencv required for auto-click — pip install opencv-python")
                return
            self._vscode_mode = True
            # Start outbox watcher
            if not self._fw_running:
                self._fw_running = True
                self.fw_btn.config(text="■ Outbox", bg=RED, fg="white")
                self._fw_thread = threading.Thread(
                    target=self._fw_loop, daemon=True)
                self._fw_thread.start()
            # Start auto-click scan
            if not self._autoclick_running:
                self._autoclick_running = True
                self._ac_scan_btn.config(text="■ Scanning", fg=RED)
                self._autoclick_thread = threading.Thread(
                    target=self._autoclick_loop, daemon=True)
                self._autoclick_thread.start()
            # Send initial workflow briefing to Agent 3 on activation.
            # If A3 has an independent workspace configured (post-Anthropic
            # update), prepend a directive so it operates in the project folder.
            briefing = GROUND_RULES_VSCODE_AGENT3
            a3ws = self._agent3_workspace_var.get().strip()
            if a3ws:
                briefing = (
                    f"[SOC] Your project workspace for this session:\n"
                    f"  {a3ws}\n"
                    f"Open / cd to this folder before any file work — Anthropic's "
                    f"default workspace is separate from the project.\n\n"
                    + briefing
                )
            self._write_outbox("agent3", briefing, "briefing")
            self.vscode_btn.config(text="■ VS Code", bg=GREEN, fg="#1e1e1e")
            self._log(
                "[VS Code mode] ON\n"
                "  • Outbox watching: outbox/agent1/  outbox/agent2/\n"
                "  • Auto-click scan: active\n"
                f"  • Agent 1 briefing sent — brief reminder every {REMINDER_EVERY} messages\n"
                "  Drop .md files into outbox/ from Copilot or Claude Code —\n"
                "  SOC will inject them and click approval buttons automatically.")
            self._set_status("VS Code mode ON — outbox + auto-click active")

    # ── SOC bridge: exact local-agent reply channel ──────────────────────────
    def _bridge_write_marker(self):
        """Signal SOC's presence so the chatbox pushes exact replies here. Kept
        fresh while OCR runs; when SOC stops/exits the marker goes stale and the
        chatbox feature returns to dormant on its own."""
        try:
            SOC_BRIDGE_DIR.mkdir(parents=True, exist_ok=True)
            SOC_BRIDGE_MARKER.write_text(
                json.dumps({"pid": os.getpid(), "ts": time.time()}),
                encoding="utf-8")
            self._bridge_marker_at = time.time()
        except Exception as e:
            self._log(f"[bridge] marker write error: {e}")

    def _bridge_clear_marker(self):
        """Remove the presence marker so the chatbox goes dormant immediately
        (best-effort; a stale marker would also expire on its own via TTL)."""
        try:
            if SOC_BRIDGE_MARKER.exists():
                SOC_BRIDGE_MARKER.unlink()
        except Exception:
            pass

    def _bridge_active(self) -> bool:
        """True while a recent exact reply proves the chatbox is feeding this
        channel — used to suppress the lossy OCR route for the shared window.
        Stays False (OCR keeps working) against an older chatbox that never
        writes bridge files, so this is a safe, self-arming upgrade."""
        return (time.time() - self._bridge_last_seen) < BRIDGE_TRUST_WINDOW

    def _bridge_owns_window(self, aid: str) -> bool:
        """True when the exact-reply bridge is live for the shared local window
        (agent5). While it is, OCR must stay OFF that window entirely — not just
        routing, but the strip-seek + sentinel auto-hunt SCROLLING too. Left on,
        the auto-hunt scrolls the window, which changes its content, which
        triggers another scan → another hunt → endless scroll-thrash on the very
        window SOC needs to inject the next hop (observed live: relay stalled at
        the A6 turn with the GPU pegged). Only agent5; only once the bridge has
        proven live, so an older chatbox still falls back to OCR."""
        return aid == "agent5" and self._bridge_active()

    def _bridge_loop(self):
        """Started once; self-gates on OCR state. While SOC is orchestrating it
        refreshes the presence marker and drains the chatbox's reply drop-folder,
        routing each completed local-agent reply VERBATIM (no OCR)."""
        self._log(f"[bridge] watcher armed → {SOC_BRIDGE_REPLIES}")
        while True:
            try:
                if self._ocr_running and not self._estop:
                    now = time.time()
                    if now - self._bridge_marker_at >= BRIDGE_MARKER_REFRESH:
                        self._bridge_write_marker()
                    if not self._paused:
                        self._bridge_scan_once()
            except Exception as e:
                self._log(f"[bridge] loop error: {e}")
            time.sleep(BRIDGE_POLL)

    def _bridge_scan_once(self):
        """One poll: route any settled reply files, oldest first, then archive."""
        try:
            files = sorted(SOC_BRIDGE_REPLIES.glob("*.md"))
        except Exception:
            return
        for f in files:
            try:
                size = f.stat().st_size
            except OSError:
                continue
            # Write-complete stability gate: only read once the size settles, so
            # we never route a half-written file.
            prev = self._bridge_seen.get(f.name)
            self._bridge_seen[f.name] = size
            if prev != size:
                continue
            self._bridge_seen.pop(f.name, None)
            try:
                text = f.read_text(encoding="utf-8", errors="replace").strip()
            except Exception as e:
                self._log(f"[bridge] read error {f.name}: {e}")
                continue
            if text:
                self._bridge_route(text)
            self._bridge_archive(f)

    def _bridge_route(self, text: str):
        """Route one exact reply as if it came from the shared local window —
        source 'agent5' so the hold/directional/CD-swap logic is identical to the
        OCR path, but from_bridge=True marks it authoritative (the OCR duplicate
        is dropped while the bridge is live)."""
        self._bridge_last_seen = time.time()
        self._log(f"[bridge] ← exact reply ({len(text)} chars) — routing verbatim (no OCR)")
        try:
            self._route_text(text, source_agent="agent5", from_bridge=True)
        except Exception as e:
            self._log(f"[bridge] route error: {e}")

    def _bridge_archive(self, path: Path):
        """Move a processed reply into processed/ (delete if the move fails)."""
        try:
            SOC_BRIDGE_PROCESSED.mkdir(parents=True, exist_ok=True)
            path.replace(SOC_BRIDGE_PROCESSED / path.name)
        except Exception:
            try:
                path.unlink()
            except Exception:
                pass

    def _write_outbox(self, agent_id: str, content: str, prefix: str = "soc"):
        """Write content as a .md file to outbox/agent_id/ for the
        file watcher to detect and inject into the agent's chat window."""
        ts   = datetime.datetime.now().strftime("%H%M%S%f")
        path = OUTBOX_DIR / agent_id / f"{ts}_{prefix}.md"
        try:
            path.write_text(content, encoding="utf-8")
        except Exception as e:
            self._log(f"[outbox] write error ({agent_id}/{prefix}): {e}")

    def _write_transcript(self, source, dest: str, body: str,
                          kind: str = "msg") -> None:
        """Append one inter-agent message to a durable, human-readable transcript
        at transcript/conversation_<date>.md — the single source of truth for
        "what the agents said". OCR capture + clipboard are ephemeral, outbox
        files get archived to sent/, and the debug log truncates bodies. A
        standalone monitor tails this file. Never raises into the routing path.
        Internally deduped so a message that lingers on-screen across OCR ticks
        is logged once, not every tick."""
        body = (body or "").strip()
        if not body:
            return
        try:
            src   = source or "operator"
            h     = hashlib.md5(f"{src}|{dest}|{body}".encode("utf-8")).hexdigest()
            now   = datetime.datetime.now()
            path  = TRANSCRIPT_DIR / f"conversation_{now.strftime('%Y-%m-%d')}.md"
            block = (f"\n### {now.strftime('%H:%M:%S')}  {src} → {dest}  [{kind}]\n"
                     f"{body}\n")
            with self._transcript_lock:
                if h in self._transcript_seen:
                    return
                self._transcript_seen[h] = None
                while len(self._transcript_seen) > 400:
                    self._transcript_seen.popitem(last=False)
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(block)
        except Exception as e:
            self._log(f"[transcript] write error: {e}")

    def _toggle_file_watcher(self):
        if self._fw_running:
            self._fw_running = False
            self.fw_btn.config(text="▶ Outbox", bg=BG2, fg=ACCENT)
            self._log("[outbox] stopped")
        else:
            self._fw_running = True
            self.fw_btn.config(text="■ Outbox", bg=RED, fg="white")
            self._fw_thread = threading.Thread(
                target=self._fw_loop, daemon=True)
            self._fw_thread.start()
            self._log(f"[outbox] watching  outbox/agent1/  and  outbox/agent2/")
            self._log("         drop *.md file → injects content → clicks Send")

    def _browse_agent3_outbox(self):
        """Open a folder-picker and update the A3 Outbox path."""
        import tkinter.filedialog as fd
        folder = fd.askdirectory(title="Select agent3_outbox folder")
        if folder:
            self._agent3_outbox_var.set(folder)
            self._on_outbox_path_change()

    def _browse_agent3_workspace(self):
        """Open a folder-picker and update the Agent 3 independent workspace path.
        This path is injected to Agent 3 at session start so it operates in the
        same workspace as the project, despite Anthropic giving A3 its own default."""
        import tkinter.filedialog as fd
        folder = fd.askdirectory(title="Select Agent 3 workspace folder")
        if folder:
            self._agent3_workspace_var.set(folder)
            self._save_config()
            self._log(f"[a3-workspace] set: {folder}")

    def _on_outbox_path_change(self):
        """Validate and persist the agent3 outbox path; create processed/ subfolder."""
        raw = self._agent3_outbox_var.get().strip()
        if raw:
            p = Path(raw)
            try:
                p.mkdir(parents=True, exist_ok=True)
                (p / "processed").mkdir(exist_ok=True)
                self._log(f"[a3-outbox] watching: {p}")
            except Exception as e:
                self._log(f"[a3-outbox] path error: {e}")
        self._save_config()

    def _fw_loop(self):
        while self._fw_running:
            if getattr(self, "_estop", False):          # E-STOP: outbox held
                time.sleep(0.3)
                continue
            # ── Internal outbox: SOC-written files → inject into agents ──────
            for agent_id in ("agent1", "agent2", "agent3"):
                inbox = OUTBOX_DIR / agent_id
                try:
                    files = sorted(inbox.glob("*.md"))
                except OSError as e:
                    self._log(f"[outbox] directory error ({agent_id}): {e}")
                    continue
                for f in files:
                    try:
                        content = f.read_text(encoding="utf-8").strip()
                        if content:
                            self._log(f"[outbox] {f.name} → {agent_id}")
                            self._write_transcript("operator", agent_id, content,
                                                   kind="outbox")
                            self._inject_to_agent(agent_id, content)
                        ts   = datetime.datetime.now().strftime("%H%M%S%f")
                        dest = SENT_DIR / agent_id / f"{ts}_{f.name}"
                        shutil.move(str(f), str(dest))
                    except Exception as e:
                        self._log(f"[outbox] {f.name} error: {e}")

            # ── External agent3 outbox: Agent3-written files → route to agents
            # File naming convention: [name]_to_agent1.md  or  [name]_to_agent2.md
            # Stability gate: only route when file size unchanged across two polls.
            raw_outbox = self._agent3_outbox_var.get().strip()
            if raw_outbox:
                ext_outbox = Path(raw_outbox)
                try:
                    new_files = sorted(ext_outbox.glob("*.md")) + sorted(ext_outbox.glob("*.txt"))
                except OSError:
                    new_files = []
                for f in new_files:
                    try:
                        size_now = f.stat().st_size
                        size_prev = self._agent3_outbox_seen.get(f.name)
                        if size_prev is None:
                            # First sighting — record size and wait for next poll
                            self._agent3_outbox_seen[f.name] = size_now
                            continue
                        if size_now != size_prev:
                            # Still changing — update and wait
                            self._agent3_outbox_seen[f.name] = size_now
                            continue
                        # Size stable across two polls — safe to read
                        self._agent3_outbox_seen.pop(f.name, None)

                        # Parse target agent from filename: *_to_agent1.* or *_to_agent2.*
                        import re as _re
                        m = _re.search(r"_to_(agent[123])\.", f.name, _re.IGNORECASE)
                        target_agent = m.group(1).lower() if m else None
                        if not target_agent:
                            self._log(f"[a3-outbox] {f.name} — no _to_agentN in name, skipping")
                            # Archive anyway so it doesn't loop
                            proc = ext_outbox / "processed"
                            proc.mkdir(exist_ok=True)
                            shutil.move(str(f), str(proc / f.name))
                            continue

                        content = f.read_text(encoding="utf-8").strip()
                        if content:
                            self._log(f"[a3-outbox] {f.name} → {target_agent} "
                                      f"({len(content)} chars)")
                            self._inject_to_agent(target_agent, content)
                        else:
                            self._log(f"[a3-outbox] {f.name} is empty — skipping")

                        proc = ext_outbox / "processed"
                        proc.mkdir(exist_ok=True)
                        shutil.move(str(f), str(proc / f.name))
                    except Exception as e:
                        self._log(f"[a3-outbox] {f.name} error: {e}")

            # ── Agent 2 file-relay outbox ────────────────────────────────────
            # When agent2 read mode is "file", Agent 2 writes its reply as a
            # *.md / *.txt file here. Each file is size-stabilised across two
            # polls (avoids reading a half-written file), then its body is fed
            # through _route_text(source_agent="agent2") — the SAME routing,
            # hold, dedup, and mode-transition path OCR uses, but with clean text.
            if self._agent2_read_mode == "file":
                try:
                    a2_files = (sorted(AGENT2_OUTBOX_DIR.glob("*.md")) +
                                sorted(AGENT2_OUTBOX_DIR.glob("*.txt")))
                except OSError:
                    a2_files = []
                for f in a2_files:
                    try:
                        size_now  = f.stat().st_size
                        size_prev = self._agent2_outbox_seen.get(f.name)
                        if size_prev is None or size_now != size_prev:
                            # First sighting or still growing — record and wait.
                            self._agent2_outbox_seen[f.name] = size_now
                            continue
                        # Size stable across two polls — safe to read.
                        self._agent2_outbox_seen.pop(f.name, None)
                        # Skip self-documentation / non-message files so their
                        # example envelopes are never injected as real traffic.
                        if (f.name.lower() in ("readme.md", "readme.txt")
                                or f.name.startswith(".")):
                            continue
                        content = f.read_text(encoding="utf-8").strip()
                        proc = AGENT2_OUTBOX_DIR / "processed"
                        proc.mkdir(exist_ok=True)
                        if content:
                            n = self._route_text(content, source_agent="agent2")
                            self._log(f"[a2-file] {f.name} → routed {n} message(s) "
                                      f"({len(content)} chars)")
                        else:
                            self._log(f"[a2-file] {f.name} empty — skipping")
                        ts = datetime.datetime.now().strftime("%H%M%S%f")
                        shutil.move(str(f), str(proc / f"{ts}_{f.name}"))
                    except Exception as e:
                        self._log(f"[a2-file] {f.name} error: {e}")

            time.sleep(OUTBOX_POLL)

    # ── Plugin loader ─────────────────────────────────────────────────────────

    def _vplugin_file_present(self) -> bool:
        """True if v_plugin.py exists in the plugins folder (flat or cloned layout)."""
        pd = BASE_DIR / "plugins"
        return (pd / "v_plugin.py").exists() or (pd / "v_plugin" / "v_plugin.py").exists()

    def _refresh_start_v_button(self):
        """Start V button was removed (the V-plugin auto-loads with SOC and is
        brought to front from the master widget). Retained as the refresh hook for
        the Smart Cal button, which depends on plugin load state and is called
        from several places."""
        self._refresh_smart_cal_button()

    def _refresh_smart_cal_button(self):
        """Show [◈ Smart Cal] in Phase 1 only when V plugin is loaded."""
        btn = getattr(self, "_smart_cal_btn", None)
        if btn is None:
            return
        if self._vplugin is not None:
            try: btn.pack(side="left", padx=(4, 0))
            except Exception: pass
        else:
            try: btn.pack_forget()
            except Exception: pass

    def _agent3_relay_loop(self):
        """File-drop relay for Agent 3 (Claude Code backend).
        Polls agent3_out.txt every 0.5 s. When the file has content, reads it,
        clears the file, and routes the text as if OCR saw it from agent3.
        This lets the Claude Code CLI inject messages without needing a visible
        window on screen — no OCR region required for agent3."""
        relay_path = BASE_DIR / "agent3_out.txt"
        while True:
            try:
                if relay_path.exists():
                    raw = relay_path.read_text(encoding="utf-8").strip()
                    if raw:
                        relay_path.write_text("", encoding="utf-8")
                        self._log(f"[agent3-relay] received {len(raw)} chars — routing")
                        threading.Thread(
                            target=self._route_text,
                            args=(raw, "agent3"),
                            daemon=True,
                        ).start()
            except Exception as exc:
                self._log(f"[agent3-relay] error: {exc}")
            time.sleep(0.5)

    def _poll_vplugin_file(self):
        """Poll every 5 s for plugins/v_plugin.py appearing on disk.
        Once detected, refreshes plugin-dependent buttons without restarting."""
        if self._vplugin is None:
            self._refresh_start_v_button()
        self.root.after(5000, self._poll_vplugin_file)

    # ── Master-widget control channel ───────────────────────────────────────────
    SOC_CONTROL_FILE = "soc_control.signal"

    def _soc_control_loop(self):
        """File-signal control channel for the SOC Master Widget (separate process).
        Polls soc_control.signal every 0.5 s. The widget writes a one-word command;
        we execute it on the Tk main thread and clear the file. This lets the master
        widget bring the A4 vision window forward without launching any process
        (so there is nothing that can become a zombie). Commands: 'show_a4'."""
        sig_path = BASE_DIR / self.SOC_CONTROL_FILE
        while True:
            try:
                if sig_path.exists():
                    cmd = sig_path.read_text(encoding="utf-8").strip().lower()
                    if cmd:
                        sig_path.write_text("", encoding="utf-8")
                        self.root.after(0, lambda c=cmd: self._handle_soc_control(c))
            except Exception as exc:
                self._log(f"[soc-control] error: {exc}")
            time.sleep(0.5)

    def _handle_soc_control(self, cmd: str):
        """Dispatch a master-widget control command on the Tk main thread."""
        if cmd == "show_a4":
            if getattr(self, "_vplugin", None) is None:
                self._log("[soc-control] show_a4 — V-plugin not loaded")
                return
            try:
                self._vplugin.show_window()   # raises window + re-resolves endpoint
                self._log("[soc-control] show_a4 — A4 window brought to front")
            except Exception as exc:
                self._log(f"[soc-control] show_a4 failed: {exc}")
        else:
            self._log(f"[soc-control] unknown command: {cmd!r}")

    def _load_plugins(self):
        """Discover and load optional plugins from the plugins/ folder.
        Currently supports: v_plugin (vision/Agent 4). Plugins are optional —
        SOCU runs identically without them.

        Two supported layouts:
          • Flat:   plugins/v_plugin.py            (developer drop-in)
          • Cloned: plugins/v_plugin/v_plugin.py   (installed via SOCU installer,
                                                    full V_plugin git repo)
        """
        if self._disable_vplugin:
            return
        plugin_dir = BASE_DIR / "plugins"
        if not plugin_dir.is_dir():
            return
        # Detect layout. Cloned repo wins if both exist (newer convention).
        v_flat   = plugin_dir / "v_plugin.py"
        v_cloned = plugin_dir / "v_plugin" / "v_plugin.py"
        if v_cloned.exists():
            v_module_path = "plugins.v_plugin.v_plugin"
        elif v_flat.exists():
            v_module_path = "plugins.v_plugin"
        else:
            return
        try:
            # Ensure plugins/ is importable
            if str(BASE_DIR) not in sys.path:
                sys.path.insert(0, str(BASE_DIR))
            import importlib
            _v_plugin = importlib.import_module(v_module_path)
            vlm_cfg = self._load_vlm_config()
            self._vplugin = _v_plugin.load(self, vlm_cfg)
            self._log(f"[plugins] ✓ v_plugin loaded from {v_module_path} "
                      f"(model={self._vplugin.cfg['vlm_model']})")
            self.root.after(0, self._refresh_agent4_button)
            self.root.after(0, self._refresh_start_v_button)
        except Exception as e:
            self._log(f"[plugins] v_plugin failed to load: {e}")
            self._vplugin = None

    def _refresh_agent4_button(self):
        """Show the 👁 A4 button only when the V plugin is loaded."""
        btn = getattr(self, "_a4_btn", None)
        if btn is None:
            return
        if self._vplugin is not None:
            try:
                btn.config(fg=GREEN)
                btn.pack(side="left", padx=(4, 0))
            except Exception:
                pass
        else:
            try:
                btn.pack_forget()
            except Exception:
                pass

    def _toggle_agent4_window(self):
        if self._vplugin is None:
            self._log("[v_plugin] not loaded — drop plugins/v_plugin.py and restart")
            return
        self._vplugin.toggle_window()

    def _load_vlm_config(self) -> dict:
        """Pull VLM keys out of config.json if present."""
        if not CONFIG_FILE.exists():
            return {}
        try:
            data = json.loads(CONFIG_FILE.read_text())
        except Exception:
            return {}
        return {
            "vlm_server_url":  data.get("vlm_server_url"),
            "vlm_model":       data.get("vlm_model"),
            "vlm_timeout":     data.get("vlm_timeout"),
            "vlm_max_tokens":  data.get("vlm_max_tokens"),
            "vlm_temperature": data.get("vlm_temperature"),
        }

    # ── Drag + helpers ────────────────────────────────────────────────────────

    def _log(self, msg: str):
        # Mirror to staging/soc_debug.log so external tools can tail it
        try:
            log_dir = BASE_DIR / "staging"
            log_dir.mkdir(exist_ok=True)
            with open(log_dir / "soc_debug.log", "a", encoding="utf-8") as _lf:
                _lf.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        except Exception:
            pass
        def _do():
            self.log.config(state="normal")
            self.log.insert("end", msg + "\n")
            # Cap log at 500 lines to prevent unbounded memory growth
            line_count = int(self.log.index("end-1c").split(".")[0])
            if line_count > 500:
                self.log.delete("1.0", f"{line_count - 500}.0")
            self.log.see("end")
            self.log.config(state="disabled")
        self.root.after(0, _do)

    def _set_status(self, msg: str):
        self.root.after(0, lambda: self.status_var.set(msg))

    def _toggle_log(self):
        if self._log_open:
            self.log.pack_forget()
            self._log_toggle_btn.config(text="▶ Diagnostics")
            self._log_open = False
        else:
            self.log.pack(fill="both", expand=True, padx=10, pady=(0, 4))
            self._log_toggle_btn.config(text="▼ Diagnostics")
            self._log_open = True
        self.root.after(20, self._fit_window)

    def _copy_log_selection(self, event=None):
        try:
            text = self.log.get("sel.first", "sel.last")
        except tk.TclError:
            text = self.log.get("1.0", "end")
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        return "break"  # suppress default (broken) copy in disabled state

    def _copy_log(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log.get("1.0", "end"))
        self._set_status("Log copied to clipboard")

    def _toggle_virtual_desktop(self):
        if not _VDD_OK:
            self._log("VDD not available — run setup_vdd.bat as Administrator first", "warn")
            self._set_status("Virtual display driver not installed")
            return
        if self._vdd_controller is None:
            self._vdd_controller = _VddController()
        ctrl = self._vdd_controller
        if not ctrl.is_available():
            self._log("vdd executable not found — run setup_vdd.bat as Administrator", "warn")
            self._set_status("vdd not found — see setup_vdd.bat")
            return
        if not self._vdd_active:
            ok = ctrl.add(width=1920, height=2160)
            if ok:
                self._vdd_active = True
                self._vdd_btn.config(fg=GREEN)
                self._log("Virtual display added (1920×2160) — recalibrate OCR regions", "ok")
                self._set_status("Virtual display ON")
            else:
                self._log("Failed to add virtual display", "warn")
                self._set_status("Virtual display add failed")
        else:
            ok = ctrl.remove_all()
            self._vdd_active = False
            self._vdd_btn.config(fg="#888888")
            if ok:
                self._log("Virtual display removed", "ok")
                self._set_status("Virtual display OFF")
            else:
                self._log("Virtual display remove returned non-zero", "warn")
                self._set_status("Virtual display remove failed")

    def _fit_window(self):
        """Resize window height to exactly match packed content."""
        self.root.update_idletasks()
        h = self.root.winfo_reqheight()
        x = self.root.winfo_x()
        y = self.root.winfo_y()
        self.root.geometry(f"{self._win_w}x{h}+{x}+{y}")

    def _drag_start(self, e):
        self._drag_x = e.x_root - self.root.winfo_x()
        self._drag_y = e.y_root - self.root.winfo_y()

    def _drag_move(self, e):
        self.root.geometry(
            f"+{e.x_root - self._drag_x}+{e.y_root - self._drag_y}")

    # ── Config persistence ───────────────────────────────────────────────────────────────

    def _save_config(self):
        """Persist window titles, prefix settings, auto-click states, and
        calibrated coordinates. Coordinates are saved so restart skips
        template matching. Use Re-calibrate if windows have moved."""
        import json
        data = {}
        for aid, cfg in self.agents.items():
            data[aid] = {
                "window_title":   cfg.title if cfg.title != "(not set)" else None,
                "prefix_enabled": cfg.prefix_enabled.get() if cfg.prefix_enabled else False,
                "prefix_text":    cfg.prefix_var.get()     if cfg.prefix_var    else "",
                "ocr_region":     list(cfg.ocr_region)    if cfg.ocr_region    else None,
                "input_xy":       list(cfg.input_xy)      if cfg.input_xy      else None,
                "send_xy":        list(cfg.send_xy)        if cfg.send_xy       else None,
                "scroll_dn_xy":   list(cfg.scroll_dn_xy)  if cfg.scroll_dn_xy  else None,
            }
        data["project_name"]      = self._project_name_var.get()
        data["agent3_outbox_path"] = self._agent3_outbox_var.get()
        data["agent3_workspace"]   = self._agent3_workspace_var.get()
        data["bypass_agent3"]    = self._bypass_agent3
        data["bypass_agent5"]    = self._bypass_agent5
        data["disable_vplugin"]  = self._disable_vplugin
        data["agent2_read_mode"] = self._agent2_read_mode
        data["cd_disk"]          = dict(self._cd_disk)   # A4/A5 model-swap "CD changer" magazine
        data["model_profiles"]   = dict(self._model_profile_overrides)  # adaptive-guidance overrides
        data["ocr_blindzone"]  = list(self._ocr_blindzone) if self._ocr_blindzone else None
        # VLM / V plugin config (preserved across restarts; safe defaults set
        # by v_plugin if absent)
        if self._vplugin is not None:
            for k in ("vlm_server_url", "vlm_model", "vlm_timeout",
                      "vlm_max_tokens", "vlm_temperature"):
                data[k] = self._vplugin.cfg.get(k)
        # Auto-click toggle states keyed by template stem
        data["autoclick"] = {
            stem: var.get() for stem, var in self._autoclick_vars.items()
        }
        try:
            CONFIG_FILE.write_text(json.dumps(data, indent=2))
        except Exception as e:
            self._log(f"[config] save error: {e}")

    def _load_config(self):
        """Load config.json → restore window titles, prefix settings,
        auto-click toggle states, and calibrated coordinates.
        Recalibrate manually if windows have moved since last save."""
        if not CONFIG_FILE.exists():
            return
        import json
        try:
            data = json.loads(CONFIG_FILE.read_text())
        except Exception as e:
            self._log(f"[config] load error: {e}")
            return
        for aid, cfg in self.agents.items():
            d = data.get(aid, {})
            if d.get("window_title"):
                cfg.title = d["window_title"]
            if cfg.prefix_var and d.get("prefix_text"):
                cfg.prefix_var.set(d["prefix_text"])
            if cfg.prefix_enabled and d.get("prefix_enabled"):
                cfg.prefix_enabled.set(True)
            if self._valid_region(d.get("ocr_region")):
                cfg.ocr_region = tuple(d["ocr_region"])
                x1, y1, x2, y2 = cfg.ocr_region
                w, h = x2 - x1, y2 - y1
                if cfg.lbl_region:
                    cfg.lbl_region.config(
                        text=f"region: {w}x{h}px ({x1},{y1})", fg=GREEN)
            if d.get("input_xy"):
                cfg.input_xy = tuple(d["input_xy"])
                if cfg.lbl_input:
                    cfg.lbl_input.config(
                        text=f"input: {cfg.input_xy}", fg=GREEN)
            if d.get("send_xy"):
                cfg.send_xy = tuple(d["send_xy"])
                if cfg.lbl_send:
                    cfg.lbl_send.config(
                        text=f"send: {cfg.send_xy}", fg=GREEN)
            if d.get("scroll_dn_xy"):
                cfg.scroll_dn_xy = tuple(d["scroll_dn_xy"])
        if data.get("project_name"):
            self._project_name_var.set(data["project_name"])
        if data.get("agent3_outbox_path"):
            self._agent3_outbox_var.set(data["agent3_outbox_path"])
        if data.get("agent3_workspace"):
            self._agent3_workspace_var.set(data["agent3_workspace"])
        # Restore agent3 bypass state (default True if not in config)
        self._bypass_agent3 = data.get("bypass_agent3", True)
        # Restore agent5 bypass state (default True if not in config)
        self._bypass_agent5 = data.get("bypass_agent5", True)
        # Restore A4/A5/A6/A7 model-swap "CD changer" disk tokens
        _cd = data.get("cd_disk") or {}
        for _aid in ("agent4", "agent5", "agent6", "agent7"):
            _name = (_cd.get(_aid) or "").strip()
            if _name:
                self._cd_disk[_aid] = _name
                if _aid in self._cd_disk_var:
                    self._cd_disk_var[_aid].set(_name)
        # Restore adaptive-guidance overrides (operator-set: name-substring → tier)
        _mp = data.get("model_profiles")
        self._model_profile_overrides = dict(_mp) if isinstance(_mp, dict) else {}
        # Restore V plugin disabled state (default False — enabled)
        self._disable_vplugin = data.get("disable_vplugin", False)
        if hasattr(self, "_disable_v_btn"):
            if self._disable_vplugin:
                self._disable_v_btn.config(text="V:⊘", fg="#666666")
            else:
                self._disable_v_btn.config(text="V:on", fg="#4ec9b0")
        # Restore Agent 2 read mode (default "file" — file-relay outbox)
        self._agent2_read_mode = data.get("agent2_read_mode", "file")
        if hasattr(self, "_a2_mode_btn"):
            self._refresh_a2_mode_button()
        # Restore OCR blind zone
        bz = data.get("ocr_blindzone")
        self._ocr_blindzone = tuple(bz) if bz else None
        if self._ocr_blindzone and hasattr(self, "_blindzone_btn"):
            x0, y0, x1, y1 = self._ocr_blindzone
            self._blindzone_btn.config(text=f"🚫 zone active", fg=RED)
        if hasattr(self, "_a3_bypass_btn"):
            if self._bypass_agent3:
                self._a3_bypass_btn.config(text="⊘ Agent 3  [bypassed]", fg="#666666")
                self._a3_panel_frame.pack_forget()
            else:
                self._a3_bypass_btn.config(text="● Agent 3  [active]", fg=GREEN)
                self._a3_panel_frame.pack(fill="x")
                if "agent3" in self._hold_btns:
                    self._hold_btns["agent3"].pack(side="left", padx=(0, 4),
                                                   before=self._pause_btn)
        if hasattr(self, "_a5_bypass_btn"):
            if self._bypass_agent5:
                self._a5_bypass_btn.config(
                    text="⊘ Agent 5  [bypassed]  (GGUF Chatbox)", fg="#666666")
                self._a5_panel_frame.pack_forget()
            else:
                self._a5_bypass_btn.config(
                    text="● Agent 5  [active]  (GGUF Chatbox)", fg=GREEN)
                self._a5_panel_frame.pack(fill="x")
        # Restore auto-click toggle states
        for stem, enabled in data.get("autoclick", {}).items():
            # Sequence-critical stems are hardcoded — never restore from saved state
            if any(p in stem.lower() for p in AUTOCLICK_SEQUENCE):
                continue
            if stem in self._autoclick_vars:
                self._autoclick_vars[stem].set(bool(enabled))
            else:
                # Template added since last save — var will be created by
                # _refresh_autoclick_list(); store the saved value for it
                var = tk.BooleanVar(value=bool(enabled))
                self._autoclick_vars[stem] = var
            # Keep the thread-safe plain set in sync with restored state
            if bool(enabled):
                self._autoclick_enabled.add(stem)
            else:
                self._autoclick_enabled.discard(stem)
        self._log("[config] window titles + prefix settings + auto-click states restored")
        self._auto_locate_windows()
        self.root.after(200, self._check_phase1_complete)
        self.root.after(300, lambda: self._show_phase(
            3 if self._calibration_complete() else 1))

    def _auto_locate_windows(self):
        """Find agent windows by matching saved title strings against open windows."""
        try:
            live = PLATFORM.find_windows()
            for aid, cfg in self.agents.items():
                if not cfg.title or cfg.title == "(not set)" or cfg.hwnd:
                    continue
                for hwnd, title in live:
                    # Partial match — tolerates tab-name changes
                    if _title_match(cfg.title, title):
                        cfg.hwnd = hwnd
                        short = (title[:26] + "…") if len(title) > 26 else title
                        cfg.lbl_window.config(text=f"window: {short} ⋅auto", fg=GREEN)
                        self._log(f"[{aid}] window auto-located: {title}")
                        break
        except ImportError:
            pass
        except Exception as e:
            self._log(f"[config] window locate error: {e}")

    def _startup_calibrate(self):
        """Auto-run calibration on startup if templates exist.
        Templates find current on-screen positions — always accurate
        regardless of where windows were moved since last session."""
        templates = list(TEMPLATE_DIR.glob("*.png"))
        if not templates:
            self._log("[startup] no templates yet — hover-capture each target to train")
            return
        self._log(f"[startup] {len(templates)} template(s) — locating targets on screen…")
        threading.Thread(target=self._auto_calibrate, daemon=True).start()

    # ── Template matching + auto-calibration ──────────────────────────────────
    #
    # Naming convention for PNGs in 'buttons database/':
    #   agent1_input.png      → Agent 1 chat input field
    #   agent1_send.png       → Agent 1 send button
    #   agent1_scroll_dn.png  → Agent 1 scroll-down arrow
    #   agent1_scroll_up.png  → Agent 1 scroll-up arrow
    #   agent2_*              → same for Agent 2
    #
    # ⌖ Calibrate takes ONE screenshot and matches all templates at once.
    # Thin buttons (scroll arrows) work fine — OpenCV sub-pixel matching.
    # Multi-step sequences: Scroll Read uses scroll_dn_xy in a loop.

    def _apply_blindzone(self, img: "Image.Image", rx0: int, ry0: int) -> "Image.Image":
        """Mask the OCR blind zone out of a captured screen region before Tesseract sees it."""
        bz = self._ocr_blindzone
        if not bz:
            return img
        bx0, by0, bx1, by1 = bz
        ix0 = max(0, bx0 - rx0)
        iy0 = max(0, by0 - ry0)
        ix1 = min(img.width, bx1 - rx0)
        iy1 = min(img.height, by1 - ry0)
        if ix1 > ix0 and iy1 > iy0:
            from PIL import ImageDraw
            img = img.copy()
            ImageDraw.Draw(img).rectangle([ix0, iy0, ix1 - 1, iy1 - 1], fill=(220, 220, 220))
        return img

    def _set_blindzone_mode(self):
        """Click to set blind zone from window under cursor, or clear if already set."""
        if self._ocr_blindzone:
            self._ocr_blindzone = None
            self._blindzone_btn.config(text="🚫 Blind Zone", fg="#888888")
            self._save_config()
            self._log("[blind zone] cleared")
            self._set_status("OCR blind zone cleared")
            return
        self._set_status("Click any window to set as OCR blind zone…")
        self._blindzone_btn.config(text="● clicking…", fg=ORANGE)

        def _capture():
            deadline = time.time() + 2.0
            while time.time() < deadline:
                if not PLATFORM.left_button_down():
                    break
                time.sleep(0.02)
            deadline = time.time() + 15.0
            while time.time() < deadline:
                if PLATFORM.left_button_down():
                    px, py_ = PLATFORM.cursor_pos()
                    got = PLATFORM.window_from_point(px, py_)
                    if not got:
                        time.sleep(0.02)
                        continue
                    _hwnd, title, _cls, rect = got
                    title = title or "(unknown)"
                    x0, y0, x1, y1 = rect
                    while PLATFORM.left_button_down():
                        time.sleep(0.01)
                    self._ocr_blindzone = (x0, y0, x1, y1)
                    self._save_config()
                    short = (title[:18] + "…") if len(title) > 18 else title

                    def _ui(s=short, t=title, _x0=x0, _y0=y0, _x1=x1, _y1=y1):
                        self._blindzone_btn.config(text=f"🚫 {s}", fg=RED)
                        self._log(f"[blind zone] '{t}'  ({_x0},{_y0})→({_x1},{_y1})")
                        self._set_status(f"OCR blind zone: {_x0},{_y0}→{_x1},{_y1}  (click again to clear)")
                    self.root.after(0, _ui)
                    return
                time.sleep(0.02)
            self.root.after(0, lambda: (
                self._blindzone_btn.config(text="🚫 Blind Zone", fg="#888888"),
                self._set_status("Blind zone pick timed out")))

        threading.Thread(target=_capture, daemon=True).start()

    def _recalibrate(self):
        """Clear saved coordinates for all agents and run fresh template matching.
        Use when windows have moved or UI has changed since last calibration.
        Scroll coords are manually calibrated and preserved — template match
        updates them if it finds a better value, but never clears them."""
        for cfg in self.agents.values():
            cfg.input_xy = None
            cfg.send_xy  = None
            if cfg.lbl_input:
                cfg.lbl_input.config(text="input: —", fg=FG)
            if cfg.lbl_send:
                cfg.lbl_send.config(text="send: —", fg=FG)
        self._log("[cal] input/send coords cleared — scroll coords preserved — re-running calibration")
        self._set_status("Re-calibrating…")
        threading.Thread(target=self._auto_calibrate, daemon=True).start()

    def _smart_calibrate(self):
        """VLM-assisted calibration using V plugin. Runs in background thread."""
        if self._vplugin is None:
            self._set_status("Smart Cal: V plugin not loaded")
            return
        threading.Thread(target=self._smart_calibrate_thread, daemon=True).start()

    def _smart_calibrate_thread(self):
        import json as _json

        self._set_status("Smart Cal: querying vision model…")
        self._log("[smart-cal] starting VLM-assisted calibration…")

        # Build cheat sheet: describe known element types by naming convention
        cheat = (
            "UI element naming guide:\n"
            "  input / input_field  — text box where the user types messages\n"
            "  send / send_button   — button that submits the message (arrow, enter, send icon)\n"
            "  scroll_dn            — scroll-down arrow or chevron\n"
            "  scroll_up            — scroll-up arrow or chevron\n"
            "  copy_button          — copy icon next to a message\n"
            "  allow_button         — permission/allow button that appears in dialogs\n"
            "Geo markers (visual anchors, not interactive):\n"
            + "\n".join(
                f"  {f.stem}" for f in sorted(TEMPLATE_DIR.glob("*.png"))
                if any(p in f.stem.lower() for p in AUTOCLICK_HIDDEN)
            )
        )

        calibrated = 0
        for aid, cfg in self.agents.items():
            if not cfg.hwnd:
                self._log(f"[smart-cal] {aid}: no window set — skipping")
                continue
            rect = PLATFORM.get_window_rect(cfg.hwnd)   # (x0, y0, x1, y1)
            if rect is None:
                self._log(f"[smart-cal] {aid}: get_window_rect failed")
                continue

            x0, y0, x1, y1 = rect
            if x1 - x0 <= 0 or y1 - y0 <= 0:
                self._log(f"[smart-cal] {aid}: window has zero size — skipping")
                continue

            prompt = (
                f"You are calibrating UI automation for a chat window (agent id: {aid}).\n\n"
                f"{cheat}\n\n"
                "Look at the screenshot of this chat window and locate:\n"
                "  input, send, scroll_dn, scroll_up\n\n"
                "Return ONLY a JSON object — no explanation, no markdown:\n"
                '{"input": [x, y], "send": [x, y], "scroll_dn": [x, y], "scroll_up": [x, y]}\n\n'
                "Use null for any element not visible. "
                "Coordinates are pixel positions within the screenshot."
            )

            self._log(f"[smart-cal] {aid}: sending window crop to VLM…")
            response = self._vplugin.query_vision(prompt, region=rect)

            if response.startswith("ERROR:"):
                self._log(f"[smart-cal] {aid}: {response}")
                continue

            # Parse JSON — strip markdown fences if the model added them
            text = response.strip()
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]

            try:
                coords = _json.loads(text)
            except Exception as e:
                self._log(f"[smart-cal] {aid}: JSON parse error: {e}\n  raw: {text[:300]}")
                continue

            role_map = {"input": "input", "send": "send",
                        "scroll_dn": "scroll_dn", "scroll_up": "scroll_up"}
            applied = 0
            for key, role in role_map.items():
                val = coords.get(key)
                if not val or not isinstance(val, list) or len(val) < 2:
                    continue
                try:
                    sx = x0 + int(val[0])
                    sy = y0 + int(val[1])
                    self._apply_template_match(f"{aid}_{role}", (sx, sy), 0.90)
                    self._log(f"[smart-cal] {aid}.{role} → ({sx},{sy})")
                    applied += 1
                except Exception as e:
                    self._log(f"[smart-cal] {aid}.{role}: {e}")

            self._log(f"[smart-cal] {aid}: {applied}/4 applied")
            calibrated += applied

        self._save_config()
        self._save_registry()
        self._set_status(f"Smart Cal: {calibrated} coordinate(s) set")
        if hasattr(self, "_cal_status_lbl"):
            self.root.after(0, lambda n=calibrated:
                self._cal_status_lbl.config(
                    text=f"smart cal: {n} set",
                    fg=GREEN if n > 0 else ORANGE))
        self.root.after(0, self._check_phase1_complete)

    def _auto_calibrate(self):
        """Screenshot → match all templates → fill agent coordinates."""
        if not _CV2_OK:
            self._log("[cal] opencv-python not installed.\n"
                      "      Run:  pip install opencv-python numpy")
            return
        templates = list(TEMPLATE_DIR.glob("*.png"))
        if not templates:
            self._log(
                f"[cal] 'buttons database' is empty — drop cropped PNGs here:\n"
                "        agent1_input.png   agent1_send.png\n"
                "        agent1_scroll_dn.png  agent1_scroll_up.png\n"
                "        agent2_input.png   agent2_send_CLD.png\n"
                "        agent2_scroll_dn.png  agent2_scroll_up.png")
            return
        self._log(f"[cal] scanning screen against {len(templates)} templates…")
        with _mss_ctor() as sct:
            raw = sct.grab(sct.monitors[1])
            screen_img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        screen_gray = cv2.cvtColor(np.array(screen_img), cv2.COLOR_RGB2GRAY)
        found = 0
        for tpl_path in sorted(templates):
            tpl = self._safe_imread(tpl_path, cv2.IMREAD_GRAYSCALE)
            if tpl is None:
                self._log(f"[cal] could not load {tpl_path.name}")
                continue
            th, tw = tpl.shape
            sh, sw = screen_gray.shape
            if th > sh or tw > sw:
                self._log(f"[cal] {tpl_path.name} larger than screen — skip")
                continue
            res = cv2.matchTemplate(screen_gray, tpl, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if max_val < TEMPLATE_THRESH:
                self._log(f"[cal] {tpl_path.name}  no match (best={max_val:.2f})")
                continue
            cx = max_loc[0] + tw // 2
            cy = max_loc[1] + th // 2
            self._apply_template_match(tpl_path.stem.lower(), (cx, cy), max_val)
            found += 1
        self._log(f"[cal] done — {found}/{len(templates)} matched")
        self._set_status(f"Calibrated: {found}/{len(templates)} templates found")
        self._save_registry()   # single JSON write after all templates processed
        self._save_config()
        n_done, t_total = found, len(templates)
        if hasattr(self, "_cal_status_lbl"):
            self.root.after(0, lambda n=n_done, t=t_total:
                self._cal_status_lbl.config(
                    text=f"{n}/{t} matched",
                    fg=GREEN if n == t else ORANGE))
        self.root.after(0, self._check_phase1_complete)

    def _apply_template_match(self, stem: str, xy: tuple, conf: float):
        """Map a template filename stem to the right AgentConfig slot
        and update the training registry for that template."""
        for aid in ("agent1", "agent2", "agent3", "agent5"):
            if not stem.startswith(aid + "_"):
                continue
            role = stem[len(aid) + 1:]   # input / send / scroll_dn / scroll_up
            cfg  = self.agents[aid]
            x, y = xy

            # Bounds check: scroll buttons must be inside the OCR region (the chat area).
            # Input fields and send buttons are in the toolbar BELOW the OCR region —
            # they are intentionally excluded from this check.
            if role in ("scroll_dn", "scroll_up") and cfg.ocr_region:
                rx0, ry0, rx1, ry1 = cfg.ocr_region
                if not (rx0 <= x <= rx1 and ry0 <= y <= ry1):
                    self._log(f"[cal] {aid}.{role} → ({x},{y}) outside OCR region — skipped")
                    return
            elif role in ("input", "send") and cfg.hwnd:
                # For input/send: reject only if truly outside the window frame
                try:
                    r = PLATFORM.get_window_rect(cfg.hwnd)
                    if r and not (r[0] <= x <= r[2] and r[1] <= y <= r[3]):
                        self._log(f"[cal] {aid}.{role} → ({x},{y}) outside window — skipped")
                        return
                except Exception:
                    pass

            # ── Update training registry ──────────────────────────────────
            key = f"{stem}.png"
            rec = self._registry.setdefault(key, {
                "matches": 0, "conf_sum": 0.0, "trained": False,
                "action": self._infer_action(role)})
            rec["matches"]  += 1
            rec["conf_sum"] += conf
            avg = rec["conf_sum"] / rec["matches"]
            just_trained = not rec["trained"] and rec["matches"] >= TRAINED_THRESHOLD
            if just_trained:
                rec["trained"] = True
            # Note: _save_registry() is called once by _auto_calibrate after all templates,
            # not per-template, to avoid N redundant disk writes per calibration run.

            # ── Log training progress ─────────────────────────────────────
            n, needed = rec["matches"], TRAINED_THRESHOLD
            if just_trained:
                self._log(f"[★ TRAINED] {key}  —  action={rec['action']}  "
                          f"avg_conf={avg:.2f}  ({n} matches)")
            elif rec["trained"]:
                self._log(f"[cal] {aid}.{role} → ({x},{y})  "
                          f"conf={conf:.2f}  ★trained ({n} matches)")
            else:
                bar = "█" * n + "·" * (needed - n)
                self._log(f"[cal] {aid}.{role} → ({x},{y})  "
                          f"conf={conf:.2f}  [{bar}] {n}/{needed}")

            # ── Fill agent config slot (never overwrite manually set coords) ──
            def _ui(r=role, c=cfg, px=x, py=y, trained=rec["trained"]):
                colour = GREEN if trained else ACCENT
                if r == "input":
                    if c.input_xy is None:
                        c.input_xy = (px, py)
                    c.lbl_input.config(text=f"input field: ({c.input_xy})", fg=colour)
                elif r == "send":
                    if c.send_xy is None:
                        c.send_xy = (px, py)
                    c.lbl_send.config(text=f"send button: ({c.send_xy})", fg=colour)
                elif r == "scroll_dn":
                    if c.scroll_dn_xy is None:
                        c.scroll_dn_xy = (px, py)
                    c.lbl_scroll.config(text=f"scroll↓: ({c.scroll_dn_xy})", fg=colour)
                elif r == "scroll_up":
                    if c.scroll_up_xy is None:
                        c.scroll_up_xy = (px, py)
                    c.lbl_scroll.config(text=f"scroll↑↓: ({c.scroll_up_xy})", fg=colour)
            self.root.after(0, _ui)
            return

        # Not an agent routing template — generic auto-click target.
        # Update registry stats so training counts accumulate; no slot to fill.
        key = f"{stem}.png"
        rec = self._registry.setdefault(key, {
            "matches": 0, "conf_sum": 0.0, "trained": False, "action": "click"})
        rec["matches"]  += 1
        rec["conf_sum"] += conf
        just_trained = not rec["trained"] and rec["matches"] >= TRAINED_THRESHOLD
        if just_trained:
            rec["trained"] = True
        n, needed = rec["matches"], TRAINED_THRESHOLD
        if rec["trained"]:
            self._log(f"[cal] {stem} → {xy}  conf={conf:.2f}  ★trained ({n} matches)")
        else:
            bar = "█" * n + "·" * (needed - n)
            self._log(f"[cal] {stem} → {xy}  conf={conf:.2f}  [{bar}] {n}/{needed}")

    @staticmethod
    def _infer_action(role: str) -> str:
        """Derive the intended automation action from a template role name."""
        return {
            "input":     "focus_paste",   # click to focus, then Ctrl+V
            "send":      "click",          # single click
            "scroll_dn": "click",          # click scroll-down arrow
            "scroll_up": "click",          # click scroll-up arrow
        }.get(role, "click")

    # ── Shared helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _valid_region(r) -> bool:
        """Validate an ocr_region value from config JSON.
        Must be a list/tuple of 4 ints with x1 < x2, y1 < y2, all within 8192px."""
        return (
            isinstance(r, (list, tuple)) and len(r) == 4
            and all(isinstance(v, int) for v in r)
            and 0 <= r[0] < r[2] <= 8192
            and 0 <= r[1] < r[3] <= 8192
        )

    # PNG magic bytes — first 8 bytes of any valid PNG file
    _PNG_MAGIC = b'\x89PNG\r\n\x1a\n'

    def _safe_imread(self, path: "Path", flags: int = None) -> "np.ndarray | None":
        """Read an image via OpenCV only after verifying the PNG magic bytes.
        Guards against malformed or non-PNG files in the user-writable templates dir."""
        if not _CV2_OK:
            return None
        try:
            if path.read_bytes()[:8] != self._PNG_MAGIC:
                return None
        except OSError:
            return None
        _flags = cv2.IMREAD_COLOR if flags is None else flags
        return cv2.imread(str(path), _flags)

    def _load_template_cached(self, stem: str, png: "Path") -> "np.ndarray | None":
        """Return the OpenCV image for a template, loading from disk only when
        the file's mtime changes. Eliminates continuous disk reads in the
        auto-click scan loop at 1.5s intervals."""
        if not _CV2_OK:
            return None
        try:
            mtime = png.stat().st_mtime
        except OSError:
            return None
        entry = self._template_cache.get(stem)
        if entry and entry[0] == mtime:
            return entry[1]
        img = self._safe_imread(png)
        if img is not None:
            self._template_cache[stem] = (mtime, img)
        return img

    def _find_two_buttons(self, agent_id: str) -> "tuple":
        """Take one full-screen screenshot and locate both the input field and
        the send button for agent_id via template matching.
        Returns a 2-tuple ((ix,iy), (sx,sy)); either entry may be None.
        Collects ALL matching templates per role (sorted for determinism) and
        returns the first on-screen hit — prevents early-return on a stale template
        blocking a trained one (e.g. agent2_send.png failing before agent2_send_cld.png)."""
        if not _CV2_OK:
            return None, None
        ag_num = agent_id[-1]   # '1' or '2'
        input_tpls: "list[np.ndarray]" = []
        send_tpls:  "list[np.ndarray]" = []
        for png in sorted(TEMPLATE_DIR.iterdir()):
            if png.suffix.lower() != ".png":
                continue
            s = png.stem.lower()
            if f"agent{ag_num}" not in s:
                continue
            tpl = self._safe_imread(png, cv2.IMREAD_GRAYSCALE)
            if tpl is None:
                continue
            if "input" in s:
                input_tpls.append(tpl)
            elif "send" in s:
                send_tpls.append(tpl)
        if not input_tpls and not send_tpls:
            return None, None
        with _mss_ctor() as sct:
            raw = sct.grab(sct.monitors[1])
            gray = cv2.cvtColor(
                np.array(Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")),
                cv2.COLOR_RGB2GRAY)

        def _match(tpl: "np.ndarray") -> "tuple | None":
            th, tw = tpl.shape
            res = cv2.matchTemplate(gray, tpl, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if max_val >= TEMPLATE_THRESH:
                return (max_loc[0] + tw // 2, max_loc[1] + th // 2)
            return None

        def _match_first(tpls: "list") -> "tuple | None":
            for tpl in tpls:
                hit = _match(tpl)
                if hit:
                    return hit
            return None

        return _match_first(input_tpls), _match_first(send_tpls)

    def _load_registry(self) -> dict:
        """Load template training history from registry.json."""
        if not REGISTRY_FILE.exists():
            return {}
        import json
        try:
            return json.loads(REGISTRY_FILE.read_text())
        except Exception:
            return {}

    def _save_registry(self):
        """Write current training registry to registry.json."""
        import json
        try:
            REGISTRY_FILE.write_text(
                json.dumps(self._registry, indent=2))
        except Exception as e:
            self._log(f"[registry] save error: {e}")

    def _find_agent_button_xy(self, agent_id: str, role: str) -> tuple | None:
        """Locate agent input field or send button via template matching.
        role = 'input' or 'send'.  Returns (x,y) centre or None.
        Tries ALL matching templates in sorted order — first on-screen hit wins.
        Sorted so trained variants (e.g. agent2_send_cld.png) are tried
        predictably instead of depending on filesystem iteration order."""
        if not _CV2_OK:
            return None
        ag_num = agent_id[-1]   # '1' or '2'
        kw = {"input": "input", "send": "send"}.get(role, "")

        # Build a bounding box from the OCR region to reject false positives that
        # land in another agent's window.  Use a tighter x-margin (buttons live
        # within the window width) but a larger bottom margin (send/input sit up
        # to ~100px below the OCR region's lower edge).
        cfg = self.agents.get(agent_id)
        bounds = None
        if cfg and cfg.ocr_region:
            rx0, ry0, rx1, ry1 = cfg.ocr_region
            bounds = (rx0 - 60, ry0 - 60, rx1 + 60, ry1 + 120)

        for png in sorted(TEMPLATE_DIR.iterdir()):
            if png.suffix.lower() != ".png":
                continue
            stem = png.stem.lower()
            if f"agent{ag_num}" in stem and kw in stem:
                found = self._find_template(png.name)
                if found:
                    if bounds and not (bounds[0] <= found[0] <= bounds[2]
                                       and bounds[1] <= found[1] <= bounds[3]):
                        self._log(f"[tmpl] {png.name} @ {found} outside {agent_id} bounds — skipped")
                        continue
                    return found
        return None

    def _find_template(self, name: str, thresh: float = TEMPLATE_THRESH) -> tuple | None:
        """Find a single named template on screen. Returns (x,y) centre or None.
        thresh overrides TEMPLATE_THRESH for templates that need a looser match."""
        if not _CV2_OK:
            return None
        tpl_path = TEMPLATE_DIR / name
        if not tpl_path.exists():
            return None
        tpl = self._safe_imread(tpl_path, cv2.IMREAD_GRAYSCALE)
        if tpl is None:
            return None
        with _mss_ctor() as sct:
            raw = sct.grab(sct.monitors[1])
            gray = cv2.cvtColor(
                np.array(Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")),
                cv2.COLOR_RGB2GRAY)
        th, tw = tpl.shape
        res = cv2.matchTemplate(gray, tpl, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        if max_val >= thresh:
            return (max_loc[0] + tw // 2, max_loc[1] + th // 2)
        return None

    def _find_template_at(self, name: str, cx: int, cy: int,
                          margin: int = 60, thresh: float = TEMPLATE_THRESH) -> tuple | None:
        """Like _find_template but searches only within margin pixels of (cx, cy).
        Much faster and more reliable when the button is always at a known screen position."""
        if not _CV2_OK:
            return None
        tpl_path = TEMPLATE_DIR / name
        if not tpl_path.exists():
            return None
        tpl = self._safe_imread(tpl_path, cv2.IMREAD_GRAYSCALE)
        if tpl is None:
            return None
        th, tw = tpl.shape
        x0, y0 = max(0, cx - margin), max(0, cy - margin)
        x1, y1 = cx + margin, cy + margin
        with _mss_ctor() as sct:
            raw = sct.grab({"left": x0, "top": y0, "width": x1 - x0, "height": y1 - y0})
            gray = cv2.cvtColor(
                np.array(Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")),
                cv2.COLOR_RGB2GRAY)
        if gray.shape[0] < th or gray.shape[1] < tw:
            return None
        res = cv2.matchTemplate(gray, tpl, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        if max_val >= thresh:
            return (x0 + max_loc[0] + tw // 2, y0 + max_loc[1] + th // 2)
        return None

    # ── Scroll-while-read ─────────────────────────────────────────────────────
    #
    # Sequence per step:
    #   1. OCR visible screen area
    #   2. Merge new lines into rolling buffer (overlap-deduplicated)
    #   3. Check buffer for complete  To agentX … end message now  block
    #   4. If found → route + stop
    #   5. Scroll down:  click scroll_dn_xy  OR  mouse-wheel if no template
    #   6. Wait SCROLL_PAUSE, repeat up to SCROLL_MAX_STEPS

    def _start_scroll_read(self, agent_id: str):
        """Launch scroll-read in a background thread."""
        threading.Thread(
            target=self._scroll_read_thread,
            args=(agent_id,), daemon=True).start()
        self._log(f"[scroll] starting scroll-read on {agent_id}")
        self._set_status(f"Scroll reading {agent_id}…")

    def _scroll_read_thread(self, agent_id: str):
        """Scroll the agent window down, OCR-ing each view, until the
        full  To agentX … end message now  block is assembled."""
        cfg = self.agents.get(agent_id)
        if not cfg or not cfg.hwnd:
            self._log(f"[scroll] {agent_id} window not set — click Set Window first")
            return
        try:
            PLATFORM.focus_window(cfg.hwnd)
            time.sleep(0.3)
        except Exception as exc:
            self._log(f"[scroll] focus error: {exc}")
            return

        buffer = ""
        # Determine grab box once — region won’t change during a scroll run
        _grab_init: dict | None = None
        if cfg.ocr_region:
            x1, y1, x2, y2 = cfg.ocr_region
            _grab_init = {"left": x1, "top": y1,
                          "width": x2 - x1, "height": y2 - y1}

        with _mss_ctor() as sct:
            grab_box = _grab_init if _grab_init else sct.monitors[1]
            for step in range(SCROLL_MAX_STEPS):
                # 1. OCR current view
                raw = sct.grab(grab_box)
                img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
                new_text = pytesseract.image_to_string(img, config="--psm 6")

                # 2. Merge — append only lines not already in buffer tail
                buffer += "\n" + self._merge_scroll_text(buffer, new_text)

                # 3. Check for complete message block
                low = buffer.lower()
                if (re.search(r"to\s+agent\s*[12]", low)
                        and any(v in low for v in _SENTINEL_VARIANTS)):
                    n = self._route_text(buffer)
                    if n > 0:
                        self._log(f"[scroll] ✓ message routed (step {step + 1})")
                        self._set_status(f"Scroll read: message routed from {agent_id}")
                        return

                # 4. Scroll down
                if cfg.scroll_dn_xy:
                    pyautogui.click(*cfg.scroll_dn_xy)
                else:
                    sw, sh = pyautogui.size()
                    pyautogui.scroll(-5, x=sw // 2, y=sh // 2)

                time.sleep(SCROLL_PAUSE)

        self._log(f"[scroll] max steps ({SCROLL_MAX_STEPS}) reached — no message found")
        self._set_status("Scroll read: no message found")

    def _merge_scroll_text(self, existing: str, new_text: str) -> str:
        """Append lines from new_text that don't already appear in the
        last 25 lines of existing, then return the full combined buffer.
        Handles overlapping scroll views — each merge grows the buffer."""
        if not existing.strip():
            return new_text
        tail = {ln.strip().lower()
                for ln in existing.strip().splitlines()[-25:]
                if ln.strip()}
        fresh = [ln for ln in new_text.splitlines()
                 if ln.strip().lower() not in tail]
        if not fresh:
            return existing
        return existing.rstrip("\n") + "\n" + "\n".join(fresh)

    # ── Mode system ───────────────────────────────────────────────────────────

    def _update_mode_indicator(self):
        """Update the GUI mode indicator to reflect current state.
        Safe to call from any thread (uses root.after for Tk thread safety).

        Thread-safety note: _mode and _agent2_hold are written from background
        threads without a dedicated lock. In CPython the GIL makes single
        attribute assignments atomic, and all transitions are idempotent, so a
        threading.Lock is not required here. Counters that gate state changes
        (_agent2_impl_attempts, etc.) are mutated only inside _inject_lock."""
        if self._agent2_hold:
            color = RED
            label = "⚠ AGENT2 HOLD"
            sub   = "Runaway prevented. Click Disengage to reset."
            dis_bg, dis_fg = RED, "white"
        elif self._mode == "implementation":
            color = GREEN
            label = "IMPLEMENTATION MODE"
            sub   = "Executing stored blocks."
            dis_bg, dis_fg = BG2, ORANGE
        else:
            color = ACCENT   # blue
            label = "MODULE BLOCK MODE"
            sub   = "Storing blocks only. Implementation disabled."
            dis_bg, dis_fg = BG2, FG

        def _do():
            self._mode_dot.config(fg=color)
            self._mode_lbl.config(text=label, fg=color)
            self._mode_sub.config(text=sub)
            self._disengage_btn.config(bg=dis_bg, fg=dis_fg)
        self.root.after(0, _do)

    def _disengage_impl_mode(self):
        """User override: reset to MODULE BLOCK MODE and clear any Agent2 HOLD.
        Resets all session counters so anti-drift cadence starts fresh."""
        prev = self._mode
        self._mode                    = "module_block"
        self._agent2_hold             = False
        self._agent2_impl_attempts    = 0
        self._agent1_inbound_count    = 0
        self._consecutive_saved_count = 0
        self._impl_format_count       = {"agent1": 0, "agent2": 0}
        self._update_mode_indicator()
        self._log(
            f"[mode] Disengaged by user  ({prev} → module_block)  "
            "hold + all session counters cleared")
        self._set_status("Mode reset: MODULE BLOCK MODE")

    def _manual_engage_impl_mode(self):
        """User double-click override: force implementation mode from MODULE BLOCK MODE."""
        if self._mode == "implementation":
            return  # already in impl mode, double-click on green label is a no-op
        self._mode = "implementation"
        self._impl_format_count = {"agent1": 0, "agent2": 0}
        self._update_mode_indicator()
        self._log("[mode] ✓ IMPLEMENTATION MODE — manually engaged by user (double-click)")
        self._set_status("Implementation mode engaged manually")

    def _start_agent1(self):
        """Send Agent1 SOP prompt to Agent1's chat window."""
        if not self.agents["agent1"].hwnd:
            self._set_status("Agent 1 window not set — click Set Win after focusing it")
            return
        self._inject_grace["agent1"] = time.time() + 25
        threading.Thread(
            target=self._inject_to_agent,
            args=("agent1", AGENT1_SOP),
            kwargs={"bypass_mode_check": True},
            daemon=True).start()
        self._log("[mode] Agent1 SOP sent — 25s OCR grace active")
        self._set_status("Agent1 SOP sent")

    def _start_agent2(self):
        """Send Agent2 SOP prompt to Agent2's chat window."""
        if not self.agents["agent2"].hwnd:
            self._set_status("Agent 2 window not set — click Set Win after focusing it")
            return
        self._inject_grace["agent2"] = time.time() + 25
        sop = AGENT2_SOP
        if self._agent3_outbox_var.get().strip():
            sop = sop + AGENT2_OUTBOX_NOTE
        threading.Thread(
            target=self._inject_to_agent,
            args=("agent2", sop),
            kwargs={"bypass_mode_check": True},
            daemon=True).start()
        self._log("[mode] Agent2 SOP sent — 25s OCR grace active")
        self._set_status("Agent2 SOP sent")

    # ── Session manager (start/end/refresh lifecycle) ─────────────────────────
    def _flag_session_full_ui(self):
        """Highlight the New Session button when the auto-detect threshold trips."""
        if self._session_btn and not self._session_refresh_pending:
            self._session_btn.config(bg=RED, fg="white", text="↻ New Session ⚠")

    def _beacon_project(self, on: bool):
        """Red pulsing beacon on the Project field — prompts the operator to set
        the NEW project name during a session refresh (between New Session and
        Re-establish). It only *prompts*; switching project context is manual.

        Multi-tenant hook seam: an integration may attach `_project_swap_hook` to
        automate that switch. Unset by default, so this is a no-op unless
        something opts in. See `_project_swap_hook` below."""
        lbl = getattr(self, "_project_label", None)
        ent = getattr(self, "project_entry", None)
        if not lbl:
            return
        self._project_beacon_on = on
        if on:
            try:
                ent.config(highlightthickness=2, highlightbackground=RED,
                           highlightcolor=RED)
            except Exception:
                pass
            self._project_beacon_pulse(True)
            # Multi-tenant hook seam. Set `_project_swap_hook` to a callable
            # taking one str phase argument ("prompt") to automate project
            # switching; unset by default.
            hook = getattr(self, "_project_swap_hook", None)
            if callable(hook):
                try:
                    hook("prompt")
                except Exception:
                    pass
        else:
            after_id = getattr(self, "_project_beacon_after", None)
            if after_id:
                try:
                    self.root.after_cancel(after_id)
                except Exception:
                    pass
                self._project_beacon_after = None
            lbl.config(fg=FG, text="Project:")
            try:
                ent.config(highlightthickness=0)
            except Exception:
                pass

    def _project_beacon_pulse(self, bright=True):
        if not getattr(self, "_project_beacon_on", False):
            return
        lbl = getattr(self, "_project_label", None)
        if lbl:
            lbl.config(fg=(RED if bright else "#7a1f1f"), text="Project ⚠:")
        self._project_beacon_after = self.root.after(
            600, lambda: self._project_beacon_pulse(not bright))

    def _archive_transcript(self) -> str:
        """Move the current transcript into transcript/archive/ (recoverable, not deleted)."""
        try:
            day = datetime.datetime.now().strftime("%Y-%m-%d")
            src = TRANSCRIPT_DIR / f"conversation_{day}.md"
            if not src.exists():
                return "nothing to archive"
            arc = TRANSCRIPT_DIR / "archive"
            arc.mkdir(parents=True, exist_ok=True)
            ts  = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            dst = arc / f"conversation_{day}__session_{ts}.md"
            shutil.move(str(src), str(dst))
            return dst.name
        except Exception as e:
            self._log(f"[session] archive error: {e}")
            return f"error: {e}"

    def _toggle_new_session(self):
        """Operator-clicked session refresh (2 steps; auto-detect prompts via the button).
        Step 1: archive the transcript + reset session counters; the operator then starts
                a fresh Copilot chat (resets window geometry, clears leftover triggers).
        Step 2: re-inject SOP + project summary + a continuation note so the fresh window
                resumes with full context. The chat window is disposable; the project
                summary is the durable state."""
        if not self._session_refresh_pending:
            archived = self._archive_transcript()
            self._session_agent1_count    = 0
            self._session_full_flagged    = False
            self._session_refresh_pending = True
            if self._session_btn:
                self._session_btn.config(text="✓ Re-establish", bg=ACCENT, fg="white")
            self._beacon_project(True)   # prompt operator to set the new project name
            self._log(
                f"[session] transcript archived ({archived}). Start a NEW CHAT in Copilot "
                "now, set the Project name, then click ✓ Re-establish.")
            self._set_status("New Session — fresh Copilot chat, set Project name, then ✓ Re-establish")
        else:
            self._session_refresh_pending = False
            self._beacon_project(False)
            if self._session_btn:
                self._session_btn.config(text="↻ New Session", bg=BG2, fg=FG)
            threading.Thread(target=self._session_reestablish, daemon=True).start()
            self._set_status("New Session — re-establishing context in fresh chat…")

    def _session_reestablish(self):
        """Rebuild the fresh chat's context in one inject: SOP + continuation + summary."""
        self._inject_grace["agent1"] = time.time() + 30
        proj = self._project_name_var.get().strip()
        summary = ""
        if self._p1a_summary_file:
            try:
                with open(self._p1a_summary_file, "r", encoding="utf-8") as f:
                    summary = f.read().strip()
            except Exception as e:
                self._log(f"[session] summary read error: {e}")
        cont = ("[SESSION REFRESH — fresh chat"
                + (f" for project '{proj}'" if proj else "") + "]\n"
                "This is a new chat continuing the SAME project. Resume the work from where it "
                "left off and reply ONLY in the routing envelope: To AgentN / content / end message now.")
        msg = AGENT1_SOP + "\n\n" + cont
        if summary:
            msg = msg + "\n\nPROJECT SUMMARY (your durable context):\n\n" + summary
        self._inject_to_agent("agent1", msg, bypass_mode_check=True)
        self._log(f"[session] context re-established in fresh chat "
                  f"(SOP + continuation{' + summary' if summary else ''})")

    # ── A4/A5 model-swap "CD changer" (Phase 1: operator-prompted) ────────────
    # GGUF Chatbox holds one model at a time; A4 (vision) and A5 (writing) each
    # need their own disk. We probe the loaded model before dispatch and, if it's
    # the wrong disk, beacon the operator to swap it (Phase 2 will auto-swap via a
    # GGUF Chatbox backend control port). See CD_* constants for the contract.
    def _cd_required_disk(self, agent_id: str) -> str:
        """Raw disk config for Agent 4 / Agent 5 — a comma-separated *playlist* of
        acceptable model-name tokens (empty = no gating). Any one token matching the
        loaded model counts as the right disk, so a slot isn't pinned to one file."""
        return (self._cd_disk.get(agent_id, "") or "").strip()

    def _cd_tokens(self, agent_id: str) -> list:
        """The playlist split into match tokens (lowercased, blanks dropped).
        Local disk agents (agent5/6/7) with NO explicit token auto-map to their
        magazine slot by number (A5→MODEL 1, A6→MODEL 2, A7→MODEL 3): the slot's
        model filename becomes the token. An explicit token always overrides."""
        toks = [t.strip().lower()
                for t in self._cd_required_disk(agent_id).split(",") if t.strip()]
        if toks:
            return toks
        if agent_id in ("agent5", "agent6", "agent7"):
            idx = int(agent_id[-1]) - 5          # agent5→slot idx 0, 6→1, 7→2
            mag = self._cd_magazine()
            if 0 <= idx < len(mag):
                name = str(mag[idx].get("model_path", "")).replace("\\", "/")
                name = name.rsplit("/", 1)[-1].strip().lower()
                if name:
                    return [name]
        return []

    @staticmethod
    def _cd_parse_models(payload) -> "str | None":
        """Extract the loaded model id from a /v1/models response. Accepts both the
        OpenAI shape {"data":[{"id":...}]} AND the llama.cpp / GGUF-Chatbox shape
        {"models":[{"name":...,"model":...}]} that the live server actually returns
        (where the id is typically the full .gguf file path). Returns None if empty."""
        if not isinstance(payload, dict):
            return None
        entries = payload.get("data") or payload.get("models") or []
        if not entries:
            return None
        first = entries[0] or {}
        return (str(first.get("id") or first.get("model")
                    or first.get("name") or "").strip() or None)

    def _cd_loaded_disk(self, force: bool = False):
        """Probe the GGUF Chatbox proxy for the currently-served model id.
        Returns the id string, or None if unreachable/unknown. Cached for
        CD_PROBE_TTL so per-tick dispatch checks don't hammer the endpoint."""
        now = time.time()
        ts, cached = self._cd_loaded_cache
        if not force and (now - ts) < CD_PROBE_TTL:
            return cached
        loaded = None
        try:
            import urllib.request
            with urllib.request.urlopen(CD_PROXY_MODELS_URL, timeout=3.0) as r:
                payload = json.loads(r.read().decode("utf-8", "replace"))
            loaded = self._cd_parse_models(payload)
        except Exception:
            loaded = None
        self._cd_loaded_cache = (now, loaded)
        return loaded

    def _cd_disk_ready(self, agent_id: str) -> tuple:
        """Decide whether Agent 4 / Agent 5 may be dispatched right now.
        Returns (ready: bool, reason: str). 'ready' is True when no swap is
        needed — disk unconfigured, already loaded, or the proxy can't be read
        (we never block on an unreadable endpoint). 'ready' is False ONLY when we
        positively observe the WRONG disk loaded; then the operator is beaconed to
        swap and the dispatch is deferred until the right disk appears."""
        tokens = self._cd_tokens(agent_id)
        if not tokens:
            return True, "cd: unconfigured"
        loaded = self._cd_loaded_disk()
        if loaded is None:
            return True, "cd: proxy unreadable — proceeding unverified"
        low = loaded.lower()
        match = next((t for t in tokens if t in low), None)
        if match:
            if self._cd_swap_for == agent_id:      # the swap we were waiting on just landed
                self._cd_clear_swap(loaded)
            return True, f"cd: '{match}' loaded"
        # Show the RESOLVED tokens (explicit or slot-fallback), not the raw
        # config field — an empty field displayed "load one of []" even though
        # the slot auto-map had resolved a disk perfectly well.
        want = self._cd_required_disk(agent_id) or ", ".join(tokens)
        self._cd_raise_swap(agent_id, want, loaded)
        return False, f"cd: wrong disk (need one of [{want}], have '{loaded}')"

    def _cd_raise_swap(self, agent_id: str, want: str, loaded: str):
        """Prompt the operator (log + transcript + beacon) to load an acceptable disk.
        `want` is the comma-separated playlist; the beacon shows the preferred (first)
        token to stay slender, the log/transcript show the full set of acceptable tokens."""
        short = {"agent4": "A4", "agent5": "A5",
                 "agent6": "A6", "agent7": "A7"}.get(agent_id, agent_id)
        pref = (want.split(",")[0].strip() or want)
        if self._cd_swap_for != agent_id:
            self._cd_swap_for = agent_id
            self._cd_swap_since = time.time()
            self._log(f"[cd-changer] 🔁 swap disk for {short}: load one of [{want}] in "
                      f"GGUF Chatbox (currently '{loaded}')")
            try:
                self._write_transcript(
                    "soc", agent_id,
                    f"CD CHANGER: load a disk matching [{want}] in GGUF Chatbox (have '{loaded}')",
                    kind="cd-swap")
            except Exception:
                pass
        elif time.time() - self._cd_swap_since > CD_SWAP_TIMEOUT:
            self._log(f"[cd-changer] swap wait exceeded {CD_SWAP_TIMEOUT:.0f}s — "
                      f"still need one of [{want}] for {short}")
            self._cd_swap_since = time.time()   # reset so we nag at most once per interval
        self._cd_update_beacon(f"🔁 load '{pref}' → {short}", RED)

    def _cd_clear_swap(self, loaded: str):
        """The required disk is now loaded — clear the pending-swap state."""
        if self._cd_swap_for:
            self._log(f"[cd-changer] ✓ disk ready: '{loaded}' loaded")
        self._cd_swap_for = None
        self._cd_swap_since = 0.0
        self._cd_update_beacon(f"💿 {loaded}", GREEN)

    # ── Automatic CD change (trigger-then-wait via GGUF Chatbox :8086) ─────────

    def _cd_magazine(self) -> list:
        """The CD-changer magazine (disk registry) from GGUF Chatbox settings.json:
        [{model_path, mmproj_path, label}, ...]. Raw slot order is PRESERVED
        (including empty slots) because agent5/6/7 map to slots by index.
        Read fresh each call — the operator can edit the magazine any time."""
        try:
            data = json.loads(Path(GGUF_SETTINGS_FILE).read_text(encoding="utf-8"))
            return [d if isinstance(d, dict) else {}
                    for d in (data.get("magazine") or [])]
        except Exception:
            return []

    def _cd_disk_paths(self, agent_id: str):
        """Resolve this agent's disk to concrete swap paths: the first magazine
        entry whose model path matches one of the agent's playlist tokens.
        Returns (model_path, mmproj_path | None), or None when unconfigured /
        no magazine match (caller falls back to the operator prompt)."""
        tokens = self._cd_tokens(agent_id)
        if not tokens:
            return None
        for d in self._cd_magazine():
            low = str(d.get("model_path", "")).lower()
            if any(t in low for t in tokens):
                return d.get("model_path"), (d.get("mmproj_path") or None)
        return None

    # ── Adaptive per-model guidance ───────────────────────────────────────────
    def _model_chat_template(self) -> str:
        """Best-effort read of the LOADED model's native chat template from
        llama-server /props (proxy first, backend fallback). Returns '' on any
        failure — the caller treats 'unknown' as a plain model (extra guidance,
        harmless). Read at dispatch time, when the target disk is already loaded."""
        import urllib.request
        for url in (MODEL_PROPS_URL, MODEL_PROPS_URL_BE):
            try:
                with urllib.request.urlopen(url, timeout=3.0) as r:
                    data = json.loads(r.read().decode("utf-8", "replace"))
                tmpl = data.get("chat_template") or (
                    data.get("default_generation_settings") or {}).get("chat_template") or ""
                if tmpl:
                    return tmpl
            except Exception:
                continue
        return ""

    def _model_profile_cached(self, model_path: str) -> dict:
        """Profile the model at model_path (tier + tool_capable), computed once
        per path and cached. The chat-template read is live, so this must be
        called while that disk is the loaded one (i.e. at/after dispatch)."""
        if not model_path:
            return {"name": "", "tier": "weak", "tool_capable": False}
        prof = self._model_profiles_cache.get(model_path)
        if prof is None:
            tmpl = self._model_chat_template()
            prof = _model_profile(model_path, tmpl, self._model_profile_overrides)
            self._model_profiles_cache[model_path] = prof
            self._log(f"[adaptive] {Path(model_path).name} → {prof['tier']} "
                      f"(tool_capable={prof['tool_capable']})")
        return prof

    def _agent_tool_capable(self, agent_id: str) -> bool:
        """Whether this local agent's disk is a tool-trained model — drives the
        adaptive head-guidance. Unknown/unresolved ⇒ False (weak: extra guidance,
        never harmful to a strong model)."""
        disk = self._cd_disk_paths(agent_id)
        model_path = disk[0] if disk else ""
        return self._model_profile_cached(model_path).get("tool_capable", False)

    def _cd_trigger_swap(self, agent_id: str) -> bool:
        """POST the agent's disk to the swap endpoint. Server-side idempotent
        (re-requesting the loaded disk does NOT restart it), so repeats are safe.
        False when the disk can't be resolved or the endpoint is unreachable."""
        disk = self._cd_disk_paths(agent_id)
        if not disk:
            return False
        model_path, mmproj = disk
        body = {"model_path": model_path}
        if mmproj:
            body["mmproj_path"] = mmproj
        try:
            import urllib.request
            req = urllib.request.Request(
                CD_SWAP_URL, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10.0) as r:
                resp = json.loads(r.read().decode("utf-8", "replace"))
            return bool(resp.get("ok"))
        except Exception as e:
            self._log(f"[cd-changer] swap endpoint unreachable "
                      f"({e.__class__.__name__}) — operator prompt stands")
            return False

    def _cd_chat_clear(self) -> bool:
        """Remote New-Chat (hop hygiene): POST the chatbox's :8086/chat/clear so
        the shared chat window starts clean before the next agent's dispatch —
        the routed envelope carries the content; the previous disk's turns must
        not bleed into the new agent's context. Best-effort: False = endpoint
        down/old chatbox build (dispatch proceeds, window just keeps history)."""
        try:
            import urllib.request
            req = urllib.request.Request(CD_CHAT_CLEAR_URL, data=b"", method="POST")
            with urllib.request.urlopen(req, timeout=5.0) as r:
                resp = json.loads(r.read().decode("utf-8", "replace"))
            return bool(resp.get("ok"))
        except Exception as e:
            self._log(f"[cd-changer] chat clear unavailable "
                      f"({e.__class__.__name__}) — window keeps prior turns")
            return False

    def _cd_release_shared_hold(self, source_agent) -> None:
        """A reply just arrived FROM the shared local window (A5/A6/A7 all render
        into the one GGUF Chatbox window, OCR'd as canonical 'agent5') and is being
        routed ONWARD to a different disk — which parks + swaps. The disk that
        produced this reply has therefore ANSWERED, so the shared-window hold must
        release now. Otherwise it stays set to 'agent5' (every local dispatch injects
        as agent5) and the swapped-in disk's redispatch — also _try_route('agent5')
        — is blocked by the stale hold ('⏸ holding — waiting for agent5 reply'),
        stalling the 2nd local hop until the 180s timeout and dropping the parked
        message. This is the local-relay killer: A1→A5 flowed, A5→A6 never landed.
        The normal hold-release (in _try_route) can't fire here because the local→
        local hop PARKS instead of delivering, bypassing _try_route entirely."""
        if source_agent == "agent5" and self._waiting_reply == "agent5":
            self._log("[cd-changer] shared-window reply received — "
                      "releasing agent5 hold so the next disk can be dispatched")
            self._waiting_reply     = None
            self._waiting_body_hash = None
            self.root.after(0, self._update_ocr_hold_label)

    def _cd_disk_file_ok(self, agent_id: str) -> bool:
        """False (and beacon) if this agent's disk or mmproj path points at a
        moved/deleted file. The chatbox swap KILLS the running model before
        loading the new one, so a missing target leaves the server DEAD and the
        watcher polling for the full timeout (observed live 2026-07-15: A6 at a
        deleted gemma → whole relay stalled + chatbox crashed). An unresolved disk
        returns True here — that path is _cd_trigger_swap's own responsibility."""
        disk = self._cd_disk_paths(agent_id)
        if not disk:
            return True
        model_path, mmproj = disk
        short = {"agent4": "A4", "agent5": "A5",
                 "agent6": "A6", "agent7": "A7"}.get(agent_id, agent_id)
        for kind, p in (("disk", model_path), ("mmproj", mmproj)):
            if p and not os.path.exists(p):
                self._log(f"[cd-changer] ✗ {short} {kind} file MISSING — "
                          f"refusing swap (would kill the server): {p}")
                self._cd_update_beacon(f"💿 {short} {kind} MISSING", RED)
                return False
        return True

    def _cd_auto_swap(self, agent_id: str, body: str, source_agent) -> bool:
        """Wrong disk observed for a dispatch: trigger the swap, PARK the message,
        and let a watcher redispatch it once the disk is up (the graceful wait —
        the message is never dropped while the CD changes). Returns True when the
        swap was triggered; False = auto-swap unavailable (magazine unset /
        endpoint down / disk file missing), leaving the operator-prompt behavior
        in charge."""
        if not self._cd_disk_file_ok(agent_id):
            return False
        if not self._cd_trigger_swap(agent_id):
            return False
        digit = agent_id[-1]
        envelope = f"To Agent{digit}\n{body}\nend message now"
        with self._cd_park_lock:
            self._cd_parked.setdefault(agent_id, []).append((envelope, source_agent))
            already_watching = agent_id in self._cd_watchers
            if not already_watching:
                self._cd_watchers.add(agent_id)
        if not already_watching:
            threading.Thread(target=self._cd_swap_watcher,
                             args=(agent_id,), daemon=True).start()
        return True

    def _cd_swap_watcher(self, agent_id: str):
        """Graceful wait: poll the loaded disk until this agent's disk is up, then
        replay the parked message(s) through _route_text — which re-runs the full
        dispatch gating (disk check now passes, GPU lock, plugin checks)."""
        short = {"agent4": "A4", "agent5": "A5",
                 "agent6": "A6", "agent7": "A7"}.get(agent_id, agent_id)
        tokens = self._cd_tokens(agent_id)
        deadline = time.time() + CD_SWAP_LOAD_TIMEOUT
        ready = False
        while time.time() < deadline:
            loaded = self._cd_loaded_disk(force=True)
            if loaded and any(t in loaded.lower() for t in tokens):
                ready = True
                break
            self._cd_update_beacon(f"💿 swapping → {short}…", YELLOW)
            time.sleep(CD_SWAP_POLL_INTERVAL)
        with self._cd_park_lock:
            parked = self._cd_parked.pop(agent_id, [])
            self._cd_watchers.discard(agent_id)
        if not ready:
            self._log(f"[cd-changer] ✗ {short} disk not up after "
                      f"{CD_SWAP_LOAD_TIMEOUT:.0f}s — {len(parked)} message(s) dropped")
            self._cd_update_beacon(f"💿 swap {short} FAILED", RED)
            return
        self._cd_clear_swap(self._cd_loaded_disk() or "?")
        # Hop-hygiene window-wipe RETIRED (2026-07-14): the GGUF Chatbox now keeps
        # a separate persistent conversation LAYER per magazine slot (cd1/cd2/cd3),
        # so each local agent has its own context — the previous agent's turns can
        # no longer bleed into the next, and there is nothing to wipe. Clearing
        # here would instead destroy the agent's OWN accumulated context, and it
        # used to eat a reply that landed right on the clear boundary (blank
        # window). The chatbox switches to the loaded disk's layer automatically
        # on the next send. (_cd_chat_clear is kept for manual/remote New-Chat.)
        self._log(f"[cd-changer] ✓ CD changed — redispatching "
                  f"{len(parked)} parked message(s) to {short}")
        # Local hemisphere agents (A5/6/7) ALL inject into the canonical agent5
        # window (_try_route("agent5", …)). If we redispatched with the recorded
        # window source — also "agent5" (the reply was OCR'd from that window) —
        # the directional self-route guard would DROP the delivery ("agent5 seen
        # in its own window") and the next disk would never receive the message.
        # Redispatch these as a system source so the guard passes. A4 (HTTP, its
        # own window) keeps its real source so its mission banner shows the origin.
        _local_target = agent_id in ("agent5", "agent6", "agent7")
        for envelope, src in parked:
            try:
                redispatch_src = "cd_changer" if _local_target else (src or "cd_changer")
                self._route_text(envelope, redispatch_src)
            except Exception as e:
                self._log(f"[cd-changer] redispatch error: {e}")

    def _cd_update_beacon(self, text: str, color: str):
        """Update the CD-changer status label (thread-safe via root.after)."""
        lbl = getattr(self, "_cd_status_lbl", None)
        if lbl is None:
            return
        def _do():
            try:
                lbl.config(text=text[:36], fg=color)
            except Exception:
                pass
        try:
            self.root.after(0, _do)
        except Exception:
            pass

    def _on_cd_disk_change(self, agent_id: str):
        """Persist an edited disk name and refresh state."""
        var = self._cd_disk_var.get(agent_id)
        if var is not None:
            self._cd_disk[agent_id] = var.get().strip()
        self._cd_loaded_cache = (0.0, None)   # force a fresh probe on next check
        self._model_profiles_cache.clear()    # re-profile disks after a config change
        self._save_config()

    # ── Unified local-GPU inference lock (A4 vision + A5 writing) ─────────────
    def _gpu_try_acquire(self, aid: str) -> bool:
        """Atomically claim the single local-GPU inference slot for A4/A5 so they
        can never infer at the same instant (they share the GPU). Returns True if
        acquired (or already held by aid), False if the other local agent holds it.
        A lock held past GPU_LOCK_TIMEOUT is force-reclaimed so it can't deadlock."""
        with self._gpu_lock:
            now = time.time()
            h = self._gpu_holder
            if h and h != aid and (now - self._gpu_since) > GPU_LOCK_TIMEOUT:
                self._log(f"[gpu-lock] force-releasing stale slot held by {h} "
                          f"({now - self._gpu_since:.0f}s)")
                h = self._gpu_holder = None
            if h is None:
                self._gpu_holder = aid
                self._gpu_since = now
                self._gpu_seen_active = False
                return True
            return h == aid          # re-entrant if already ours

    def _gpu_release(self, aid: str):
        """Free the local-GPU slot if aid holds it."""
        with self._gpu_lock:
            if self._gpu_holder == aid:
                self._gpu_holder = None
                self._gpu_since = 0.0
                self._gpu_seen_active = False

    def _gpu_monitor_tick(self):
        """Release the slot once its holder has finished inferring, using each
        agent's existing busy signal (A4 = agent4_window._busy, A5 = we're still
        waiting on its reply). Waits to SEE the holder become active first so it
        can't release in the gap between acquire and dispatch; falls back to
        GPU_ACQUIRE_GRACE if a dispatch never starts (never deadlocks)."""
        try:
            h = self._gpu_holder
            if h:
                if h == "agent4":
                    win = getattr(getattr(self, "_vplugin", None), "agent4_window", None)
                    active = bool(getattr(win, "_busy", False))
                elif h == "agent5":
                    active = (self._waiting_reply == "agent5")
                else:
                    active = False
                if active:
                    self._gpu_seen_active = True
                elif self._gpu_seen_active:
                    self._gpu_release(h)                         # finished → free slot
                elif (time.time() - self._gpu_since) > GPU_ACQUIRE_GRACE:
                    self._log(f"[gpu-lock] {h} never became active in "
                              f"{GPU_ACQUIRE_GRACE:.0f}s — releasing slot")
                    self._gpu_release(h)
        except Exception:
            pass
        self.root.after(500, self._gpu_monitor_tick)

    def _launch_phase2a(self):
        """Open Phase 2a security audit dialog — collects stack notes, assembles
        the security audit SOP with project context, and writes it to
        staging/phase2a_security_audit.md for the user to drag into Claude."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Phase 2a — Security Audit")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.grab_set()

        tk.Label(dlg, text="Phase 2a: Security Audit",
                 bg=BG, fg="#4ec9b0", font=("Segoe UI", 11, "bold"),
                 pady=8).pack(fill="x", padx=16)

        tk.Label(dlg,
                 text="⚠  Use a SEPARATE VS Code instance for this session.\n"
                      "Do NOT use the Agent 2 window — SOC watches it.",
                 bg="#1a2a1a", fg="#4ec9b0",
                 font=("Segoe UI", 8, "bold"), justify="left",
                 wraplength=360, pady=6, padx=8).pack(fill="x", padx=16, pady=(0, 6))

        tk.Label(dlg,
                 text="Optional: note the tech stack or any areas of concern.\n"
                      "Leave blank to run a full general audit.",
                 bg=BG, fg=FG, font=("Segoe UI", 8), justify="left",
                 wraplength=360).pack(anchor="w", padx=16)

        txt = tk.Text(dlg, width=48, height=6,
                      bg=BG2, fg=FG, insertbackground=FG,
                      font=("Consolas", 9), relief="flat",
                      padx=6, pady=6, wrap="word")
        txt.pack(fill="both", padx=16, pady=(6, 0))
        txt.insert("1.0", "Stack: \nAreas of concern: ")
        txt.focus_set()

        status_lbl = tk.Label(dlg, text="", bg=BG, fg=GREEN,
                              font=("Segoe UI", 8, "italic"))
        status_lbl.pack(padx=16, pady=(4, 0))

        def _prepare():
            stack_notes = txt.get("1.0", "end").strip()
            workspace = os.path.dirname(os.path.abspath(__file__))
            project   = self._project_name_var.get().strip() or "(unnamed)"
            try:
                import subprocess
                git_log = subprocess.check_output(
                    ["git", "-C", workspace, "log", "--oneline", "-20"],
                    stderr=subprocess.DEVNULL, text=True).strip()
            except Exception:
                git_log = "(git log unavailable)"

            sop = PHASE2A_SOP_TEMPLATE.format(
                workspace=workspace,
                project=project,
                git_log=git_log,
                stack=stack_notes or "(not specified — run general audit)")
            outbox_path = self._agent3_outbox_var.get().strip()
            if outbox_path:
                sop += AGENT3_OUTBOX_PROTOCOL.format(outbox_path=outbox_path)

            soc_dir     = os.path.dirname(os.path.abspath(__file__))
            staging_dir = os.path.join(soc_dir, "staging")
            os.makedirs(staging_dir, exist_ok=True)
            sop_path = os.path.join(staging_dir, "phase2a_security_audit.md")
            try:
                with open(sop_path, "w", encoding="utf-8") as f:
                    f.write(sop)
            except Exception as e:
                status_lbl.config(text=f"Error writing file: {e}", fg=RED)
                return

            try:
                import subprocess
                subprocess.Popen(["code", sop_path], shell=True)
            except Exception:
                pass

            short = sop_path.replace(os.path.expanduser("~"), "~")
            status_lbl.config(
                text=f"Saved: {short}\n"
                     "Open a NEW VS Code window (not Agent 2's).\n"
                     "Drag this file into Claude's chat to begin the audit.",
                fg=GREEN)
            self._log(f"[phase2a] security audit SOP written -> {sop_path}")

        btn_row = tk.Frame(dlg, bg=BG)
        btn_row.pack(fill="x", padx=16, pady=(8, 12))
        tk.Button(
            btn_row, text="Prepare Audit File",
            command=_prepare,
            bg="#1a2a3a", fg="#4ec9b0",
            font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2",
            padx=12, pady=5
        ).pack(side="left")
        tk.Button(
            btn_row, text="Close",
            command=dlg.destroy,
            bg=BG2, fg=FG,
            font=("Segoe UI", 8),
            relief="flat", cursor="hand2",
            padx=10, pady=5
        ).pack(side="right")

    def _launch_phase3(self):
        """Open Phase 3 debug dialog — collects user's issue list, assembles the
        debug SOP with live project context, and writes it to phase3_debug_sop.md
        in the workspace so the user can drag it into Claude's chat."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Phase 3 — Debug")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.grab_set()

        tk.Label(dlg, text="Phase 3: Debugging Agent",
                 bg=BG, fg="#c586c0", font=("Segoe UI", 11, "bold"),
                 pady=8).pack(fill="x", padx=16)

        tk.Label(dlg,
                 text="⚠  Use a SEPARATE VS Code instance for this session.\n"
                      "Do NOT use the Agent 2 window — SOC watches it and\n"
                      "will try to route Claude's replies as agent messages.",
                 bg="#3a1a1a", fg="#f48771",
                 font=("Segoe UI", 8, "bold"), justify="left",
                 wraplength=360, pady=6, padx=8).pack(fill="x", padx=16, pady=(0, 6))

        tk.Label(dlg,
                 text="Describe what isn't working. List each issue separately\n"
                      "so Claude can tackle them one at a time.",
                 bg=BG, fg=FG, font=("Segoe UI", 8), justify="left",
                 wraplength=360).pack(anchor="w", padx=16)

        txt = tk.Text(dlg, width=48, height=10,
                      bg=BG2, fg=FG, insertbackground=FG,
                      font=("Consolas", 9), relief="flat",
                      padx=6, pady=6, wrap="word")
        txt.pack(fill="both", padx=16, pady=(6, 0))
        txt.insert("1.0",
                   "1. \n"
                   "2. \n"
                   "3. \n")
        txt.focus_set()

        status_lbl = tk.Label(dlg, text="", bg=BG, fg=GREEN,
                              font=("Segoe UI", 8, "italic"))
        status_lbl.pack(padx=16, pady=(4, 0))

        def _prepare():
            user_report = txt.get("1.0", "end").strip()
            if not user_report or user_report in ("1. \n2. \n3.", "1. \n2. \n3. "):
                status_lbl.config(text="Please describe what isn't working first.", fg=ORANGE)
                return

            # Gather live project context
            workspace = os.path.dirname(os.path.abspath(__file__))
            project   = self._project_name_var.get().strip() or "(unnamed)"
            try:
                import subprocess
                git_log = subprocess.check_output(
                    ["git", "-C", workspace, "log", "--oneline", "-12"],
                    stderr=subprocess.DEVNULL, text=True).strip()
            except Exception:
                git_log = "(git log unavailable)"

            sop = PHASE3_SOP_TEMPLATE.format(
                workspace=workspace,
                project=project,
                git_log=git_log,
                user_report=user_report)
            # Phase 3 is free-form human↔Agent3 debugging — no outbox routing.
            # Agent3 communicates directly with the user and uses pc.py tools.

            # Write to staging/ inside the SOC Ultralight source folder so it is
            # included in source backups, but naturally quarantined — agents only
            # see content injected into their chat windows, never files in folders.
            soc_dir     = os.path.dirname(os.path.abspath(__file__))
            staging_dir = os.path.join(soc_dir, "staging")
            os.makedirs(staging_dir, exist_ok=True)
            sop_path = os.path.join(staging_dir, "phase3_debug_sop.md")
            try:
                with open(sop_path, "w", encoding="utf-8") as f:
                    f.write(sop)
            except Exception as e:
                status_lbl.config(text=f"Error writing file: {e}", fg=RED)
                return

            # Open in VS Code so user can drag it into Claude's chat
            try:
                import subprocess
                subprocess.Popen(["code", sop_path], shell=True)
            except Exception:
                pass

            short = sop_path.replace(os.path.expanduser("~"), "~")
            status_lbl.config(
                text=f"Saved: {short}\n"
                     "Open a NEW VS Code window (not Agent 2's).\n"
                     "Drag this file into Claude's chat there to begin.",
                fg=GREEN)
            self._log(f"[phase3] debug SOP written → {sop_path}")

        btn_row = tk.Frame(dlg, bg=BG)
        btn_row.pack(fill="x", padx=16, pady=(8, 12))
        tk.Button(
            btn_row, text="Prepare Debug File",
            command=_prepare,
            bg="#3a2a4a", fg="#c586c0",
            font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2",
            padx=12, pady=5
        ).pack(side="left")
        tk.Button(
            btn_row, text="Close",
            command=dlg.destroy,
            bg=BG2, fg=FG,
            font=("Segoe UI", 8),
            relief="flat", cursor="hand2",
            padx=10, pady=5
        ).pack(side="right")

    def _log_scroll_top(self):
        """[Home] — jump the diagnostics log to the first entry."""
        if not self._log_open:
            self._toggle_log()   # auto-open so user can see the top
        def _do():
            self.log.config(state="normal")
            self.log.see("1.0")
            self.log.config(state="disabled")
        self.root.after(0, _do)


# ── Single-instance lock ──────────────────────────────────────────────────────

def _acquire_instance_lock() -> bool:
    return PLATFORM.acquire_instance_lock("SOCUltralight_v1")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Hide any console window — covers python.exe launches and stray library consoles
    PLATFORM.hide_own_console()

    # Declare a distinct App ID BEFORE any window is created so Windows gives
    # SOC's taskbar windows (e.g. the Agent 4 · Vision window) their OWN identity
    # and honour each window's iconbitmap, instead of falling back to the shared
    # pythonw taskbar icon. Best-effort; never fatal (no-op on Linux — identity
    # comes from the .desktop file there).
    PLATFORM.set_app_id("Baxters.SOC.Ultralight")

    if not _acquire_instance_lock():
        try:
            _r = tk.Tk()
            _r.withdraw()
            messagebox.showerror("SOC Ultralight", "SOC Ultralight is already running.")
        except Exception:
            pass          # no display available — the exit code is the signal
        sys.exit(1)

    try:
        root = tk.Tk()
        app = SOCUltralight(root)
        root.mainloop()
    except Exception:
        # Launched without a console (pyw), so an uncaught startup exception
        # would vanish silently. Persist it and surface a visible box.
        import traceback
        _tb = traceback.format_exc()
        try:
            (BASE_DIR / "crash.log").write_text(_tb, encoding="utf-8")
        except Exception:
            pass
        try:
            _r = tk.Tk()
            _r.withdraw()
            messagebox.showerror("SOC Ultralight — startup error", _tb[-1500:])
        except Exception:
            pass
        sys.exit(1)
    sys.exit(0)
