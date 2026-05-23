# **SOC Ultralight >🤖↔️🤖)**

> A lightweight desktop widget that silently routes messages between AI chat agents — no vision LLM, no API keys, no cloud. Runs entirely on your local machine.

![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey?logo=windows)
![License](https://img.shields.io/badge/License-MIT-green)
![OCR](https://img.shields.io/badge/OCR-Tesseract-orange)

---

## What It Does

**SOC Ultralight** (Screen OCR Controller) sits in the corner of your screen and acts as a silent relay between AI chat agents. It reads messages that one agent writes on screen via OCR or file outbox, strips routing headers, and pastes them directly into another agent's chat input — then clicks Send automatically.

| Feature | Detail |
|---|---|
| Up to 3 agents | Bing Copilot, GitHub Copilot, Claude Code — or any chat window |
| Three routing pipelines | OCR watcher, file outbox, manual inject |
| Auto-click | OpenCV template matching fires Send/Allow buttons automatically |
| No API keys | Everything happens through the screen — no integrations required |
| Compact widget | Stays out of the way; does not appear in the taskbar |

---

## Agent Roles (typical setup)

| Agent | Who | Role |
|---|---|---|
| **Agent 1** | Bing Copilot (Edge browser) | Senior Software Engineer — implements tasks in VS Code |
| **Agent 2** | GitHub Copilot (VS Code Chat) | Development Project Manager — directs Agent 1, reviews output |
| **Agent 3** | Claude Code | Senior Coding Advisor — high-level planning, directs Agent 2 |

Any chat-based AI tool can be used in any slot — the roles are just a starting point.

---

## Requirements

### Python
- **Python 3.12 or later** — [python.org/downloads](https://www.python.org/downloads/)

### Tesseract OCR
Required for the OCR watcher pipeline.

1. Download the installer: https://github.com/UB-Mannheim/tesseract/wiki
2. Install to the default path: `C:\Program Files\Tesseract-OCR\tesseract.exe`

### Python packages (auto-installed)
```
pip install -r requirements.txt
```

| Package | Purpose |
|---|---|
| `pyautogui` | Mouse/keyboard automation |
| `pyperclip` | Clipboard paste |
| `pywin32` | Window focus and handle capture |
| `mss` | Fast screen capture |
| `Pillow` | Image processing |
| `pytesseract` | Tesseract OCR wrapper |
| `opencv-python` | Auto-click template matching |
| `numpy` | Array support for OpenCV |

---

## Installation

```
git clone https://github.com/BaxtersLab2/SOC_Ultralight.git
cd SOC_Ultralight
pip install -r requirements.txt
```

Then launch:
```
run.bat
```

Or double-click `SOC Ultralight.vbs` on the Desktop (no terminal window).

---

## How to Use

### First-Time Setup (do once per agent)

Settings are saved to `config.json` automatically.

| Step | What to do |
|---|---|
| **Set Win** | Click Set Win, switch to the agent window within 5 seconds — SOC captures its handle. |
| **⊙ Input** | Click, hover over the agent's chat text input box, hold still — SOC captures XY. |
| **⊙ Send** | Click, hover over the Send/Submit button, hold still for the countdown. |
| **⎕ Region** | Drag a rectangle over the area of the window that shows outgoing messages — OCR watches here. |
| **⌖ Cal** | Auto-calibrate using OpenCV template matching against the `buttons database/` folder. |

### Message Protocol

Agents communicate using this 3-line format:

```
To Agent2
<message content here>
end message now
```

- **Line 1** — routing header: `To Agent1`, `To Agent2`, or `To Agent3` (case-insensitive)
- **Middle** — message body (any text; quotes stripped automatically)
- **Last line** — sentinel: `end message now`

The sentinel prevents partial captures while text is still streaming. SOC only routes after the sentinel is visible.

**Fallback single-line format:**
```
To Agent2: please implement the login function
```

### Three Routing Pipelines

| Pipeline | How it works |
|---|---|
| **A — OCR Watcher** | Tesseract reads the configured screen region. Normal: 1.5 s scan. Rapid: 0.3 s after `To Agent` spotted. |
| **B — File Outbox** | An agent (or VS Code extension) drops a `.md` file into `outbox/agent1/` etc. SOC reads, routes, moves to `sent/`. |
| **C — Manual Inject** | Type `to agent1: message` in the widget's inject bar and press Enter. |

### Workflow Buttons

| Button | Action |
|---|---|
| `▶ Start OCR` | Starts the screen OCR watcher |
| `▶ Outbox` | Starts the file outbox poller |
| `⌖ Cal` | Runs auto-calibration against button templates |
| `⚡ VS Code` | One-click: starts outbox watcher + auto-click + briefs Agent 3 |
| `🔵 Bing` | One-click: enables Agent 1 Edge-browser-aware injection cadence |

---

## Auto-Click

SOC uses OpenCV template matching to find and click buttons automatically (e.g. VS Code Allow, Claude Yes, Send buttons).

- Templates live in `buttons database/`
- Add a PNG screenshot of any button you want auto-clicked
- Use **Train** in the Auto-Click panel to set the confidence threshold per template
- Adjust `TEMPLATE_THRESH` in `soc_ultralight.py` (default: `0.80`)

**Failsafe:** move your mouse to the top-left corner of the screen to stop all pyautogui actions immediately.

---

## Project Structure

```
SOC_Ultralight/
├── soc_ultralight.py        # Main application (~1700 lines, single file)
├── config.json              # Agent window/XY settings (auto-saved)
├── requirements.txt         # Python dependencies
├── run.bat                  # Launch script
├── README_INSTRUCTIONS.md   # Detailed setup guide
├── buttons database/        # PNG templates for auto-click matching
│   └── registry.json        # Template confidence training data
├── outbox/                  # Drop .md files here for routing (git-ignored)
│   ├── agent1/
│   ├── agent2/
│   └── agent3/
└── sent/                    # Processed messages archived here (git-ignored)
```

---

## Key Settings (top of `soc_ultralight.py`)

| Constant | Default | Purpose |
|---|---|---|
| `SCAN_NORMAL` | `1.5` | Seconds between OCR scans |
| `SCAN_RAPID` | `0.3` | Rapid scan rate after `To Agent` spotted |
| `RAPID_DURATION` | `8.0` | How long rapid mode lasts (seconds) |
| `TEMPLATE_THRESH` | `0.80` | OpenCV match confidence threshold |
| `AUTOCLICK_COOLDOWN` | `3.0` | Seconds between re-clicks of the same button |
| `MAX_INJECT_CHARS` | `8000` | Message truncation limit |

---

## Dependencies & Licences

| Dependency | Licence | Notes |
|---|---|---|
| [pyautogui](https://github.com/asweigart/pyautogui) | BSD-3 | Mouse/keyboard automation |
| [pyperclip](https://github.com/asweigart/pyperclip) | BSD-3 | Clipboard |
| [pywin32](https://github.com/mhammond/pywin32) | PSF | Windows API |
| [mss](https://github.com/BoboTiG/python-mss) | MIT | Screen capture |
| [Pillow](https://github.com/python-pillow/Pillow) | HPND | Image processing |
| [pytesseract](https://github.com/madmaze/pytesseract) | Apache-2.0 | Tesseract OCR wrapper |
| [opencv-python](https://github.com/opencv/opencv-python) | MIT | Template matching |
| [numpy](https://numpy.org/) | BSD-3 | Array support |
| [Tesseract OCR](https://github.com/UB-Mannheim/tesseract) | Apache-2.0 | OCR engine (installed separately) |

---

## Licence

```
MIT License

Copyright (c) 2026 BaxtersLab2

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
