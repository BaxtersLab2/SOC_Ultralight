# SOC Ultralight — 3-Agent App-Building Framework

> Describe what you want to build. SOC Ultralight guides a team of three AI agents — planner, implementer, and debugger — from idea to finished application. No API keys. No cloud. Runs entirely on your local machine using the free tiers of Bing Copilot and Claude Code.

![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey?logo=windows)
![License](https://img.shields.io/badge/License-MIT-green)
![OCR](https://img.shields.io/badge/OCR-Tesseract-orange)

---

> **New to SOC Ultralight?** See [QUICKSTART.md](QUICKSTART.md) for a step-by-step guide to running your first dual-agent project, including the project summary template and the prompt to give Bing Copilot.

---

## The Vision

The long-term goal of SOC Ultralight is a **single prompt to a finished project** — write a detailed description of what you want to build (5+ pages covering every feature, data model, edge case, and integration), hand it to the 3-agent team, and receive a complete, tested, working application.

That goal is reachable today for well-scoped projects when the prompt is thorough enough. For larger or less-defined projects, the guided phase workflow below covers every step, handles scope ambiguity, and includes automated debugging at the end.

---

## What It Does

SOC Ultralight (Screen OCR Controller) is a lightweight Windows widget that orchestrates a team of three AI agents across your screen. It watches agent chat windows via OCR, intercepts protocol messages, and routes them automatically — no API keys, no cloud bridge, no code.

### The Agent Team

| Agent | Role | Powered by |
|---|---|---|
| **Agent 1** | Architect / Planner — refines the project description, designs the module structure, writes numbered instruction blocks, and delivers them one at a time | Bing Copilot (Edge) |
| **Agent 2** | Implementer — receives each block, stores it, confirms receipt, then implements all blocks in order when authorized | Claude Code (VS Code) |
| **Agent 3** *(optional)* | Second implementer for parallel workstreams or large projects | Claude Code (second VS Code) |
| **Claude** *(your session)* | Used at two key moments: polishing the project summary before it reaches Agent 1, and driving the Phase 3 debug loop | Claude Code CLI or claude.ai |

### The Guided Path

SOC guides you from idea to finished app through four phases:

```
Phase 1   →   Phase 1a   →   Phase 2   →   Phase 3
Calibrate     Define &        Automated     Automated
windows       refine          routing &     debug loop
              project         implement     with Claude
```

**Phase 1 — Calibrate**
Set window handles, auto-locate input fields and Send buttons via template matching, draw OCR regions. One-time setup, saved automatically.

**Phase 1a — Define the Project**
Brainstorm with Agent 1 or load an existing project summary. Claude reviews and improves the summary — tightening ambiguous requirements, flagging gaps, and making it precise enough for Agent 1 to chunk reliably. When the summary is approved, SOC sends it to Agent 1 with the module block template and routing protocol.

**Phase 2 — Build**
SOC takes over. Agent 1 writes numbered module instruction blocks and sends them one at a time. SOC routes each block to Agent 2 automatically. Agent 2 confirms receipt and replies. Once all blocks are delivered, Agent 1 sends the authorization phrase and Agent 2 implements everything in alphanumeric order.

**Phase 3 — Debug**
Click **🔬 Phase 3: Debug** at the bottom of the Phase 2 slide. Describe the issues you see. SOC assembles a debug SOP — project context, git log, your issue list — and saves it to `staging/phase3_debug_sop.md`. Drag the file into Claude's chat to begin the debug session. Claude and you ping-pong through the issue list until every bug is resolved.

> Phase 3 is quarantined from Agents 1 and 2 until you deliberately start it. Agents only see content injected into their chat windows — they never browse your file system.

---

## Requirements

### Python 3.12 or later
Download from [python.org/downloads](https://www.python.org/downloads/)

> **Important:** During install, check **"Add Python to PATH"**.

### Tesseract OCR
Required for the OCR message watcher.

1. Download the installer from [github.com/UB-Mannheim/tesseract/wiki](https://github.com/UB-Mannheim/tesseract/wiki)
2. Install to the default path: `C:\Program Files\Tesseract-OCR\tesseract.exe`

---

## Requirements

### Python 3.12 or later
Download from [python.org/downloads](https://www.python.org/downloads/)

> **Important:** During install, check **"Add Python to PATH"**.

### Tesseract OCR
Required for the OCR message watcher.

1. Download the installer from [github.com/UB-Mannheim/tesseract/wiki](https://github.com/UB-Mannheim/tesseract/wiki)
2. Install to the default path: `C:\Program Files\Tesseract-OCR\tesseract.exe`

---

## Installation

```
git clone https://github.com/BaxtersLab2/SOC_Ultralight.git
cd SOC_Ultralight
```

No manual `pip install` needed — `run.bat` installs dependencies automatically.

---

## Launching SOC Ultralight

Double-click **`run.bat`** in the SOC Ultralight folder.

The widget opens as a compact dark panel pinned to your screen. It does not appear in the taskbar.

> If the app fails to launch, run `python soc_ultralight.py` in a terminal to see the error.

---

## First-Time Setup

Do this once. All settings are saved automatically to `config.json`.

### Step 1 — Set the Agent Windows

For each agent (Agent 1 and Agent 2):

1. Click **Set Win** next to the agent.
2. Switch to that agent's window within **5 seconds** — a countdown is shown in the status bar.
3. SOC captures the window handle and title. You will see it confirmed in the log.

> No keyboard needed — the capture is timer-based.

### Step 2 — Auto-Calibrate

Click **⌖ Cal**.

SOC screenshots the screen and uses OpenCV template matching to locate the input fields, Send buttons, and scroll buttons for each agent. Matched coordinates are saved automatically.

Check the log — lines marked `★trained` show successful matches. Lines marked `outside window` or `no match` mean that button template was not found on screen.

> Make sure both agent windows are **visible on screen** before running Cal.

### Step 3 — Set OCR Regions

For each agent that sends messages (usually both agents):

1. Click **⎕ Region** next to the agent.
2. A dark overlay appears. **Click and drag** a rectangle over the area of that agent's window that shows **outgoing messages** (the chat output area, not the input box).
3. Release the mouse to confirm.

> Make the region tight — just the chat output area. Exclude the input box, browser chrome, and other UI.

### Step 4 — Verify Coordinates

After Cal, you can check the captured coordinates shown under each agent. If a coordinate looks wrong (e.g. a button outside the window), click **⊙ Input** or **⊙ Send** to manually hover and capture it:

1. Click the capture button.
2. Hover your mouse over the target (input field or Send button).
3. Hold still — SOC captures after a countdown.

---

## Phase 1a — Project Priming

After calibration is complete, clicking **→ Launch Workflow** opens the Project Priming slide. This is where you define the project before any module blocks are written. Phase 2 (automated routing) does not start until you click **→ Begin Workflow** at the bottom of this slide.

### Option A — Brainstorm from scratch

Click **▶ Brainstorm**.

SOC injects a starter prompt into Agent 1 asking it to help you design the project. You describe what you want to build in the Copilot window. Agent 1 asks clarifying questions, refines the scope, and produces a complete project summary when you are satisfied.

### Option B — Load an existing summary

Click **Browse…** and select a `.md` or `.txt` file containing your project summary.

Click **→ Inject Summary into Agent 1**.

SOC reads the file and sends it to Agent 1 with framing that asks Agent 1 to confirm the scope and flag any loose ends before proceeding.

### Mark the summary ready

Once Agent 1 has a complete project summary (whether from brainstorm or file), click **✓ Summary Ready**. The button turns green and the template step unlocks.

### Send the module block template

Click **→ Send Template to Agent 1**.

SOC reads `templates/GENERAL_MODULE_BLOCK_TEMPLATE.md` and sends it to Agent 1 along with the relay protocol rules:

- Deliver each block in `To Agent2 / content / end message now` format.
- Wait for Agent 2's confirmation before sending the next block.
- Send the authorization phrase when all blocks are delivered.

The button turns green when sent.

### Begin the workflow

Click **→ Begin Workflow** (enabled only after both steps above are green).

This advances to Phase 2 where SOC takes over routing automatically. See [The Module Block Workflow](#the-module-block-workflow) below.

> **Returning to a project mid-session?** On startup SOC skips Phase 1a and lands directly in Phase 2 if your window calibration is already saved. Use **▶ Agent 1 SOP** and **▶ Agent 2 SOP** from Phase 2 to re-sync agents if needed.

---

## Message Protocol

All agent-to-agent messages must use this exact 3-line format:

```
To Agent2
<message body here>
end message now
```

- **Line 1** — Routing header. Must be `To Agent1` or `To Agent2` (case-insensitive, number can be digit or OCR variant).
- **Middle** — Message body. Any text, any number of lines.
- **Last line** — Sentinel. Must be exactly `end message now`.

SOC only routes a message **after the sentinel appears on screen**, preventing partial captures while text is still streaming.

> OCR sometimes misreads `1` as `l`, `i`, or `|`. SOC automatically normalises these variants — `To Agentl` and `To Agent1` are treated identically.

---

## The Module Block Workflow

### Overview

This phase begins after Phase 1a is complete and **→ Begin Workflow** has been clicked.

Agent 1 (Bing Copilot) plans a project and breaks it into numbered module blocks. It sends each block to Agent 2 one at a time. Agent 2 saves each block and confirms receipt. Once all blocks are delivered, Agent 1 authorizes implementation. Agent 2 implements all blocks in alphanumeric order and reports completion.

### Setup: Send the SOPs

Before starting, each agent needs its operating instructions:

1. Click **▶ Agent 2 SOP** — sends the strict protocol instructions to Agent 2 (Claude Code). Agent 2 will learn its exact reply format and rules.
2. Click **▶ Agent 1 SOP** — sends the workflow structure template to Agent 1 (Bing Copilot). Agent 1 will learn the module block format and delivery process.

> Send Agent 2's SOP first so it is ready before Agent 1 begins delivering blocks.

### Start OCR

Click **▶ Start OCR**.

The button turns red and the status changes to `OCR: scanning…`. SOC is now watching both agent windows.

### The Loop

Once both SOPs are delivered and OCR is running:

1. **Agent 1** writes a module block addressed to Agent 2 and ends it with `end message now`.
2. **SOC** detects it in Agent 1's OCR region, routes it to Agent 2's input field, and clicks Send.
3. **Agent 2** saves the block and replies:
   ```
   To Agent1
   module block A-1 saved, ready for next block
   end message now
   ```
4. **SOC** detects Agent 2's reply in Agent 2's OCR region and routes it back to Agent 1.
5. **Agent 1** sends the next block. Repeat until all blocks are delivered.

### Ending the Block Phase

When all blocks are sent, Agent 1 sends the implementation trigger phrase:

```
To Agent2
that is the final block you may begin implementation in alphanumeric order now
end message now
```

SOC detects this phrase, switches to **IMPLEMENTATION MODE**, and routes the message to Agent 2.

### Implementation Phase

Agent 2 implements all stored blocks in alphanumeric order (A1, A2, B1, etc.). If it hits a blocker, it replies:

```
To Agent1
PROBLEM: <one sentence>
QUESTION: <what it needs>
end message now
```

When implementation is complete, Agent 2 replies:

```
To Agent1
implementation of instruction blocks is complete
end message now
```

SOC detects this phrase and switches back to **MODULE BLOCK MODE**.

---

## Phase 3 — Automated Debug Loop

When implementation is done (or when you hit a wall mid-build), click **🔬 Phase 3: Debug** at the bottom of the Phase 2 slide.

### What it does

1. A dialog opens. Type a numbered list of every bug, broken feature, or unexpected behaviour you want fixed.
2. Click **Prepare Debug File**.
3. SOC assembles a complete debug SOP containing:
   - Your workspace path and project name
   - The last 20 git commits
   - Your issue list, numbered and formatted for action
4. The SOP is saved to `staging/phase3_debug_sop.md` and opened in VS Code automatically.

### Running the debug session

Drag `phase3_debug_sop.md` from the VS Code file explorer into Claude's chat input field (or paste its contents). Claude reads the context and begins working through the issue list.

The session is a **ping-pong loop**:

```
You (report issues)  →  Claude (diagnoses, fixes, asks questions)
                     ←  You (confirm fixes or surface new issues)
                     →  Claude (next fix) ...
```

Repeat until all items on your list are resolved. Claude carries full project context across the session — it can read files, run the test suite, check git history, and propose targeted fixes without you needing to re-explain the codebase.

> **Note:** Phase 3 has limitations. It works best for bugs that are isolated to specific files or clear logic errors. For large architectural changes, go back to Phase 1a, refine the project summary, and rebuild the affected modules through Phase 2.

### Quarantine

The SOP file lives in `staging/` inside the SOC Ultralight source folder. This means:
- It is included in source backups and git history if you commit it.
- Agents 1 and 2 never see it — they only receive content that SOC explicitly injects into their chat windows. No agent browses your file system.
- You control exactly when Phase 3 begins by choosing when to drag the file into Claude.

---

## Hold State

After routing a message to an agent, SOC **holds** — it pauses routing and waits for that agent to reply before sending the next message. This prevents message pile-ups.

- The OCR status label shows `OCR: ⏸ waiting agentX…` and the **↺** button turns red when holding.
- When the destination agent replies (SOC sees a message addressed to the *other* agent), hold is automatically released.
- **Hold timeout:** If no reply arrives within 60 seconds, the hold releases. Click **↺** to allow the same message to be re-routed; otherwise SOC waits for new content.
- **Manual release:** Click the **↺** button at any time to clear the hold and any same-body dedup block.

### Per-Agent Hold Buttons (⏸ Hold A1 / ⏸ Hold A2)

Two manual gate buttons sit below the OCR row. Each one blocks routing **to** that agent until you release it.

**One-shot behaviour:** the hold is not sticky. As soon as one message successfully routes to either agent, both holds automatically release and the ping pong resumes on its own.

**Primary use case — re-entering a workflow:**

When you restart OCR mid-session, both agents may have outgoing messages visible at the same time. Without intervention, SOC reads whichever it sees first and creates a double-fire.

1. Start OCR.
2. Immediately click **⏸ Hold A2** (or whichever side you want to pause).
3. Read both agents' pending messages. Pick the better/more correct one to send first.
4. If the one you want is addressed TO Agent 2 — click **▶ Resume A2**. SOC routes it and both holds drop automatically.
5. If you want Agent 2's reply to reach Agent 1 first — leave A2 held, click **⏸ Hold A1**, then release A1. Agent 2's message routes, both holds clear, and the sequence continues.

**During a normal session:**

If agents get out of sync (Agent 1 sends a correction while Agent 2 is mid-reply), click the hold button for the side you want to pause, let the priority message through, and the sequence self-corrects. Agent 1 can include alignment instructions in its correction, which Agent 2 will receive on the next routed message.

| Button state | Meaning |
|---|---|
| `⏸ Hold A1` (normal) | Idle — routing to Agent 1 is open |
| `▶ Resume A1` (red) | Held — routing to Agent 1 is blocked |

---

## Mode Indicator

The mode indicator at the top of the widget shows the current routing mode:

| Display | Meaning |
|---|---|
| `MODULE BLOCK MODE` | Normal block delivery phase. Messages to Agent 2 get a `<Module Block Mode Active>` header prepended. Implementation commands are blocked unless the exact trigger phrase is used. |
| `IMPLEMENTATION MODE` | Agent 2 is implementing blocks. Implementation commands are allowed through. |

Click the mode indicator to toggle manually if the agents get out of sync.

---

## Anti-Drift Reminders

Every **5th message** sent to Agent 2 and every **10th message** sent to Agent 1 automatically includes a protocol reset reminder. This keeps agents on-role during long sessions.

- Reminders are prepended to the next real message — no extra injection.
- Reminders are suppressed during hold-timeout retries to avoid confusing agents.
- You can see reminders in the log: `[recal] role reminder injected to agentX`.

---

## OCR Controls

| Button / Label | Action |
|---|---|
| `▶ Start OCR` | Starts the OCR watcher. Scans every 1.5 s normally, 0.3 s in rapid mode. |
| `■ Stop OCR` | Stops the watcher. |
| `OCR: scanning…` | Normal — both windows being scanned. |
| `OCR: RAPID ⚡` | Rapid scan active (triggered by seeing `To Agent` on screen). |
| `OCR: ⏸ waiting agentX…` | Hold active — waiting for that agent to reply. |
| `↺ Release` | Release hold manually. Clears the hold and any same-body dedup block so the message can re-route. |
| `⏸ Hold A1` | Block routing to Agent 1 (one-shot gate — auto-releases after the next successful send). |
| `⏸ Hold A2` | Block routing to Agent 2 (one-shot gate — auto-releases after the next successful send). |
| `⏸ Pause` | Pause all routing globally. OCR keeps scanning but nothing injects. Click **▶ Resume** to resume — body-match guards are cleared on resume so the agents' current window content routes fresh. |
| `⟳ Welfare` | Send a re-sync prompt to both agents showing the last message sent and received. Fires automatically after 2 minutes of no pixel activity in the active OCR region. |

---

## Other Controls

| Button / Field | Action |
|---|---|
| `⌖ Cal` | Run auto-calibration using template matching. |
| `▶ Agent 1 SOP` | Send the Agent 1 workflow SOP (from `agent1 soc ultralight .txt`). |
| `▶ Agent 2 SOP` | Send the Agent 2 protocol SOP (from `agent 2 soc ultralight.txt`). |
| `▶ Outbox` | Start the file outbox watcher (`outbox/agent1/`, `outbox/agent2/`). |
| `⚡ VS Code` | One-click: starts outbox watcher + auto-click scan. |
| `🔵 Bing` | Enable Bing Copilot–aware injection timing (double-click focus, send button polling). |
| `Project:` field | Type the active project name. SOC prepends `[ACTIVE PROJECT: name]` to every message sent to Agent 1 as an anti-drift reminder. Saved automatically. |
| `—` (title bar) | Minimize the widget. |
| `X` (title bar) | Quit. |

---

## Auto-Click

SOC can automatically click on-screen buttons (e.g. VS Code "Allow", "Keep All Changes", approval dialogs) using OpenCV template matching.

### Enabling Auto-Click

1. Click **▶ Auto-Click** at the bottom of the widget to expand the panel.
2. Check the **auto** checkbox next to any template you want auto-clicked.

### Adding a New Template

1. Take a screenshot crop of the button (PNG, native resolution).
2. Save it to the `buttons database/` folder with a descriptive name.
3. Click **↺ Refresh** in the Auto-Click panel — the new template appears.
4. Click **Train** to let SOC capture the exact click position: SOC hides, you click the real button within 15 s, SOC restores and saves.
5. Enable the **auto** checkbox.

> Templates marked 🔒 are routing infrastructure (input fields, send buttons) — they cannot be toggled.

---

## Diagnostics

The **▼ Diagnostics** drawer at the bottom shows all events in real time.

| Log prefix | Meaning |
|---|---|
| `[ocr:agentX]` | OCR scan result for that agent's window. `trigger=YES` means `To AgentX` was seen. `sentinel=YES` means `end message now` was also seen. |
| `[→agentX]` | Message successfully routed and injected into that agent. |
| `[ocr]` | Hold state changes, timeouts, and directional skip events. |
| `[recal]` | Anti-drift reminder prepended to a message. |
| `[cal]` | Auto-calibration match results. |
| `[auto-click]` | Template matched and clicked. |
| `[mode]` | Mode switch (MODULE BLOCK ↔ IMPLEMENTATION). |

Click **Copy All** to copy the full log to clipboard.

---

## Troubleshooting

**App does not start**  
Run `python soc_ultralight.py` in a terminal. Read the error. Most common causes: Python not on PATH, Tesseract not installed at the default path, missing package.

**OCR sees the message but does not route it**  
- Check the log for `[ocr:agentX] trigger=YES sentinel=YES hold=none` followed by `[dedup] body matches last sent` — the message body is identical to the last one routed. Click **↺ Release** to clear the block and allow it to re-send. This typically happens after a hold timeout where the same message is still on screen.
- If `trigger=no sentinel=YES`, the `To Agent1/2` header is visible but not matching. Check that the message uses the correct format (header on its own line).
- If `trigger=YES sentinel=no`, the `end message now` line is not yet visible — wait for the agent to finish generating.

**OCR sees nothing**  
- Make sure the OCR region (⎕ Region) covers the correct output area of the agent's window.
- Make sure both agent windows are on-screen (not minimised or off-screen).
- Check that Tesseract is installed at `C:\Program Files\Tesseract-OCR\tesseract.exe`.

**Messages route but Send is not clicked**  
- Re-run ⌖ Cal, or manually re-capture ⊙ Send for the affected agent.
- Make sure the agent window is visible and not behind other windows when SOC tries to inject.

**Agent 1 (Bing Copilot) injection problems**  
- Enable **🔵 Bing** mode. This adds a double-click for contenteditable focus and polls for the Send button to appear after paste (up to 6 s).
- The window handle goes stale if the browser tab is closed/reopened — click **Set Win** again.

**Agent 2 (Claude Code) window not captured**  
- VS Code uses GPU-accelerated rendering. SOC uses `PIL.ImageGrab` (GDI capture) for VS Code windows which works correctly. If OCR shows nothing from the VS Code window, check that the OCR region is set correctly.

**Mode is wrong (agents out of sync)**  
- Click the mode indicator label to toggle the mode manually.
- Re-send the SOPs using **▶ Agent 1 SOP** and **▶ Agent 2 SOP** to re-align both agents.

**Failsafe**  
Move your mouse to the **top-left corner** of the screen at any time to stop all pyautogui actions immediately.

---

## Project Structure

```
SOC_Ultralight/
├── soc_ultralight.py               Main application
├── run.bat                         Launch script (installs deps + starts app)
├── requirements.txt                Python dependencies
├── config.json                     Auto-saved window handles and coordinates (git-ignored)
├── agent1 soc ultralight .txt      Agent 1 SOP — workflow structure template
├── agent 2 soc ultralight.txt      Agent 2 SOP — strict protocol instructions
├── templates/                      Phase 1a project priming files
│   ├── PROJECT_SUMMARY_TEMPLATE.md     Fill this in or load it via Browse in Phase 1a
│   └── GENERAL_MODULE_BLOCK_TEMPLATE.md  Block format sent to Agent 1 by Phase 1a
├── docs/
│   └── images/                     Desktop layout screenshots (see Desktop Layout section)
├── buttons database/               PNG templates for auto-click and calibration
│   └── registry.json               Template confidence training data
├── outbox/                         Drop .md files here for routing (git-ignored)
│   ├── agent1/
│   └── agent2/
├── sent/                           Processed outbox files archived here (git-ignored)
└── staging/                        Phase 3 debug SOP output (quarantined from agents)
    └── phase3_debug_sop.md             Generated by Phase 3 — drag into Claude to start debug session
```

---

## Desktop Layout

SOC Ultralight shares your screen with up to three chat windows and a small folder explorer — arranging them correctly before starting saves a lot of pain. The images below show the recommended layout for each configuration.

**Suggested filenames** (place your screenshots in `docs/images/`):

| File | Contents |
|---|---|
| `desktop_layout_2agent.png` | 2-agent setup: Agent 1 + Agent 2 side by side, SOC widget, folder explorer showing `run.bat` |
| `desktop_layout_3agent.png` | 3-agent setup: Agent 1, Agent 2, Agent 3 slim side by side, SOC widget, folder explorer |

### 2-Agent Layout

![2-Agent Desktop Layout](docs/images/desktop_layout_2agent.png)

### 3-Agent Layout

![3-Agent Desktop Layout](docs/images/desktop_layout_3agent.png)

**Tips:**
- Slim each chat window so all fit side by side — Agent 1 on the left, Agent 2 (and Agent 3) on the right
- Open the folder explorer at the project workspace root so `run.bat` is visible and clickable
- SOC Ultralight sits on top of everything (`overrideredirect` — always on top); dock it to a corner before calibrating
- Run calibration (**Phase 1 → Cal A1 / Cal A2**) with each chat window in its final position — coordinates are saved and reused on restart

---

## Key Constants (top of `soc_ultralight.py`)

| Constant | Default | Purpose |
|---|---|---|
| `SCAN_NORMAL` | `1.5 s` | Seconds between OCR scans |
| `SCAN_RAPID` | `0.3 s` | Rapid scan rate after `To Agent` spotted |
| `RAPID_DURATION` | `8.0 s` | How long rapid mode stays active |
| `WAIT_REPLY_TIMEOUT` | `180.0 s` | Hold timeout before re-sending (3 min, sized for large blocks) |
| `REMINDER_EVERY_AGENT1` | `10` | Agent 1 reminder interval (every N messages) |
| `REMINDER_EVERY_AGENT2` | `5` | Agent 2 reminder interval (every N messages) |
| `TEMPLATE_THRESH` | `0.80` | OpenCV match confidence threshold |
| `MAX_INJECT_CHARS` | `8000` | Message truncation limit |

---

## Dependencies

| Package | Purpose |
|---|---|
| `pyautogui` | Mouse/keyboard automation |
| `pyperclip` | Clipboard paste |
| `pywin32` | Window focus and handle capture |
| `mss` | Fast screen capture (fallback) |
| `Pillow` | Image processing + GDI screen capture |
| `pytesseract` | Tesseract OCR wrapper |
| `opencv-python` | Auto-click template matching |
| `numpy` | Array support for OpenCV |
| Tesseract OCR | OCR engine (installed separately) |

---

## Licence

MIT License — Copyright (c) 2026 BaxtersLab2

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions: The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED.
