#!/usr/bin/env python3
"""SOC Ultralight — Transcript Monitor (standalone, read-only).

A lightweight window that TAILS the durable inter-agent transcript SOC writes
to  transcript/conversation_<date>.md  and renders it chat-style, colour-coded
by sender, auto-scrolling as new messages land.

Why standalone: the agents talk over ephemeral channels (OCR capture, clipboard,
outbox files that get archived). SOC core writes every routed/parsed message to
the transcript file; THIS is just a reader. It deliberately does NOT live inside
GGUF Chatbox (shared infrastructure) or the vision plugin (wrong category) — it
is decoupled and disposable. Run it beside SOC; close it any time.

    py -3 transcript_monitor.py

No third-party deps — stdlib tkinter only.
"""
import re
import time
import tkinter as tk
from pathlib import Path
from datetime import datetime

TRANSCRIPT_DIR = Path(__file__).parent / "transcript"
HEARTBEAT_FILE = Path(__file__).parent / ".soc_alive"
HEARTBEAT_STALE = 6.0   # secs without a SOC heartbeat -> SOC exited, self-close
POLL_MS = 800

BG   = "#1e1e1e"
FG   = "#d4d4d4"
HDR  = "#2d2d2d"
SENDER_COLOR = {
    "operator": "#4ec9b0",   # teal
    "agent1":   "#6a9955",   # green   (Bing Copilot / planner)
    "agent2":   "#ce9178",   # orange  (Claude Code / implementer)
    "agent3":   "#c586c0",   # purple  (orchestrator)
    "agent4":   "#569cd6",   # blue    (vision)
    "agent5":   "#dcdcaa",   # yellow  (long-context LLM)
}
_HDR_RE = re.compile(r"^### (\d\d:\d\d:\d\d)\s+(\S+)\s+→\s+(\S+)\s+\[(\w+)\]")


def latest_transcript() -> Path | None:
    """Newest conversation_*.md in the transcript dir, or None if none yet."""
    if not TRANSCRIPT_DIR.exists():
        return None
    files = sorted(TRANSCRIPT_DIR.glob("conversation_*.md"))
    return files[-1] if files else None


def tag_for_line(line: str) -> str | None:
    """Return a sender-colour tag name for a header line, else None."""
    m = _HDR_RE.match(line)
    if not m:
        return None
    return f"snd_{m.group(2)}"


class Monitor:
    def __init__(self):
        # Declare a distinct App ID BEFORE creating the window so Windows gives
        # this standalone process its OWN taskbar identity (otherwise it inherits
        # the shared pythonw icon regardless of iconbitmap).
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "Baxters.SOC.OutboxMonitor")
        except Exception:
            pass

        self.root = tk.Tk()
        self.root.title("SOC — Agent Transcript Monitor")
        # Taskbar/titlebar icon: the "OB" mark (replaces the default python icon).
        # Best-effort — a missing asset must never stop the monitor from opening.
        try:
            _ico = Path(__file__).resolve().parent / "assets" / "ob_icon.ico"
            if _ico.exists():
                self.root.iconbitmap(default=str(_ico))
        except Exception:
            pass
        self.root.configure(bg=BG)
        self.root.geometry("200x200")   # compact by default; drag-resize bigger anytime
        self.root.minsize(140, 140)

        bar = tk.Frame(self.root, bg=HDR)
        bar.pack(fill="x")
        self.status = tk.Label(bar, text="waiting for transcript…", bg=HDR,
                               fg=FG, font=("Consolas", 9), anchor="w", padx=8)
        self.status.pack(side="left", fill="x", expand=True)
        tk.Button(bar, text="⤓ bottom", command=self._jump_bottom, bg=HDR,
                  fg=FG, relief="flat", font=("Consolas", 9)).pack(side="right", padx=4)

        self.txt = tk.Text(self.root, bg=BG, fg=FG, insertbackground=FG,
                           font=("Consolas", 10), wrap="word", padx=10, pady=8,
                           borderwidth=0)
        self.txt.pack(fill="both", expand=True)
        sb = tk.Scrollbar(self.txt, command=self.txt.yview)
        sb.pack(side="right", fill="y")
        self.txt.config(yscrollcommand=sb.set, state="disabled")

        for sender, colour in SENDER_COLOR.items():
            self.txt.tag_config(f"snd_{sender}", foreground=colour,
                                font=("Consolas", 10, "bold"))
        self.txt.tag_config("snd_default", foreground=FG,
                            font=("Consolas", 10, "bold"))

        self._file: Path | None = None
        self._offset = 0
        self._soc_seen = False   # have we seen SOC alive? (gate the self-close)
        self.root.after(200, self._poll)

    def _jump_bottom(self):
        self.txt.see("end")

    def _at_bottom(self) -> bool:
        return self.txt.yview()[1] >= 0.999

    def _append(self, chunk: str):
        stick = self._at_bottom()
        self.txt.config(state="normal")
        for line in chunk.splitlines(keepends=True):
            tag = tag_for_line(line)
            self.txt.insert("end", line, (tag,) if tag else ())
        self.txt.config(state="disabled")
        if stick:
            self.txt.see("end")

    def _poll(self):
        # Self-close when SOC exits: its heartbeat stops (clean quit deletes the
        # file; a crash lets it go stale). Only after we've actually seen SOC alive,
        # so launching the monitor standalone doesn't immediately quit.
        try:
            if (HEARTBEAT_FILE.exists()
                    and time.time() - HEARTBEAT_FILE.stat().st_mtime <= HEARTBEAT_STALE):
                self._soc_seen = True
            elif self._soc_seen:
                self.root.destroy()
                return
        except Exception:
            pass
        try:
            newest = latest_transcript()
            if newest is None:
                self.status.config(text=f"waiting for transcript in {TRANSCRIPT_DIR}…")
            else:
                if newest != self._file:
                    # New day / new file — reset and load from the top.
                    self._file = newest
                    self._offset = 0
                    self.txt.config(state="normal")
                    self.txt.delete("1.0", "end")
                    self.txt.config(state="disabled")
                size = newest.stat().st_size
                if size < self._offset:        # file truncated/rotated
                    self._offset = 0
                if size > self._offset:
                    with open(newest, "r", encoding="utf-8", errors="replace") as fh:
                        fh.seek(self._offset)
                        chunk = fh.read()
                        self._offset = fh.tell()
                    self._append(chunk)
                self.status.config(
                    text=f"{newest.name}   ·   {self._offset} bytes   ·   "
                         f"updated {datetime.now().strftime('%H:%M:%S')}")
        except Exception as e:
            self.status.config(text=f"monitor error: {e}")
        self.root.after(POLL_MS, self._poll)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    Monitor().run()
