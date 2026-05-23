#!/usr/bin/env python3
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

import tkinter as tk
from tkinter import scrolledtext
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
from PIL import Image, ImageTk
import pytesseract

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
PASTE_DELAY      = 0.25   # seconds after window focus before paste
SEND_DELAY       = 2.0    # seconds after paste before clicking Send
                          # (VS Code/Bing send button only appears after text is entered)
OUTBOX_POLL      = 0.5    # seconds between outbox folder checks
MAX_SEEN_HASHES  = 300    # rolling dedup window
REMINDER_EVERY   = 5      # inject ground rules every N messages per agent
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
AUTOCLICK_LOCKED = ("input_field", "send_message", "_scroll")

for _d in [OUTBOX_DIR / "agent1", OUTBOX_DIR / "agent2", OUTBOX_DIR / "agent3",
           SENT_DIR   / "agent1", SENT_DIR   / "agent2", SENT_DIR   / "agent3",
           TEMPLATE_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

pyautogui.FAILSAFE = True
pyautogui.PAUSE    = 0.1

# ── Per-agent anti-drift recalibration reminders ────────────────────────────
# Injected every REMINDER_EVERY sends to keep each agent on-role.

GROUND_RULES_AGENT1 = (
    "[SYSTEM RECALIBRATION — AGENT 1]\n"
    "You are Agent 1 — Senior Software Engineer (30 years experience).\n"
    "Your role: receive implementation tasks from the Project Manager (Agent 2) "
    "and carry them out with precision inside VS Code.\n"
    "\n"
    "COMMUNICATION PROTOCOL (outgoing messages to Agent 2):\n"
    "  To Agent2\n"
    "  <your message here>\n"
    "  end message now\n"
    "\n"
    "ESCALATION RULE: If you hit a blocker, ambiguity, or error you cannot resolve:\n"
    "  — STOP implementation immediately.\n"
    "  — Report to Agent 2 in this exact format:\n"
    "      To Agent2\n"
    "      PROBLEM: <describe the issue clearly>\n"
    "      QUESTION: <ask what path to take next>\n"
    "      end message now\n"
    "  — Wait for a reply before continuing.\n"
    "\n"
    "Send only clean, professional, implementation-focused content."
)

GROUND_RULES_AGENT2 = (
    "[SYSTEM RECALIBRATION — AGENT 2]\n"
    "You are Agent 2 — Development Project Manager.\n"
    "Your role: direct Agent 1 (Senior Engineer in VS Code) through the project "
    "implementation plan, one clear set of steps at a time.\n"
    "\n"
    "COMMUNICATION PROTOCOL (outgoing messages to Agent 1):\n"
    "  To Agent1\n"
    "  <your instructions here>\n"
    "  ignore edge browser metadata noise\n"
    "  end message now\n"
    "\n"
    "WHEN AGENT 1 REPORTS A PROBLEM:\n"
    "  — Read the blocker report carefully.\n"
    "  — Respond with a clear, numbered resolution path:\n"
    "      To Agent1\n"
    "      Step 1: <action>\n"
    "      Step 2: <action>\n"
    "      ignore edge browser metadata noise\n"
    "      end message now\n"
    "  — Keep responses focused — one problem, one resolution set.\n"
    "\n"
    "Send only clean, professional, project-directive content."
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
SENTINEL_RE = re.compile(
    r"(?i)to\s+agent\s*([123])\s*[\r\n]+"   # header line
    r"(.*?)"                                  # message body (any lines)
    r"[\r\n]+\s*end\s+message\s+now",        # sentinel
    re.DOTALL)

# Fallback: single-line  "to agent1: message here"
INLINE_RE = re.compile(
    r"(?i)\bto\s+agent\s*([123])\s*[:\-]\s*(.+?)(?=\bto\s+agent\s*[123]\b|$)",
    re.DOTALL)

# Trigger: just seeing "to agent" text → enter rapid mode
TRIGGER_RE = re.compile(r"(?i)\bto\s+agent\s*[123]\b")

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
IMPL_TRIGGER_PHRASE  = "This is the final block. Agent2 may begin implementation."
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

        self._fw_running  = False
        self._fw_thread   = None
        self._vscode_mode = False   # Copilot+Claude Code mode (outbox + auto-click)
        self._bing_mode   = False   # Agent 1 Edge-browser-aware mode

        self._seen_hashes: OrderedDict[str, None] = OrderedDict()
        self._dedup_lock   = threading.Lock()    # guards _seen_hashes
        self._inject_lock  = threading.Lock()    # serialises clipboard writes
        self._click_count  = 0
        self._registry: dict = self._load_registry()  # template training history

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
        self.root.after(1800, self._startup_calibrate)  # auto-match templates

    # ── Window ────────────────────────────────────────────────────────────────

    def _build_window(self):
        self.root.title("SOC Ultralight")
        self.root.attributes("-topmost", True)
        self.root.overrideredirect(True)
        self.root.configure(bg=BG)
        self.root.resizable(False, False)

        w, h = 250, 1040
        sw = self.root.winfo_screenwidth()
        self.root.geometry(f"{w}x{h}+{sw - w - 20}+20")

        self.root.bind("<Button-1>",  self._drag_start)
        self.root.bind("<B1-Motion>", self._drag_move)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Title bar
        tb = tk.Frame(self.root, bg=BG2, height=28)
        tb.pack(fill="x")
        tb.bind("<Button-1>",  self._drag_start)
        tb.bind("<B1-Motion>", self._drag_move)
        tk.Label(tb, text="  SOC Ultralight",
                 bg=BG2, fg=FG, font=("Segoe UI", 9, "bold")
                 ).pack(side="left", pady=4)
        tk.Button(tb, text="X", command=self.root.destroy,
                  bg=BG2, fg=FG, relief="flat", font=("Segoe UI", 9, "bold"),
                  activebackground=RED, activeforeground="white",
                  cursor="hand2", bd=0, padx=8).pack(side="right")
        tk.Button(tb, text="—", command=self.root.iconify,
                  bg=BG2, fg=FG, relief="flat", font=("Segoe UI", 9, "bold"),
                  activebackground=BG2, activeforeground="white",
                  cursor="hand2", bd=0, padx=8).pack(side="right")

        # Protocol reminder
        tk.Label(self.root,
                 text='Protocol:  To agent1  →  body  →  end message now',
                 bg=BG2, fg=YELLOW, font=("Consolas", 7), anchor="w", pady=3,
                 wraplength=244
                 ).pack(fill="x", padx=0)

        # Agent panels
        self._build_agent_panel("agent1", "Agent 1")
        tk.Frame(self.root, bg=BG2, height=1).pack(fill="x", padx=10, pady=2)
        self._build_agent_panel("agent2", "Agent 2")
        tk.Frame(self.root, bg=BG2, height=1).pack(fill="x", padx=10, pady=2)
        self._build_agent_panel("agent3", "Agent 3")
        tk.Frame(self.root, bg=BG2, height=1).pack(fill="x", padx=10, pady=4)

        # ── Mode indicator ─────────────────────────────────────────────────
        mode_row = tk.Frame(self.root, bg=BG2, pady=3)
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

        # Controls row 0: Agent startup + Home
        ctrl0 = tk.Frame(self.root, bg=BG, pady=2)
        ctrl0.pack(fill="x", padx=12)
        tk.Button(
            ctrl0, text="⌂", command=self._log_scroll_top,
            bg=BG2, fg=FG, font=("Segoe UI", 9),
            relief="flat", cursor="hand2", padx=6, pady=4
        ).pack(side="left")
        tk.Button(
            ctrl0, text="▶ Agent 1", command=self._start_agent1,
            bg=BG2, fg=ACCENT, font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2", padx=8, pady=4
        ).pack(side="left", padx=(4, 0))
        tk.Button(
            ctrl0, text="▶ Agent 2", command=self._start_agent2,
            bg=BG2, fg=GREEN, font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2", padx=8, pady=4
        ).pack(side="left", padx=(4, 0))

        # Controls row 1: OCR
        ctrl1 = tk.Frame(self.root, bg=BG, pady=2)
        ctrl1.pack(fill="x", padx=12)

        self.ocr_btn = tk.Button(
            ctrl1, text="▶ Start OCR", command=self._toggle_ocr,
            bg=GREEN, fg="#1e1e1e", font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2", activebackground="#3aaf7a",
            padx=10, pady=4)
        self.ocr_btn.pack(side="left")

        self.ocr_lbl = tk.Label(ctrl1, text="OCR: OFF",
                                 bg=BG, fg=FG, font=("Segoe UI", 8, "italic"))
        self.ocr_lbl.pack(side="left", padx=6)

        # Controls row 2: Outbox / Calibrate / sends
        ctrl2 = tk.Frame(self.root, bg=BG, pady=2)
        ctrl2.pack(fill="x", padx=12)

        self.fw_btn = tk.Button(
            ctrl2, text="▶ Outbox", command=self._toggle_file_watcher,
            bg=BG2, fg=ACCENT, font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2", padx=8, pady=4)
        self.fw_btn.pack(side="left")

        tk.Button(
            ctrl2, text="⌖ Cal", command=self._auto_calibrate,
            bg=BG2, fg=YELLOW, font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2", padx=8, pady=4
        ).pack(side="left", padx=(4, 0))

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

        self.clicks_lbl = tk.Label(ctrl2, text="sends: 0",
                                    bg=BG, fg=YELLOW, font=("Segoe UI", 8))
        self.clicks_lbl.pack(side="right")

        # Manual inject
        inj = tk.Frame(self.root, bg=BG)
        inj.pack(fill="x", padx=12, pady=(4, 2))
        tk.Label(inj, text="Inject:", bg=BG, fg=FG,
                 font=("Segoe UI", 8)).pack(side="left")
        self.inject_entry = tk.Entry(
            inj, bg=BG2, fg=FG, insertbackground=FG,
            relief="flat", font=("Segoe UI", 9))
        self.inject_entry.pack(side="left", fill="x", expand=True, padx=(6, 4))
        self.inject_entry.insert(0, "to agent1: hello from agent2")
        self.inject_entry.bind("<Return>", self._manual_inject)
        tk.Button(inj, text="Send", command=self._manual_inject,
                  bg=BG2, fg=ACCENT, relief="flat",
                  font=("Segoe UI", 8), cursor="hand2", padx=8
                  ).pack(side="right")

        # Log drawer header (always visible)
        self._log_open = False
        log_hdr = tk.Frame(self.root, bg=BG2)
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

        # Log (collapsible)
        self.log = scrolledtext.ScrolledText(
            self.root, height=8, wrap="word",
            bg=BG2, fg=FG, insertbackground=FG,
            font=("Consolas", 8), relief="flat",
            borderwidth=0, padx=6, pady=6)
        # Log starts collapsed; opened via ▶ Diagnostics toggle
        self.log.config(state="disabled")
        # Ctrl+C copies selection (widget is read-only/disabled so default copy is suppressed)
        self.log.bind("<Control-c>", self._copy_log_selection)

        # Status
        self.status_var = tk.StringVar(
            value="Set agent windows, input fields, and send buttons")
        tk.Label(self.root, textvariable=self.status_var,
                 bg=BG, fg=ORANGE, font=("Segoe UI", 8, "italic"),
                 anchor="w", wraplength=234
                 ).pack(fill="x", padx=12, pady=(0, 4))

        # Auto-Click settings panel
        self._build_autoclick_panel()

    def _build_agent_panel(self, agent_id: str, label: str):
        cfg = self.agents[agent_id]
        outer = tk.Frame(self.root, bg=BG, pady=2)
        outer.pack(fill="x", padx=12)

        r1 = tk.Frame(outer, bg=BG)
        r1.pack(fill="x")
        tk.Label(r1, text=label, bg=BG, fg=ACCENT,
                 font=("Segoe UI", 9, "bold"), width=6, anchor="w"
                 ).pack(side="left")
        cfg.lbl_window = tk.Label(r1, text="window: (not set)",
                                   bg=BG, fg=RED, font=("Segoe UI", 8, "italic"))
        cfg.lbl_window.pack(side="left")
        tk.Button(r1, text="Set Win",
                  command=lambda a=agent_id: self._set_window(a),
                  bg=BG2, fg=FG, relief="flat", font=("Segoe UI", 8),
                  cursor="hand2", padx=4).pack(side="right")

        r2 = tk.Frame(outer, bg=BG)
        r2.pack(fill="x")
        tk.Label(r2, text="", bg=BG, width=3).pack(side="left")
        cfg.lbl_input = tk.Label(r2, text="input field: (not set)",
                                  bg=BG, fg=RED, font=("Segoe UI", 8, "italic"))
        cfg.lbl_input.pack(side="left")
        tk.Button(r2, text="⊙ Input",
                  command=lambda a=agent_id: self._capture_coord(a, "input"),
                  bg=BG2, fg=FG, relief="flat", font=("Segoe UI", 7),
                  cursor="hand2", padx=4).pack(side="right")

        r3 = tk.Frame(outer, bg=BG)
        r3.pack(fill="x")
        tk.Label(r3, text="", bg=BG, width=3).pack(side="left")
        cfg.lbl_send = tk.Label(r3, text="send button: (not set)",
                                 bg=BG, fg=RED, font=("Segoe UI", 8, "italic"))
        cfg.lbl_send.pack(side="left")
        tk.Button(r3, text="⊙ Send",
                  command=lambda a=agent_id: self._capture_coord(a, "send"),
                  bg=BG2, fg=FG, relief="flat", font=("Segoe UI", 7),
                  cursor="hand2", padx=4).pack(side="right")

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
        tk.Label(r5, text="", bg=BG, width=3).pack(side="left")
        cfg.lbl_scroll = tk.Label(r5, text="scroll: (not set)",
                                   bg=BG, fg=RED, font=("Segoe UI", 8, "italic"))
        cfg.lbl_scroll.pack(side="left")
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

        # Row 6: OCR output region
        r6 = tk.Frame(outer, bg=BG)
        r6.pack(fill="x")
        tk.Label(r6, text="", bg=BG, width=3).pack(side="left")
        cfg.lbl_region = tk.Label(r6, text="ocr region: (not set)",
                                   bg=BG, fg=RED, font=("Segoe UI", 8, "italic"))
        cfg.lbl_region.pack(side="left")
        tk.Button(r6, text="⎕ Region",
                  command=lambda a=agent_id: self._calibrate_ocr_region(a),
                  bg=BG2, fg=YELLOW, relief="flat", font=("Segoe UI", 7),
                  cursor="hand2", padx=4).pack(side="right")

    # ── OCR region calibration overlay ───────────────────────────────────────────

    def _calibrate_ocr_region(self, agent_id: str):
        """Full-screen drag-to-select overlay. User draws a rectangle over
        the agent's message output area. That bounding box is used for all
        subsequent OCR and scroll-read grabs for this agent."""
        overlay = tk.Toplevel(self.root)
        sw = overlay.winfo_screenwidth()
        sh = overlay.winfo_screenheight()
        overlay.geometry(f"{sw}x{sh}+0+0")
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
            sw // 2, 36,
            text=f"Drag to select the {label_name} message output area",
            fill="#ffffff", font=("Segoe UI", 15, "bold"))
        canvas.create_text(
            sw // 2, 64,
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
            x1, y1, x2, y2 = _box
            if x2 - x1 < 40 or y2 - y1 < 40:
                canvas.create_text(
                    sw//2, sh//2,
                    text="Selection too small — drag a larger area",
                    fill=RED, font=("Segoe UI", 13, "bold"))
                return
            cfg = self.agents[agent_id]
            cfg.ocr_region = (x1, y1, x2, y2)
            w, h = x2 - x1, y2 - y1
            cfg.lbl_region.config(
                text=f"region: {w}x{h}px ({x1},{y1})", fg=GREEN)
            self._log(f"[{agent_id}] OCR region: ({x1},{y1})→({x2},{y2}) {w}x{h}px")
            self._save_config()
            overlay.destroy()

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>",     on_drag)
        overlay.bind("<Escape>",       lambda e: overlay.destroy())

        btn_y = sh - 52
        tk.Button(
            overlay, text="✓ Set Region", command=on_set,
            bg=GREEN, fg="#1e1e1e", font=("Segoe UI", 11, "bold"),
            relief="flat", padx=16, pady=6, cursor="hand2"
        ).place(x=sw//2 - 130, y=btn_y)
        tk.Button(
            overlay, text="✕ Cancel", command=overlay.destroy,
            bg=RED, fg="white", font=("Segoe UI", 11, "bold"),
            relief="flat", padx=16, pady=6, cursor="hand2"
        ).place(x=sw//2 + 30, y=btn_y)

    # ── Auto-Click settings panel ─────────────────────────────────────────────

    def _build_autoclick_panel(self):
        """Collapsible panel showing all templates in buttons database/ as
        thumbnail rows with an ON/OFF toggle each.  When the auto-click scan
        is running it periodically screenshots the desktop and clicks any
        enabled template it finds."""

        tk.Frame(self.root, bg=BG2, height=1).pack(fill="x", padx=10, pady=(4, 0))

        # Header row
        hdr = tk.Frame(self.root, bg=BG2)
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
        self._ac_body = tk.Frame(self.root, bg=BG)
        # _ac_body starts collapsed; opened via ▶ Auto-Click toggle

        self._ac_list_frame = tk.Frame(self._ac_body, bg=BG)
        self._ac_list_frame.pack(fill="x")

        self._refresh_autoclick_list()

    def _toggle_autoclick_panel(self):
        if self._autoclick_panel_open:
            self._ac_body.pack_forget()
            self._ac_toggle_btn.config(text="▶ Auto-Click")
        else:
            self._ac_body.pack(fill="x", padx=10, pady=(2, 4))
            self._ac_toggle_btn.config(text="▼ Auto-Click")
        self._autoclick_panel_open = not self._autoclick_panel_open

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
        self.root.iconify()
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
            with mss.MSS() as sct:
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
        with mss.MSS() as sct:
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
        try:
            import win32gui
            hwnd  = win32gui.GetForegroundWindow()
            title = win32gui.GetWindowText(hwnd)
            if "SOC Ultralight" in title:
                self._set_status(
                    f"Click into {agent_id}'s window first, then Set Window")
                return
            cfg = self.agents[agent_id]
            cfg.hwnd  = hwnd
            cfg.title = title
            short = (title[:26] + "…") if len(title) > 26 else title
            cfg.lbl_window.config(text=f"window: {short}", fg=GREEN)
            self._log(f"[{agent_id}] window → {title}")
            self._save_config()
        except ImportError:
            self._set_status("pywin32 missing — pip install pywin32")
        except Exception as e:
            self._set_status(f"Set window error: {e}")

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
            with mss.MSS() as sct:
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
                self.root.iconify()
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

    def _inject_to_agent(self, agent_id: str, text: str):
        """Focus agent window, paste text into input field, click Send.
        Uses _find_two_buttons to locate input+send in one screenshot.
        Serialised via _inject_lock to prevent clipboard clobber on concurrent calls."""
        cfg = self.agents.get(agent_id)
        if not cfg or not cfg.hwnd:
            self._log(f"[router] {agent_id} window not configured — skipped")
            return
        with self._inject_lock:
            try:
                import win32gui, win32con

                # ── Mode system: Agent2 intercept + safety header ─────────────
                if agent_id == "agent2":
                    if self._agent2_hold:
                        self._log(
                            "[mode] Agent2 is in HOLD — message blocked. "
                            "Click Disengage to reset.")
                        return
                    # Safety: IMPL_TRIGGER_PHRASE contains "begin implementation"
                    # which would match IMPL_ATTEMPT_RE. This guard is safe because
                    # _route_text sets self._mode = "implementation" before calling
                    # _inject_to_agent, so the phrase arrives here with mode already
                    # "implementation" and this branch is skipped.
                    if self._mode == "module_block" and IMPL_ATTEMPT_RE.search(text):
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
                    if self._mode == "module_block":
                        text = MODULE_BLOCK_HEADER + "\n" + text

                # ── Mode system: Agent1 anti-drift counters ───────────────────
                if agent_id == "agent1":
                    self._agent1_inbound_count += 1
                    if BLOCK_SAVED_RE.search(text):
                        self._consecutive_saved_count += 1
                    else:
                        self._consecutive_saved_count = 0
                    # Block-sequence reminder: every 10th consecutive block-saved
                    if (self._mode == "module_block"
                            and self._consecutive_saved_count > 0
                            and self._consecutive_saved_count % ANTIDRIFT_EVERY == 0):
                        text = ANTIDRIFT_BLOCK_REM + "\n" + text
                        self._log(
                            f"[anti-drift] block-sequence reminder "
                            f"(#{self._consecutive_saved_count})")
                    # Message-count reminder: every ANTIDRIFT_EVERY messages
                    elif (self._mode == "module_block"
                            and self._agent1_inbound_count % ANTIDRIFT_EVERY == 0):
                        text = ANTIDRIFT_MSG_REM + "\n" + text
                        self._log(
                            f"[anti-drift] count reminder "
                            f"(msg #{self._agent1_inbound_count})")

                # Guard: truncate oversized messages before they can hang the UI
                if len(text) > self.MAX_INJECT_CHARS:
                    self._log(f"[router] message truncated "
                              f"{len(text)} → {self.MAX_INJECT_CHARS} chars")
                    text = text[:self.MAX_INJECT_CHARS]

                # Prepend per-agent prefix if enabled (Agent 1 manual checkbox)
                if agent_id != "agent1" and cfg.prefix_enabled and cfg.prefix_enabled.get() and cfg.prefix_var:
                    prefix = cfg.prefix_var.get().strip()
                    if prefix:
                        text = prefix + text

                # Periodic anti-drift recalibration (every REMINDER_EVERY messages)
                cfg.msg_count += 1
                if cfg.msg_count % REMINDER_EVERY == 0:
                    if agent_id == "agent3":
                        rules = GROUND_RULES_VSCODE_BRIEF
                    elif agent_id == "agent1":
                        rules = GROUND_RULES_AGENT1
                    else:
                        rules = GROUND_RULES_AGENT2
                    text = rules + "\n\n" + text
                    self._log(f"[recal] role reminder injected to {agent_id} "
                              f"(msg #{cfg.msg_count})")
                elif agent_id == "agent1" and self._bing_mode:
                    # Messages 1-4 of every 5: short edge browser noise-ignore note
                    text = BING_NOISE_PREFIX + text

                pyperclip.copy(text)
                win32gui.ShowWindow(cfg.hwnd, win32con.SW_RESTORE)
                win32gui.SetForegroundWindow(cfg.hwnd)
                time.sleep(PASTE_DELAY)

                # — One screenshot locates both input and send buttons —
                tmpl_input, tmpl_send = self._find_two_buttons(agent_id)
                input_xy = tmpl_input or cfg.input_xy

                # If still no input field: ask user to click it or skip this send
                if not input_xy:
                    input_xy = self._prompt_missing_coord(agent_id, "input")
                    if not input_xy:
                        self._log(
                            f"[router] {agent_id}: input field not located — "
                            "send aborted. Use ⊙ Input to set it.")
                        self._set_status(
                            f"⚠ {agent_id}: input field missing — set via ⊙ Input")
                        return
                    # Re-focus agent window after user interaction
                    win32gui.ShowWindow(cfg.hwnd, win32con.SW_RESTORE)
                    win32gui.SetForegroundWindow(cfg.hwnd)
                    time.sleep(PASTE_DELAY)

                pyautogui.click(*input_xy)
                time.sleep(0.15)
                # Select-all then paste so any leftover text is replaced cleanly.
                pyautogui.hotkey("ctrl", "a")
                time.sleep(0.05)

                pyautogui.hotkey("ctrl", "v")
                # Wait for send button to appear (only shows after text is in the field)
                time.sleep(SEND_DELAY)

                # — Send: use pre-found coord, or search again now button is visible —
                send_xy = tmpl_send or self._find_agent_button_xy(agent_id, "send") or cfg.send_xy

                # If still no send button: ask user to click it or accept paste-only
                if not send_xy:
                    send_xy = self._prompt_missing_coord(agent_id, "send")

                if send_xy:
                    pyautogui.click(*send_xy)
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

    # ── Routing logic ─────────────────────────────────────────────────────────

    def _route_text(self, ocr_text: str) -> int:
        """Extract and route messages. Returns number of messages routed."""
        # Strip Edge browser prefix echoed back in Agent 1's output
        if self._bing_mode and BING_NOISE_PREFIX in ocr_text:
            ocr_text = ocr_text.replace(BING_NOISE_PREFIX, "")

        # Implementation authorization trigger
        if (self._mode == "module_block"
                and IMPL_TRIGGER_PHRASE.lower() in ocr_text.lower()):
            self._mode = "implementation"
            self.root.after(0, self._update_mode_indicator)
            self._log("[mode] ✓ IMPLEMENTATION MODE activated — authorization phrase detected")

        routed = 0

        # Primary: sentinel-delimited protocol
        #   To agent1
        #   "body"
        #   paste then send this now
        for m in SENTINEL_RE.finditer(ocr_text):
            agent_id = f"agent{m.group(1)}"
            # Strip surrounding whitespace + quotation marks from body
            body = m.group(2).strip().strip('"\'').strip()
            if not body:
                continue
            if self._dedup(body):
                self._inject_to_agent(agent_id, body)
                routed += 1

        # Fallback: inline single-line  "to agent1: message"
        if routed == 0:
            for m in INLINE_RE.finditer(ocr_text):
                agent_id = f"agent{m.group(1)}"
                body = m.group(2).strip().strip('"\'').strip()
                if not body:
                    continue
                if self._dedup(body):
                    self._inject_to_agent(agent_id, body)
                    routed += 1

        return routed

    def _dedup(self, text: str) -> bool:
        """Return True if text is new (not seen before). Thread-safe via lock.
        Uses OrderedDict as an ordered set so the oldest hashes are evicted first."""
        h = hashlib.md5(text.encode()).hexdigest()
        with self._dedup_lock:
            if h in self._seen_hashes:
                return False
            self._seen_hashes[h] = None
            while len(self._seen_hashes) > MAX_SEEN_HASHES:
                self._seen_hashes.popitem(last=False)  # evict oldest
        return True

    def _manual_inject(self, _event=None):
        text = self.inject_entry.get().strip()
        if text:
            n = self._route_text(text)
            if n == 0:
                self._log(f"[manual] no route pattern found — {text[:60]}")
            self.inject_entry.delete(0, "end")

    # ── OCR watcher ───────────────────────────────────────────────────────────

    def _toggle_ocr(self):
        if self._ocr_running:
            self._ocr_running = False
            self.ocr_btn.config(text="▶ Start OCR", bg=GREEN, fg="#1e1e1e",
                                 activebackground="#3aaf7a")
            self.ocr_lbl.config(text="OCR: OFF", fg=FG)
            self._log("[ocr] stopped")
        else:
            self._ocr_running = True
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
        with mss.MSS() as sct:
            while self._ocr_running:
                try:
                    self._ocr_tick(sct)
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

                in_rapid = time.time() < self._rapid_until
                self.root.after(0, lambda r=in_rapid: self.ocr_lbl.config(
                    text="OCR: RAPID ⚡" if r else "OCR: scanning…",
                    fg=YELLOW if r else GREEN))
                time.sleep(SCAN_RAPID if in_rapid else SCAN_NORMAL)

    def _ocr_tick(self, sct):
        # Use union of defined OCR regions; fall back to full primary monitor
        regions = [cfg.ocr_region for cfg in self.agents.values() if cfg.ocr_region]
        if regions:
            x1 = min(r[0] for r in regions)
            y1 = min(r[1] for r in regions)
            x2 = max(r[2] for r in regions)
            y2 = max(r[3] for r in regions)
            grab_box = {"left": x1, "top": y1,
                        "width": x2 - x1, "height": y2 - y1}
        else:
            grab_box = sct.monitors[0]
        raw = sct.grab(grab_box)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

        # PSM 6: assume uniform block of text — good for chat windows
        text = pytesseract.image_to_string(img, config="--psm 6")
        low  = text.lower()

        # Step 1: "to agent" spotted → enter rapid mode regardless
        if TRIGGER_RE.search(text):
            self._rapid_until = time.time() + RAPID_DURATION

        # Step 2: full sentinel present → extract and route
        # _SENTINEL_VARIANTS covers common OCR garbling (rnessage, messaqe, etc.)
        if any(v in low for v in _SENTINEL_VARIANTS):
            self._route_text(text)

        # Step 2b: implementation authorization phrase (may appear without sentinel)
        if (self._mode == "module_block"
                and IMPL_TRIGGER_PHRASE.lower() in low):
            self._mode = "implementation"
            self.root.after(0, self._update_mode_indicator)
            self._log("[mode] ✓ IMPLEMENTATION MODE activated via OCR scan")

        # Step 3: [CMD: ...] hook for Bing disconnected-hand (disabled)
        self._parse_cmd_blocks(text)

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

    def _auto_locate_windows(self):
        """Find agent windows by matching saved title strings against open windows."""
        try:
            import win32gui
            live = []
            win32gui.EnumWindows(
                lambda hwnd, lst: lst.append((hwnd, win32gui.GetWindowText(hwnd)))
                    if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd)
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
        with mss.MSS() as sct:
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

    def _apply_template_match(self, stem: str, xy: tuple, conf: float):
        """Map a template filename stem to the right AgentConfig slot
        and update the training registry for that template."""
        for aid in ("agent1", "agent2"):
            if not stem.startswith(aid + "_"):
                continue
            role = stem[len(aid) + 1:]   # input / send / scroll_dn / scroll_up
            cfg  = self.agents[aid]
            x, y = xy

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
        self._log(f"[cal] unrecognized template name: {stem}\n"
                  "      Expected format:  agent1_input / agent1_send / "
                  "agent1_scroll_dn / agent1_scroll_up  (and agent2_*)")

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
        with mss.MSS() as sct:
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
        with mss.MSS() as sct:
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

        with mss.MSS() as sct:
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
        threading.Thread(
            target=self._inject_to_agent,
            args=("agent1", AGENT1_SOP),
            daemon=True).start()
        self._log("[mode] Agent1 SOP sent → awaiting 'Ready to plan project.'")
        self._set_status("Agent1 SOP sent")

    def _start_agent2(self):
        """Send Agent2 SOP prompt to Agent2's chat window."""
        if not self.agents["agent2"].hwnd:
            self._set_status("Agent 2 window not set — click Set Win after focusing it")
            return
        threading.Thread(
            target=self._inject_to_agent,
            args=("agent2", AGENT2_SOP),
            daemon=True).start()
        self._log("[mode] Agent2 SOP sent → awaiting 'Ready to save instruction blocks.'")
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


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    root = tk.Tk()
    app = SOCUltralight(root)
    root.mainloop()
