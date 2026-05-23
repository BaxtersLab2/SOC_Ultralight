# SOC Ultralight — Instructions
### Agent Message Router + OCR Watcher

SOC Ultralight sits in the corner of your screen and acts as a **silent relay** between AI chat agents. It reads messages that one agent writes on screen (via OCR or outbox files), strips routing headers, and pastes them directly into another agent's chat input — then clicks Send automatically.

No vision LLM required. No API keys. Runs entirely on your local machine.

---

## Requirements

| Dependency | Notes |
|---|---|
| Python 3.12+ | Install from https://www.python.org/downloads/ |
| Tesseract OCR | Install from https://github.com/UB-Mannheim/tesseract/wiki — default path `C:\Program Files\Tesseract-OCR\tesseract.exe` |
| `pyautogui`, `pyperclip`, `mss`, `Pillow`, `pytesseract` | `pip install -r requirements.txt` |
| `opencv-python`, `numpy` | Required for Auto-Click template matching (`pip install opencv-python numpy`) |
| `pywin32` | Required for window focus (`pip install pywin32`) |

---

## Launching

Double-click **SOC Ultralight.vbs** on the Desktop.  
The widget opens as a compact dark panel. It does not appear in the taskbar.

---

## UI Overview

```
┌─────────────────────────────────┐
│ ▶ Start OCR    OCR: OFF         │  ← Row 1: OCR controls
│ ▶ Outbox  ⌖ Cal  ⚡ VS Code  🔵 Bing │  ← Row 2: Workflow controls
│ Inject: [__________________][Send] │  ← Manual inject bar
│                                 │
│ Agent 1  window: (not set)  [Set Win] │
│          input field: (not set) [⊙ Input] │
│          send button: (not set) [⊙ Send]  │
│          Prefix: [Ignore Edge browser...] │
│          scroll: (not set) [⊙↑][⊙↓][Read]│
│          ocr region: (not set)  [⎕ Region]│
│ ────────────────────────────── │
│ Agent 2  ...same rows...        │
│ ────────────────────────────── │
│ Agent 3  ...same rows...        │
│ ────────────────────────────── │
│ ▼ Diagnostics           [Copy All] │
│ [log output...]                 │
│ status bar                      │
│ ▶ Auto-Click ↺ ⌖ [panel...]    │
└─────────────────────────────────┘
```

---

## Agent Roles

| Agent | Who | Tool |
|---|---|---|
| **Agent 1** | Senior Software Engineer (30 yrs) | Bing Copilot in Edge browser |
| **Agent 2** | Development Project Manager | GitHub Copilot (VS Code chat) |
| **Agent 3** | Senior Coding Advisor | Claude Code |

---

## First-Time Setup (per agent)

Do this once. Settings are saved to `config.json` automatically.

### 1. Set Window
Click **Set Win** next to the agent. Switch to that agent's window within 5 seconds. SOC captures its window handle and title.

### 2. Set Input Field
Click **⊙ Input**. Hover your mouse over the agent's **chat text input box**. Hold still — SOC captures the XY coordinates after a countdown.

### 3. Set Send Button
Click **⊙ Send**. Hover over the agent's **Send / Submit button**. Hold still for the countdown.

### 4. Set OCR Region (for source agents)
Click **⎕ Region**. A dark overlay covers your screen. **Click and drag** a rectangle around the area of the agent's window that shows its **outgoing messages**. This is the zone OCR watches for routing headers.

> Tip: make the region just big enough to capture the last few lines of the chat output — don't select the input box or browser chrome.

### 5. Set Scroll (optional, for long messages)
Click **Read** to trigger a scroll-read session (SOC scrolls down and OCR-reads until it finds a complete message). Set **⊙↑** and **⊙↓** for manual scroll button positions.

### 6. Auto-Calibrate
Click **⌖ Cal** to run template matching against the `buttons database/` folder. SOC will screenshot the screen, find button templates, and auto-fill input/send coordinates.

---

## Message Protocol

Agents communicate using this 3-line format:

```
To Agent2
<message content here>
end message now
```

- **Line 1** — routing header. Can be `To Agent1`, `To Agent2`, or `To Agent3` (case-insensitive, spaces optional).
- **Middle** — message body. Any text. Quotes are stripped automatically.
- **Last line** — sentinel. Must be exactly `end message now`.

The sentinel prevents partial captures while text is still streaming onto screen. SOC only routes after the sentinel is visible.

**Fallback (single-line):**
```
To Agent2: please implement the login function
```

---

## How SOC Routes Messages

SOC has three independent pipelines:

### A — OCR Watcher (`▶ Start OCR`)
- Tesseract scans the configured OCR region of each agent's window
- Normal scan: every **1.5 seconds**
- Rapid scan: every **0.3 seconds** — triggered the moment `to agent` appears on screen, stays rapid for 8 seconds
- When the full protocol (header + body + sentinel) is detected, SOC routes and injects it

### B — File Outbox (`▶ Outbox`)
- Polls `outbox/agent1/`, `outbox/agent2/`, `outbox/agent3/` every 0.5 seconds
- When a `.md` file appears, SOC reads it, injects the contents into the target agent, and moves the file to `sent/`
- Agents can write outbox files directly (e.g. Claude Code writing to `outbox/agent2/`)

### C — Manual Inject
- Type `to agent1: your message here` in the **Inject** bar and press Enter (or click Send)
- Useful for testing, kickstarting a conversation, or manual override

---

## Two-Agent Workflow: Agent 2 ↔ Agent 3

### Starting the VS Code Workflow
Click **⚡ VS Code**.

This simultaneously:
1. Starts the **Outbox watcher** (▶ Outbox goes red = active)
2. Starts the **Auto-Click scan** (template matching for approval buttons)
3. Sends an initial briefing to Agent 3 (Claude Code) in `outbox/agent3/`

Button turns **green + filled** (■ VS Code) when active. Click again to stop.

**How it works in practice:**
- Agent 3 (Claude Code) plans the next step and writes a `.md` file to `outbox/agent2/`
- SOC detects the file, injects it into Agent 2 (GitHub Copilot), clicks Send
- Agent 2 implements and replies using `To Agent3 ... end message now`
- OCR picks it up, routes back to Agent 3

**Agent 3 reminder cadence:**  
Every 5th message injected to Agent 3 includes a brief role reminder to prevent drift.

---

## Three-Agent Workflow: Add Agent 1 (Bing)

### Starting Bing Mode
Click **🔵 Bing**.

This activates Agent 1 (Bing Copilot in Edge) with Edge-browser-aware injection:

| Message # | What Agent 1 receives |
|---|---|
| 1, 2, 3, 4 | `Ignore Edge browser metadata noise.` prepended to the message |
| 5 (every 5th) | Full role recalibration: identity, communication protocol, escalation rules |

**Outbound stripping:** When Agent 1's OCR output is read and routed to Agent 2 or Agent 3, any Edge browser prefix noise is automatically stripped before delivery.

Button turns **blue + filled** (■ Bing) when active. Click again to stop.

### Running Both Modes Together
You can have both **⚡ VS Code** and **🔵 Bing** active at the same time. This creates a three-agent loop:

```
Agent 3 (Claude Code) — Senior Advisor
       ↓ plans & instructs (outbox/agent2/)
Agent 2 (Copilot) — Project Manager
       ↓ directs implementation (To Agent1 ... end message now)
Agent 1 (Bing) — Senior Engineer
       ↓ implements + reports back (To Agent2 ... end message now)
Agent 2 — reviews, escalates to Agent 3 if needed
```

---

## Anti-Drift Recalibration

Every **5th message** sent to an agent includes an automatic role reminder. This prevents LLMs from drifting out of their role over long sessions.

| Agent | Reminder content |
|---|---|
| Agent 1 | Full system recalibration — identity, comms protocol, escalation rules |
| Agent 2 | Full system recalibration — identity, comms protocol, problem-handling |
| Agent 3 | Brief 7-line reminder — role, outbox format, wait-for-reply rule |

This is silent — no UI notification. You can see it in the Diagnostics log: `[recal] role reminder injected`.

---

## Auto-Click Panel

Click **▶ Auto-Click** at the bottom of the widget to expand the panel.

SOC can automatically click buttons (e.g. "Approve", "Run", confirmation dialogs) using screenshot template matching.

### Adding Templates
1. Take a screenshot crop of the button you want SOC to click
2. Save the PNG to `buttons database/` with a descriptive name (e.g. `approve_run.png`)
3. Click **↺ Refresh** in the panel — the new template appears in the list

### Training a Template
For new templates, SOC needs to know the precise click position:
1. Click **Train** next to the template row
2. SOC minimises the widget
3. Click the actual button on screen within 15 seconds
4. SOC captures the region, saves it, restores the widget

### Enabling Auto-Click
Check the **auto** checkbox next to a template row to enable automatic clicking.

Locked rows (🔒) — `input_field`, `send_message`, `_scroll` — are routing infrastructure used internally. They cannot be toggled.

### ⌖ Calibrate
Click **⌖ Cal** to run auto-calibration: SOC screenshots the screen and matches all templates in `buttons database/`. Matched coordinates are saved to `config.json`.

---

## Diagnostics Log

The **▼ Diagnostics** drawer at the bottom shows all routing events in real time:

| Prefix | Meaning |
|---|---|
| `[ocr]` | OCR scan events |
| `[router]` | Message routing to an agent |
| `[outbox]` | File watcher events |
| `[recal]` | Anti-drift reminder injected |
| `[inject]` | Injection errors or truncations |
| `[autoclick]` | Template match + click events |
| `[VS Code mode]` | VS Code workflow start/stop |
| `[Bing mode]` | Bing workflow start/stop |

Click **Copy All** to copy the full log. The log is capped at 500 lines.

---

## Folder Structure

```
SOC Ultralight/
├── soc_ultralight.py       Main application
├── config.json             Auto-saved coordinates and window titles
├── SOC Ultralight.vbs      Launcher (on Desktop)
├── buttons database/       Drop PNG crops here for auto-click templates
│   └── registry.json       Template match history
├── outbox/
│   ├── agent1/             Agent 1 inbound queue (write .md files here)
│   ├── agent2/             Agent 2 inbound queue
│   └── agent3/             Agent 3 inbound queue
└── sent/
    ├── agent1/             Processed files archived here
    ├── agent2/
    └── agent3/
```

---

## Safety

- **Failsafe:** Move your mouse to the **top-left corner** of the screen at any time to stop all pyautogui actions immediately.
- **Max message size:** Messages longer than 8,000 characters are truncated before injection.
- **Duplicate guard:** A rolling 300-entry dedup window prevents the same message being injected twice (e.g. if OCR re-reads a message already on screen).
- **Outbox move:** Processed outbox files are moved to `sent/` — not deleted — so you have a full audit trail.

---

## Troubleshooting

**App won't launch**  
Run directly: `python soc_ultralight.py` in the SOC Ultralight folder and read the error.

**OCR isn't picking up messages**  
- Check that the OCR region (⎕ Region) covers the correct output area  
- Make sure the message follows the exact protocol: `To AgentX` on its own line, `end message now` on its own line  
- Check the Diagnostics log for `[ocr]` entries  

**Messages routed but Send not clicking**  
- Re-capture the ⊙ Send coordinate — the button may have moved  
- Try ⌖ Cal to auto-calibrate from screenshots  
- Check that the agent window is not minimised  

**Bing (Agent 1) not receiving messages**  
- Ensure `Set Win` was done while the Edge/Bing window was focused  
- The window handle is stale if the browser tab was closed/reopened — click Set Win again  

**Template matching not finding buttons**  
- Make sure `opencv-python` is installed  
- Re-crop the template PNG at native resolution (no scaling)  
- Lower `TEMPLATE_THRESH` in `soc_ultralight.py` (default 0.80) if matches are being missed  
