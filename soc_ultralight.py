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
from collections import OrderedDict
from pathlib import Path
from datetime import datetime

import pyperclip
import pyautogui
import mss
_mss_ctor = getattr(mss, 'MSS', None) or getattr(mss, 'mss', None)
from PIL import Image, ImageTk, ImageGrab, ImageEnhance, ImageFilter, ImageOps, ImageStat
import pytesseract

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
SCAN_NORMAL      = 1.5    # seconds between OCR scans (idle)
SCAN_RAPID       = 0.3    # seconds between scans in rapid mode
RAPID_DURATION   = 8.0    # seconds to stay rapid after "to agent" spotted
TRIGGER_PERSIST_SECS     = 30.0   # seconds a seen trigger stays remembered after scrolling off
SCROLL_ACCUM_TIMEOUT     = 45.0   # give up scroll-accumulation after this many seconds
SCROLL_ACCUM_MIN_INTERVAL = 0.8   # minimum seconds between accumulation scroll steps
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
REMINDER_EVERY        = 5    # fallback for agent3 / legacy
TEMPLATE_CAPTURE = 60     # px square crop saved when hover-capturing a target

BASE_DIR      = Path(__file__).parent
OUTBOX_DIR    = BASE_DIR / "outbox"
SENT_DIR      = BASE_DIR / "sent"
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
AUTOCLICK_LOCKED = ("input_field", "send_message", "_scroll", "agent1_send", "agent2_send")

for _d in [OUTBOX_DIR / "agent1", OUTBOX_DIR / "agent2", OUTBOX_DIR / "agent3",
           SENT_DIR   / "agent1", SENT_DIR   / "agent2", SENT_DIR   / "agent3",
           TEMPLATE_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

pyautogui.FAILSAFE = True
pyautogui.PAUSE    = 0.1

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
    "\n"
    "FORMAT — copy exactly:\n"
    "To Agent2\n"
    "[block content]\n"
    "end message now\n"
    "\n"
    "Receive confirmation → send next block. That is your entire role."
)

GROUND_RULES_AGENT2 = (
    "[PROTOCOL RESET — AGENT 2]\n"
    "You are Agent 2. You are NOT a conversational assistant.\n"
    "You do NOT ask questions. You do NOT say 'acknowledged'. "
    "You do NOT offer options or add commentary.\n"
    "\n"
    "THE ONLY PERMITTED RESPONSES ARE:\n"
    "\n"
    "After saving a block:\n"
    "  To Agent1\n"
    "  module block BLOCK_ID saved, ready for next block\n"
    "  [close with: end message now]\n"
    "\n"
    "If you have a blocker:\n"
    "  To Agent1\n"
    "  PROBLEM: <one sentence>\n"
    "  QUESTION: <what you need>\n"
    "  [close with: end message now]\n"
    "\n"
    "When implementation is complete:\n"
    "  To Agent1\n"
    "  implementation of instruction blocks is complete\n"
    "  [close with: end message now]\n"
    "\n"
    "Nothing else. No other output is permitted."
)


GROUND_RULES_VSCODE_BRIEF = (
    "[SOC] Agent 3 — Senior Coding Advisor.\n"
    "Plan the next step, write an instruction block, send to Agent 2:\n"
    "  To Agent2\n"
    "  Step 1: <specific instruction>\n"
    "  end message now\n"
    "Wait for Agent 2 reply before sending the next block."
)

# Startup briefing written to outbox/agent3/ when VS Code mode activates.
GROUND_RULES_VSCODE_AGENT3 = GROUND_RULES_VSCODE_BRIEF


#   "message body"
#   end message now
# OCR commonly garbles digits: "1"→l/i/I/!/|, "2"→z/Z, "3"→B/8
# Single-char garble map
_OCR_DIGIT_NORM: dict[str, str] = {
    "l": "1", "i": "1", "I": "1", "!": "1", "|": "1", "t": "1",
    "z": "2", "Z": "2",
    "B": "3", "8": "3",
}
_D = r"[123liI!|t]"  # digit-or-garble character class

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
    rf"(?i)to\s+agent\s*({_D})\s*[\r\n]+"  # header line
    r"(.*?)"                                 # message body (any lines)
    r"[\r\n]+\s*end\s+message\s+now",       # sentinel
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
IMPL_TRIGGER_PHRASES = (
    "that is the final block you may begin implementation in alphanumeric order now",
    "this is the final block. agent2 may begin implementation",
)
IMPL_TRIGGER_PHRASE  = IMPL_TRIGGER_PHRASES[0]   # kept for legacy references

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
IMPL_ATTEMPT_RE = re.compile(
    r"\b(begin\s+implementation|start\s+implementing|implement\s+now"
    r"|execute\s+this\s+now|now\s+implement)\b", re.IGNORECASE)

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


class AgentConfig:
    __slots__ = ("hwnd", "title", "input_xy", "send_xy",
                 "scroll_dn_xy", "scroll_up_xy", "ocr_region",
                 "lbl_window", "lbl_input", "lbl_send", "lbl_scroll", "lbl_region",
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
        }

        self._ocr_running = False
        self._ocr_thread  = None
        self._rapid_until = 0.0          # epoch time: stay rapid until this
        self._waiting_reply: str | None = None   # agent we just sent to; hold until they reply
        self._waiting_since: float      = 0.0    # epoch time the hold started
        self._last_hold_log: float      = 0.0    # throttle hold log to once per 30s
        self._last_heartbeat_log: float = 0.0   # throttle heartbeat-suppressed log

        self._fw_running  = False
        self._fw_thread   = None
        self._vscode_mode = False   # Copilot+Claude Code mode (outbox + auto-click)
        self._bing_mode   = False   # Agent 1 Edge-browser-aware mode

        self._seen_hashes: OrderedDict[str, None] = OrderedDict()
        self._dedup_lock        = threading.Lock()    # guards _seen_hashes
        self._waiting_body_hash: str | None = None    # hash to clear when hold times out
        self._last_scroll:       dict[str, float] = {}   # agent_id → last auto-scroll time
        self._scroll_grace:      dict[str, float] = {}   # agent_id → keep scrolling until this time
        self._last_routed_body:  dict[str, str]  = {}   # agent_id → hash of last body routed to them
        self._last_routed_text:  dict[str, str]  = {}   # agent_id → first line of last body (welfare check)
        self._last_route_time:   float = time.time()    # when last successful route happened
        self._welfare_fired:     bool  = False          # True after auto-welfare fires; reset on next successful route
        self._region_frame:      dict[str, str]   = {} # agent_id → pixel-hash of last captured frame
        self._region_last_change:dict[str, float] = {} # agent_id → when region pixels last changed
        self._last_ocr_text:     dict[str, str]   = {} # agent_id → md5 of last OCR text processed
        self._inject_grace:      dict[str, float] = {} # agent_id → epoch until OCR routing suppressed
        self._pending_trigger:   dict[str, tuple | None] = {}  # agent_id → (dest_agent, expiry) | None
        self._scroll_accum:      dict[str, str]   = {}  # agent_id → accumulated OCR text across frames
        self._scroll_accum_active: dict[str, bool] = {} # agent_id → True while accumulating
        self._scroll_accum_since:  dict[str, float] = {} # agent_id → epoch when accumulation started
        self._manual_hold:       dict[str, bool] = {"agent1": False, "agent2": False, "agent3": False}
        self._bypass_agent3:     bool = True   # when True, agent3 is ignored entirely
        self._attendance:        dict[str, bool] = {"agent1": False, "agent2": False, "agent3": False}
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

        self._project_name_var = tk.StringVar()  # active project name — prepended to every Agent 1 message

        # ── Mode + anti-drift state ───────────────────────────────────────────
        self._mode                    = "module_block"  # "module_block" | "implementation"
        self._agent1_inbound_count    = 0   # messages delivered to agent1
        self._consecutive_saved_count = 0   # consecutive "Block X saved" messages
        self._agent2_impl_attempts    = 0   # impl attempt intercepts in module_block mode
        self._agent2_hold             = False  # runaway HOLD state

        self._build_window()
        self._build_ui()
        self._update_mode_indicator()              # sync mode bar to initial state
        self._load_config()                        # restore saved coords
        self.root.after(100, self._fit_window)     # shrink window to content height
        self.root.after(1800, self._startup_calibrate)  # auto-match templates

    # ── Window ────────────────────────────────────────────────────────────────

    def _quit(self):
        self.root.quit()
        self.root.destroy()

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
        self._p1_progress_lbl.pack(side="left", padx=8)
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

        cal_row = tk.Frame(p, bg=BG, pady=2)
        cal_row.pack(fill="x", padx=12)
        tk.Button(
            cal_row, text="⌖ Auto-Calibrate", command=self._auto_calibrate,
            bg=BG2, fg=YELLOW, font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2", padx=10, pady=4
        ).pack(side="left")
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
        for _aid, _short in [("agent1", "A1"), ("agent2", "A2"), ("agent3", "A3")]:
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
        self._jumpin_btn.pack(fill="x", padx=12, pady=(0, 8))

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
            "I want to describe a software project I'm planning to build. "
            "Please help me think through the scope, features, technical stack, "
            "and any constraints, then produce a complete project summary when we "
            "are satisfied with the design. I'll tell you when to write the summary."
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
            "Here is the project summary. Read it fully before responding. "
            "Confirm you understand the scope, then ask me any clarifying questions "
            "before we proceed to decomposition.\n\n"
            f"{content}"
        )
        threading.Thread(
            target=lambda: self._inject_to_agent("agent1", msg),
            daemon=True).start()
        self._p1a_sum_ready_btn.config(fg=FG)
        self._log(f"[priming] project summary injected to Agent 1 ({len(content)} chars)")

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
            "before sending the next one. When all blocks are delivered, send:\n\n"
            "To Agent2\nthat is the final block you may begin implementation "
            "in alphanumeric order now\nend message now\n\n"
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
        else:
            self._p1a_advance_btn.config(state="disabled", fg="#666666",
                                         bg=BG2, activebackground=BG2)

    def _build_phase2_ui(self):
        p = self._p2_frame

        tk.Label(p, text='Protocol:  To agentX  →  body  →  end message now',
                 bg=BG2, fg=YELLOW, font=("Consolas", 7), anchor="w", pady=3,
                 wraplength=244).pack(fill="x")

        mode_row = tk.Frame(p, bg=BG2, pady=3)
        mode_row.pack(fill="x", padx=10, pady=(4, 0))
        self._mode_dot = tk.Label(mode_row, text="●",
                                   font=("Segoe UI", 10, "bold"), bg=BG2, fg=ACCENT)
        self._mode_dot.pack(side="left", padx=(4, 0))
        self._mode_lbl = tk.Label(mode_row, text="MODULE BLOCK MODE",
                                   font=("Segoe UI", 8, "bold"), bg=BG2, fg=ACCENT)
        self._mode_lbl.pack(side="left", padx=(4, 0))
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

        welfare_row = tk.Frame(p, bg=BG, pady=2)
        welfare_row.pack(fill="x", padx=12)
        tk.Button(
            welfare_row, text="⟳  Where Am I  —  Welfare Check",
            command=self._welfare_check,
            bg=BG2, fg=ORANGE, font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2", pady=4, anchor="center"
        ).pack(fill="x")

        ctrl2 = tk.Frame(p, bg=BG, pady=2)
        ctrl2.pack(fill="x", padx=12)
        self.fw_btn = tk.Button(
            ctrl2, text="▶ Outbox", command=self._toggle_file_watcher,
            bg=BG2, fg=ACCENT, font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2", padx=8, pady=4)
        self.fw_btn.pack(side="left")
        self.vscode_btn = tk.Button(
            ctrl2, text="⚡ VS Code", command=self._toggle_vscode_mode,
            bg=BG2, fg=GREEN, font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2", padx=8, pady=4)
        self.vscode_btn.pack(side="left", padx=(4, 0))
        self.bing_btn = tk.Button(
            ctrl2, text="🔵 Bing", command=self._toggle_bing_mode,
            bg=BG2, fg=ACCENT, font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2", padx=8, pady=4)
        self.bing_btn.pack(side="left", padx=(4, 0))
        self._vdd_btn = tk.Button(
            ctrl2, text="🖥 VDesk",
            command=self._toggle_virtual_desktop,
            bg=BG2, fg="#888888", font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2", padx=8, pady=4)
        self._vdd_btn.pack(side="left", padx=(4, 0))
        self.clicks_lbl = tk.Label(ctrl2, text="sends: 0",
                                    bg=BG, fg=YELLOW, font=("Segoe UI", 8))
        self.clicks_lbl.pack(side="right")

        proj_row = tk.Frame(p, bg=BG)
        proj_row.pack(fill="x", padx=12, pady=(4, 2))
        tk.Label(proj_row, text="Project:", bg=BG, fg=FG,
                 font=("Segoe UI", 8)).pack(side="left")
        self.project_entry = tk.Entry(
            proj_row, textvariable=self._project_name_var,
            bg=BG2, fg=ACCENT, insertbackground=FG,
            relief="flat", font=("Segoe UI", 9))
        self.project_entry.pack(side="left", fill="x", expand=True, padx=(6, 4))
        self.project_entry.bind("<FocusOut>", lambda _: self._save_config())
        self.project_entry.bind("<Return>", lambda _: self.project_entry.master.focus_set())

        self._build_autoclick_panel(p)

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
        required = ["agent1", "agent2"] if self._bypass_agent3 else ["agent1", "agent2", "agent3"]
        return all(
            self.agents[aid].hwnd and self.agents[aid].input_xy and self.agents[aid].send_xy
            for aid in required)

    def _phase1_complete(self) -> bool:
        """Full Phase 1 gate — calibration + roll call attendance. Used by Launch button."""
        return (self._calibration_complete() and
                all(self._attendance.get(aid, False)
                    for aid in (["agent1", "agent2"] if self._bypass_agent3
                                else ["agent1", "agent2", "agent3"])))

    def _check_phase1_complete(self):
        if not hasattr(self, "_launch_btn"):
            return
        required = ["agent1", "agent2"] if self._bypass_agent3 else ["agent1", "agent2", "agent3"]
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
        required = ["agent1", "agent2"] if self._bypass_agent3 else ["agent1", "agent2", "agent3"]
        # Reset flags and update dots
        for aid in ("agent1", "agent2", "agent3"):
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
        nums = {"agent1": "1", "agent2": "2", "agent3": "3"}
        def _send_all():
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
            # Give agents time to respond, then start watching their windows
            time.sleep(4)
            self._roll_call_watch(targets)

        threading.Thread(target=_send_all, daemon=True).start()

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
        required = ["agent1", "agent2"] if self._bypass_agent3 else ["agent1", "agent2", "agent3"]
        names = {"agent1": "A1", "agent2": "A2", "agent3": "A3"}
        for aid, lbl in self._attendance_lbls.items():
            present = self._attendance.get(aid, False)
            # Show A3 dot dimmed when bypassed
            if aid == "agent3" and self._bypass_agent3:
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
        # Use Windows virtual-screen metrics so the overlay covers every
        # monitor (including virtual displays).
        _u32 = ctypes.windll.user32
        vx = _u32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN — leftmost x
        vy = _u32.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN — topmost y
        vw = _u32.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN — total width
        vh = _u32.GetSystemMetrics(79)   # SM_CYVIRTUALSCREEN — total height

        overlay = tk.Toplevel(self.root)
        overlay.geometry(f"{vw}x{vh}+{vx}+{vy}")
        overlay.overrideredirect(True)
        overlay.attributes("-topmost", True)
        overlay.attributes("-alpha", 0.45)
        overlay.configure(bg="#050510")

        canvas = tk.Canvas(overlay, bg="#050510",
                           highlightthickness=0, cursor="crosshair")
        canvas.pack(fill="both", expand=True)

        label_name = ("Bing chat"       if agent_id == "agent1"
                      else "Claude Code" if agent_id == "agent3"
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
            is_locked = any(p in stem.lower() for p in AUTOCLICK_LOCKED)
            if is_locked:
                tk.Label(row, text="🔒 routing", bg=BG, fg=BG2,
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
        try:
            import win32api
        except ImportError:
            self._log("[train] pywin32 not found — cannot detect mouse click")
            self._training_stem = None
            self.root.after(0, self.root.deiconify)
            return

        time.sleep(0.5)   # wait for SOC window to finish minimising

        # Wait for any lingering mouse-down from clicking the Train button to clear
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if not (win32api.GetAsyncKeyState(0x01) & 0x8000):
                break
            time.sleep(0.02)

        # Now wait for the user's deliberate click
        deadline = time.time() + TRAIN_TIMEOUT
        click_pos = None
        while time.time() < deadline:
            if self._training_stem != stem:
                return   # another stem took over — bail silently
            if win32api.GetAsyncKeyState(0x01) & 0x8000:
                click_pos = win32api.GetCursorPos()   # record position on down
                # Wait for mouse-up so the screenshot shows the button at rest
                while win32api.GetAsyncKeyState(0x01) & 0x8000:
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
                try:
                    # Snapshot the enabled set — no Tcl calls from this thread
                    enabled = set(self._autoclick_enabled)
                    if enabled:
                        raw = sct.grab(sct.monitors[0])
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
                                cx = max_loc[0] + w_t // 2
                                cy = max_loc[1] + h_t // 2
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
        try:
            import win32gui, win32con, win32api
        except ImportError:
            self._set_status("pywin32 missing — pip install pywin32")
            return

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
                import win32gui, win32con, win32api
                pos  = win32api.GetCursorPos()
                hwnd = win32gui.WindowFromPoint(pos)
                hwnd = win32gui.GetAncestor(hwnd, win32con.GA_ROOT)
                title = win32gui.GetWindowText(hwnd) or "(unknown)"
                rect  = win32gui.GetWindowRect(hwnd)
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

                import win32gui, win32con

                # ── Mode system: Agent2 intercept + safety header ─────────────
                if agent_id == "agent2":
                    if self._agent2_hold:
                        self._log(
                            "[mode] Agent2 is in HOLD — message blocked. "
                            "Click Disengage to reset.")
                        return
                    if not bypass_mode_check and self._mode == "module_block" and IMPL_ATTEMPT_RE.search(text):
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
                    REMINDER_EVERY)
                if not suppress_reminder and cfg.msg_count % _reminder_interval == 0:
                    if agent_id == "agent3":
                        rules = GROUND_RULES_VSCODE_BRIEF
                    elif agent_id == "agent1":
                        rules = GROUND_RULES_AGENT1
                    else:
                        rules = GROUND_RULES_AGENT2
                    text = rules + "\n\n" + text
                    self._log(f"[recal] role reminder injected to {agent_id} "
                              f"(msg #{cfg.msg_count}, every {_reminder_interval})")
                proj = self._project_name_var.get().strip()
                if agent_id == "agent1" and proj:
                    text = f"[ACTIVE PROJECT: {proj}]\n\n" + text

                if agent_id == "agent1" and self._bing_mode:
                    text = BING_NOISE_PREFIX + text

                pyperclip.copy(text)

                # Restore and focus the target window (robust foreground set)
                try:
                    win32gui.ShowWindow(cfg.hwnd, win32con.SW_RESTORE)
                    win32gui.SetForegroundWindow(cfg.hwnd)
                except Exception:
                    pass
                time.sleep(PASTE_DELAY)

                tmpl_input, tmpl_send = self._find_two_buttons(agent_id)
                input_xy = tmpl_input or cfg.input_xy

                if not input_xy:
                    input_xy = self._prompt_missing_coord(agent_id, "input")
                    if not input_xy:
                        self._log(
                            f"[router] {agent_id}: input field not located — "
                            "send aborted. Use ⊙ Input to set it.")
                        self._set_status(
                            f"⚠ {agent_id}: input field missing — set via ⊙ Input")
                        return
                    try:
                        win32gui.ShowWindow(cfg.hwnd, win32con.SW_RESTORE)
                        win32gui.SetForegroundWindow(cfg.hwnd)
                    except Exception:
                        pass
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
                pyautogui.hotkey("ctrl", "a")
                time.sleep(_between)
                pyautogui.hotkey("ctrl", "v")

                # For Bing, poll until the send button template appears (max 6 s).
                # For other agents use the fixed SEND_DELAY then a single template check.
                if _is_bing:
                    send_xy = None
                    for _ in range(30):          # 30 × 0.2 s = 6 s max
                        time.sleep(0.2)
                        found = self._find_agent_button_xy(agent_id, "send")
                        if found:
                            send_xy = found
                            break
                    if send_xy is None:          # template never appeared — fall back
                        send_xy = tmpl_send or cfg.send_xy
                        self._log(f"[→{agent_id}] send button not found via template "
                                  f"— falling back to calibrated coords")
                else:
                    time.sleep(SEND_DELAY)
                    send_xy = tmpl_send or self._find_agent_button_xy(agent_id, "send") or cfg.send_xy

                if not send_xy:
                    send_xy = self._prompt_missing_coord(agent_id, "send")

                if send_xy:
                    try:
                        pyautogui.click(*send_xy)
                    except Exception:
                        pass
                    self._click_count += 1
                    self.root.after(0, lambda: self.clicks_lbl.config(
                        text=f"sends: {self._click_count}"))
                    self._log(
                        f"[→{agent_id}] ✓  {text[:70]}{'…' if len(text) > 70 else ''}")
                else:
                    self._log(
                        f"[→{agent_id}] pasted — send button not found "
                        f"(use ⊙ Send to set it)  {text[:60]}")
                    self._set_status(
                        f"⚠ {agent_id}: message pasted — set send button via ⊙ Send")
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

    def _route_text(self, ocr_text: str, source_agent: str | None = None) -> int:
        """Extract and route messages. Returns number of messages routed.
        source_agent: if set, skip any message addressed TO that same agent —
        a window cannot legitimately route a message to itself (prevents SOP/reminder
        text displayed in the window from being re-injected back into it)."""
        # Strip Edge browser prefix echoed back in Agent 1's output
        if self._bing_mode and BING_NOISE_PREFIX in ocr_text:
            ocr_text = ocr_text.replace(BING_NOISE_PREFIX, "")

        routed = 0

        def _try_route(agent_id: str, body: str) -> bool:
            """Apply hold-state gate then dedup, then inject. Returns True if routed."""
            # Directional guard: a window cannot self-route.
            if source_agent and agent_id == source_agent:
                self._log(f"[ocr] directional skip — '{agent_id}' seen in its own window")
                return False

            # Global pause: OCR keeps scanning but nothing injects.
            if self._paused:
                return False

            # Agent 3 bypass: when active, ignore all traffic to/from agent3.
            if self._bypass_agent3 and (agent_id == "agent3" or source_agent == "agent3"):
                return False

            # Manual per-agent hold: blocks routing FROM the held agent's window.
            # Hold A1 = pause agent1's outgoing messages (source_agent="agent1" blocked).
            if source_agent and self._manual_hold.get(source_agent):
                return False

            if self._waiting_reply == agent_id:
                elapsed = time.time() - self._waiting_since
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
            body_h = self._msg_hash(body)
            if body_h == self._last_routed_body.get(agent_id):
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

            if not self._dedup(body):
                return False
            self._inject_to_agent(agent_id, body)

            # Clear pending trigger for source window — message successfully routed
            if source_agent and source_agent in self._pending_trigger:
                self._pending_trigger[source_agent] = None

            # Store first line of body for welfare check context (block ID or reply preview).
            self._last_routed_text[agent_id] = body.splitlines()[0][:120] if body else ""
            # Routing is healthy — reset auto-welfare state.
            self._last_route_time = time.time()
            self._welfare_fired   = False

            # Update body-match guard: record what we just sent to this agent.
            # Do NOT clear the other agent's guard here — it must stay set until
            # new content from that agent naturally replaces it. Clearing it early
            # was the root cause of duplicate blocks being re-routed after hold release.
            self._last_routed_body[agent_id] = body_h

            # Mode trigger: only fires when the phrase is the BODY of a routed message.
            body_low = body.lower()
            if agent_id == "agent2" and self._mode == "module_block":
                if any(p in body_low for p in IMPL_TRIGGER_PHRASES):
                    self._mode = "implementation"
                    self.root.after(0, self._update_mode_indicator)
                    self._log("[mode] ✓ IMPLEMENTATION MODE — final block phrase routed to Agent 2")
            if agent_id == "agent1" and self._mode == "implementation":
                if IMPL_COMPLETE_PHRASE in body_low:
                    self._mode = "module_block"
                    self.root.after(0, self._update_mode_indicator)
                    self._log("[mode] ✓ Implementation complete — MODULE BLOCK MODE restored")

            # Auto-release manual holds after one successful route — one-shot gate.
            if any(self._manual_hold.values()):
                for k in self._manual_hold:
                    self._manual_hold[k] = False
                self.root.after(0, self._reset_hold_buttons)

            # Enter hold: wait for the destination agent to reply before routing again
            self._waiting_reply  = agent_id
            self._waiting_since  = time.time()
            self._waiting_body_hash = self._msg_hash(body)
            self.root.after(0, self._update_ocr_hold_label)
            return True

        # Primary: sentinel-delimited protocol
        #   To agent1
        #   "body"
        #   end message now
        for m in SENTINEL_RE.finditer(ocr_text):
            raw_ch  = m.group(1)
            digit   = _OCR_DIGIT_NORM.get(raw_ch, raw_ch)
            if digit not in ("1", "2", "3"):
                continue
            agent_id = f"agent{digit}"
            body = m.group(2).strip().strip('"\'').strip()
            if not body:
                continue
            if _try_route(agent_id, body):
                routed += 1

        # Fallback: inline single-line  "to agent1: message"
        if routed == 0:
            for m in INLINE_RE.finditer(ocr_text):
                raw_ch  = m.group(1)
                digit   = _OCR_DIGIT_NORM.get(raw_ch, raw_ch)
                if digit not in ("1", "2", "3"):
                    continue
                agent_id = f"agent{digit}"
                body = m.group(2).strip().strip('"\'').strip()
                if not body:
                    continue
                if _try_route(agent_id, body):
                    routed += 1

        return routed

    @staticmethod
    def _msg_hash(text: str) -> str:
        """Stable hash of a message body — normalises whitespace so OCR
        variation (extra spaces, different line endings) hashes identically."""
        normalised = " ".join(text.lower().split())
        return hashlib.md5(normalised.encode()).hexdigest()

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
        try:
            orig = win32api.GetCursorPos()
            pyautogui.scroll(-5, x, y)   # negative = scroll down on Windows
            win32api.SetCursorPos(orig)
        except Exception:
            pass

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
        """Toggle the per-agent manual hold. While held, OCR will not route TO that agent.
        Auto-releases after one successful route to any agent (one-shot gate)."""
        held = not self._manual_hold[agent_id]
        self._manual_hold[agent_id] = held
        short = "A1" if agent_id == "agent1" else "A2"
        btn = self._hold_btns[agent_id]
        if held:
            btn.config(text=f"▶ Resume {short}", bg=RED, fg="white",
                       activebackground="#c04040")
            self._log(f"[hold] {agent_id} paused — outgoing messages from {agent_id} blocked; auto-releases after next send")
        else:
            btn.config(text=f"⏸ Hold {short}", bg=BG2, fg=FG,
                       activebackground=BG2)
            self._log(f"[hold] {agent_id} resumed")

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
            self._log("[agent3] bypassed — agent3 OCR and routing disabled")
        else:
            self._a3_bypass_btn.config(text="● Agent 3  [active]", fg=GREEN)
            self._a3_panel_frame.pack(fill="x")
            if "agent3" in self._hold_btns:
                self._hold_btns["agent3"].pack(side="left", padx=(0, 4),
                                               before=self._pause_btn)
            self._log("[agent3] active — agent3 OCR and routing enabled")
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
        """Reset all manual hold buttons to idle state after auto-release."""
        for aid, btn in self._hold_btns.items():
            short = "A1" if aid == "agent1" else "A2"
            btn.config(text=f"⏸ Hold {short}", bg=BG2, fg=FG, activebackground=BG2)
        self._log("[hold] holds auto-released — back in sequence")

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

    def _welfare_check(self):
        """Send a compact re-sync prompt directly to both agents so they can self-locate
        and fall back into sequence. Injected directly (bypasses OCR routing).
        Only useful when the sequence has stalled — do not fire during normal operation."""
        last_to_a2 = self._last_routed_text.get("agent2", "(none recorded)")
        last_to_a1 = self._last_routed_text.get("agent1", "(none recorded)")

        project_line = f"[ACTIVE PROJECT: {self._project_name}]\n\n" if self._project_name else ""

        # Agent 2 — state position and resend confirmation if a block is pending
        msg_a2 = (
            f"{project_line}"
            "[SOC — WHERE AM I]\n"
            f"Last block SOC delivered to you: {last_to_a2}\n\n"
            "State your current position:\n"
            "1. What is the last block ID you successfully saved?\n"
            "2. Are you ready to receive the next block, or is one pending?\n\n"
            "If a block is saved and unconfirmed, resend confirmation now:\n"
            "To Agent1\n"
            "module block [BLOCK_ID] saved, ready for next block\n"
            "end message now"
        )

        # Agent 1 — orient and re-engage with the correct next block
        msg_a1 = (
            f"{project_line}"
            "[SOC — WHERE AM I]\n"
            f"Last block SOC received from you: {last_to_a2}\n"
            f"Last Agent 2 confirmation SOC forwarded to you: {last_to_a1}\n\n"
            "State your current position:\n"
            "1. What is the last block ID you delivered to Agent 2?\n"
            "2. Has Agent 2 confirmed that block?\n"
            "3. What is the next block ID you need to send?\n\n"
            "Then send the next block in the standard relay format."
        )

        self._log(f"[welfare] sending re-sync to agent1 and agent2")
        self._log(f"[welfare] last→agent2: {last_to_a2[:60]}")
        self._log(f"[welfare] last→agent1: {last_to_a1[:60]}")
        self._log("[welfare] auto-welfare will NOT repeat — if agents stay unresponsive, "
                  "human intervention required (check agent cloud connectivity)")

        # Clear the dedup/body-match blocks so routing can resume after welfare reply
        self._last_routed_body.clear()
        if self._waiting_body_hash:
            self._dedup_clear(self._waiting_body_hash)
        self._waiting_reply     = None
        self._waiting_since     = 0.0
        self._waiting_body_hash = None
        # Reset stall clock — if agents respond, routing will reset _welfare_fired.
        # If they don't respond, _welfare_fired stays True and auto-welfare won't repeat.
        self._welfare_fired  = True
        self._last_route_time = time.time()
        self.root.after(0, self._update_ocr_hold_label)

        threading.Thread(
            target=lambda: (
                self._inject_to_agent("agent2", msg_a2),
                self._inject_to_agent("agent1", msg_a1)),
            daemon=True).start()

    def _toggle_ocr(self):
        if self._ocr_running:
            self._ocr_running = False
            self._waiting_reply = None
            self._waiting_since = 0.0
            self._scroll_accum_active.clear()
            self._scroll_accum.clear()
            self._pending_trigger.clear()
            self.ocr_btn.config(text="▶ Start OCR", bg=GREEN, fg="#1e1e1e",
                                 activebackground="#3aaf7a")
            self.ocr_lbl.config(text="OCR: OFF", fg=FG)
            self._log("[ocr] stopped")
        else:
            self._ocr_running    = True
            self._waiting_reply  = None
            self._waiting_since  = 0.0
            self._last_route_time = time.time()   # reset stall clock on fresh start
            self._welfare_fired  = False
            self.ocr_btn.config(text="■ Stop OCR", bg=RED, fg="white",
                                 activebackground="#c04040")
            self.ocr_lbl.config(text="OCR: scanning…", fg=GREEN)
            self._ocr_thread = threading.Thread(
                target=self._ocr_loop, daemon=True)
            self._ocr_thread.start()
            self._log(f"[ocr] started — {SCAN_NORMAL}s normal / "
                      f"{SCAN_RAPID}s rapid (triggers on 'to agent' spotted)")
            self._log("[ocr] watching for:  To agentX  →  body  →  "
                      "paste then send this now")

    def _ocr_loop(self):
        # Open one mss context for the lifetime of the scan loop — avoids
        # per-tick OS-level context creation/destruction overhead.
        with _mss_ctor() as sct:
            while self._ocr_running:
                try:
                    self._ocr_tick(sct)
                    # Auto-welfare: region pixel-static for 2 min AND routing quiet
                    # for 2 min → fire once. Region still changing = agent working,
                    # welfare suppressed regardless of routing silence.
                    if not self._welfare_fired:
                        check_aid   = self._waiting_reply or "agent2"
                        idle_secs   = time.time() - self._region_last_change.get(check_aid, 0)
                        route_gap   = time.time() - self._last_route_time
                        if idle_secs >= HEARTBEAT_IDLE and route_gap >= HEARTBEAT_IDLE:
                            self._welfare_fired = True
                            self._log(
                                f"[welfare] ⟳ auto — {check_aid} region static "
                                f"{int(idle_secs)}s, no routing {int(route_gap)}s → sending welfare check")
                            self.root.after(0, self._welfare_check)
                        elif route_gap >= HEARTBEAT_IDLE:
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
            rx0, ry0, rx1, ry1 = cfg.ocr_region
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
            if text_h == self._last_ocr_text.get(aid):
                continue
            self._last_ocr_text[aid] = text_h

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
            self._log(
                f"[trigger] ✓ remembered trigger routed "
                f"({source_agent}→{dest_agent})")
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
            # Send initial workflow briefing to Agent 3 on activation
            self._write_outbox("agent3", GROUND_RULES_VSCODE_AGENT3, "briefing")
            self.vscode_btn.config(text="■ VS Code", bg=GREEN, fg="#1e1e1e")
            self._log(
                "[VS Code mode] ON\n"
                "  • Outbox watching: outbox/agent1/  outbox/agent2/\n"
                "  • Auto-click scan: active\n"
                f"  • Agent 1 briefing sent — brief reminder every {REMINDER_EVERY} messages\n"
                "  Drop .md files into outbox/ from Copilot or Claude Code —\n"
                "  SOC will inject them and click approval buttons automatically.")
            self._set_status("VS Code mode ON — outbox + auto-click active")

    def _write_outbox(self, agent_id: str, content: str, prefix: str = "soc"):
        """Write content as a .md file to outbox/agent_id/ for the
        file watcher to detect and inject into the agent's chat window."""
        ts   = datetime.now().strftime("%H%M%S%f")
        path = OUTBOX_DIR / agent_id / f"{ts}_{prefix}.md"
        try:
            path.write_text(content, encoding="utf-8")
        except Exception as e:
            self._log(f"[outbox] write error ({agent_id}/{prefix}): {e}")

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

    def _fw_loop(self):
        while self._fw_running:
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
                            self._inject_to_agent(agent_id, content)
                        ts   = datetime.now().strftime("%H%M%S%f")
                        dest = SENT_DIR / agent_id / f"{ts}_{f.name}"
                        shutil.move(str(f), str(dest))
                    except Exception as e:
                        self._log(f"[outbox] {f.name} error: {e}")
            time.sleep(OUTBOX_POLL)

    # ── Drag + helpers ────────────────────────────────────────────────────────

    def _log(self, msg: str):
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
        """Persist window titles, prefix settings, and auto-click toggle states.
        Coordinates are NOT saved — windows move between sessions so saved
        pixel positions would be wrong. Templates find fresh coords every time."""
        import json
        data = {}
        for aid, cfg in self.agents.items():
            data[aid] = {
                "window_title":   cfg.title if cfg.title != "(not set)" else None,
                "prefix_enabled": cfg.prefix_enabled.get() if cfg.prefix_enabled else False,
                "prefix_text":    cfg.prefix_var.get()     if cfg.prefix_var    else "",
                "ocr_region":     list(cfg.ocr_region) if cfg.ocr_region else None,
            }
        data["project_name"]   = self._project_name_var.get()
        data["bypass_agent3"]  = self._bypass_agent3
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
        and auto-click toggle states. Coordinates are always found fresh
        by template matching on startup."""
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
        if data.get("project_name"):
            self._project_name_var.set(data["project_name"])
        # Restore agent3 bypass state (default True if not in config)
        self._bypass_agent3 = data.get("bypass_agent3", True)
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
        # Restore auto-click toggle states
        for stem, enabled in data.get("autoclick", {}).items():
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
            import win32gui
            live = []
            win32gui.EnumWindows(
                lambda hwnd, lst: lst.append((hwnd, win32gui.GetWindowText(hwnd)))
                    if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd)
                    and not win32gui.IsIconic(hwnd)
                    else None,
                live)
            for aid, cfg in self.agents.items():
                if not cfg.title or cfg.title == "(not set)" or cfg.hwnd:
                    continue
                saved = cfg.title.lower()
                for hwnd, title in live:
                    # Partial match — tolerates tab-name changes
                    if saved[:30] in title.lower() or title.lower()[:30] in saved:
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
                "        agent2_input.png   agent2_send.png\n"
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
        for aid in ("agent1", "agent2"):
            if not stem.startswith(aid + "_"):
                continue
            role = stem[len(aid) + 1:]   # input / send / scroll_dn / scroll_up
            cfg  = self.agents[aid]
            x, y = xy

            # Reject matches that fall outside the agent's known window rect.
            # Prevents false positives when a template accidentally matches a
            # UI element in a different part of the screen.
            if cfg.ocr_region:
                rx0, ry0, rx1, ry1 = cfg.ocr_region
                if not (rx0 <= x <= rx1 and ry0 <= y <= ry1):
                    self._log(f"[cal] {aid}.{role} → ({x},{y}) outside window — skipped")
                    return

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

            # ── Fill agent config slot ────────────────────────────────────
            def _ui(r=role, c=cfg, px=x, py=y, trained=rec["trained"]):
                colour = GREEN if trained else ACCENT
                if r == "input":
                    c.input_xy = (px, py)
                    c.lbl_input.config(text=f"input field: ({px},{py})", fg=colour)
                elif r == "send":
                    c.send_xy = (px, py)
                    c.lbl_send.config(text=f"send button: ({px},{py})", fg=colour)
                elif r == "scroll_dn":
                    c.scroll_dn_xy = (px, py)
                    c.lbl_scroll.config(text=f"scroll↓: ({px},{py})", fg=colour)
                elif r == "scroll_up":
                    c.scroll_up_xy = (px, py)
                    c.lbl_scroll.config(text=f"scroll↑↓: ({px},{py})", fg=colour)
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
        Returns a 2-tuple ((ix,iy), (sx,sy)); either entry may be None."""
        if not _CV2_OK:
            return None, None
        ag_num = agent_id[-1]   # '1' or '2'
        input_tpl = send_tpl = None
        for png in TEMPLATE_DIR.iterdir():
            if png.suffix.lower() != ".png":
                continue
            s = png.stem.lower()
            if f"agent{ag_num}" not in s:
                continue
            if "input" in s:
                input_tpl = self._safe_imread(png, cv2.IMREAD_GRAYSCALE)
            elif "send" in s:
                send_tpl = self._safe_imread(png, cv2.IMREAD_GRAYSCALE)
        if input_tpl is None and send_tpl is None:
            return None, None
        with _mss_ctor() as sct:
            raw = sct.grab(sct.monitors[1])
            gray = cv2.cvtColor(
                np.array(Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")),
                cv2.COLOR_RGB2GRAY)

        def _match(tpl: "np.ndarray") -> "tuple | None":
            if tpl is None:
                return None
            th, tw = tpl.shape
            res = cv2.matchTemplate(gray, tpl, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if max_val >= TEMPLATE_THRESH:
                return (max_loc[0] + tw // 2, max_loc[1] + th // 2)
            return None

        return _match(input_tpl), _match(send_tpl)

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
        Searches TEMPLATE_DIR for a PNG whose stem (lowercased) contains
        the agent number AND the role keyword."""
        if not _CV2_OK:
            return None
        ag_num = agent_id[-1]   # '1' or '2'
        keywords = {"input": "input", "send": "send"}
        kw = keywords.get(role, "")
        for png in TEMPLATE_DIR.iterdir():
            if png.suffix.lower() != ".png":
                continue
            stem = png.stem.lower()
            if f"agent{ag_num}" in stem and kw in stem:
                return self._find_template(png.name)
        return None

    def _find_template(self, name: str) -> tuple | None:
        """Find a single named template on screen. Returns (x,y) centre or None."""
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
        if max_val >= TEMPLATE_THRESH:
            return (max_loc[0] + tw // 2, max_loc[1] + th // 2)
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
            import win32gui, win32con
            win32gui.ShowWindow(cfg.hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(cfg.hwnd)
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
        """Return lines from new_text that don't already appear in the
        last 25 lines of existing — handles overlapping scroll views."""
        if not existing.strip():
            return new_text
        tail = {ln.strip().lower()
                for ln in existing.strip().splitlines()[-25:]
                if ln.strip()}
        fresh = [ln for ln in new_text.splitlines()
                 if ln.strip().lower() not in tail]
        return "\n".join(fresh)

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
        self._update_mode_indicator()
        self._log(
            f"[mode] Disengaged by user  ({prev} → module_block)  "
            "hold + all session counters cleared")
        self._set_status("Mode reset: MODULE BLOCK MODE")

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
        threading.Thread(
            target=self._inject_to_agent,
            args=("agent2", AGENT2_SOP),
            kwargs={"bypass_mode_check": True},
            daemon=True).start()
        self._log("[mode] Agent2 SOP sent — 25s OCR grace active")
        self._set_status("Agent2 SOP sent")

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
_INSTANCE_MUTEX = None  # held open for the lifetime of the process

def _acquire_instance_lock() -> bool:
    global _INSTANCE_MUTEX
    h = ctypes.windll.kernel32.CreateMutexW(None, True, "Local\\SOCUltralight_v1")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return False
    _INSTANCE_MUTEX = h
    return True


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Hide any console window — covers python.exe launches and stray library consoles
    _con = ctypes.windll.kernel32.GetConsoleWindow()
    if _con:
        ctypes.windll.user32.ShowWindow(_con, 0)

    if not _acquire_instance_lock():
        _r = tk.Tk()
        _r.withdraw()
        messagebox.showerror("SOC Ultralight", "SOC Ultralight is already running.")
        sys.exit(1)

    root = tk.Tk()
    app = SOCUltralight(root)
    root.mainloop()
    sys.exit(0)
