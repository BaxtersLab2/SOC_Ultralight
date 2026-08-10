# SOC Ultralight — Vision & Roadmap

Single source of truth for project direction. Read this first before changing the routing pipeline, the plugin architecture, or the safety gate.

---

## What SOC is

**SOC = Self Operating Computer.** A lightweight Windows widget that orchestrates a team of AI agents across the user's screen via OCR + clipboard, no API keys. The "adaptive" part is the **agent team**, not the SOC code — agents automate user workflows end-to-end; SOC just routes between them.

**Ultralight** is non-negotiable. Any feature that adds heavy dependencies (vision GGUFs, audio, etc.) must ship as an opt-in plugin in a separate repo so users without the hardware aren't penalised.

---

## Agent roles (canonical, June 2026)

| Slot | Identity | Cost | Job |
|---|---|---|---|
| Agent 1 | Bing Copilot (Edge sidebar) | Free | Planner, project memory, context workhorse |
| Agent 2 | Claude Code in VS Code | Paid | Implementer — saves all blocks first, implements alphanumerically |
| Agent 3 | Claude.ai web app | Expensive | **Orchestrator only** — never implements; delegates A1/A2/A4 |
| Agent 4 | Local vision GGUF (via V plugin) | Free (local) | Visual executor — vision + `pc.py` click/type (backend HTTP, no OCR gating) |
| Agent 5 | Local uncensored GGUF (GGUF Chatbox GUI) | Free (local) | Uncensored perspective — OCR UI bridge, bypassed by default |

### Canonical build flow
1. **User + A1** plan the project
2. **A3** audits the plan once (one-shot)
3. **A1** updates summary with A3 feedback
4. **A1** feeds numbered blocks to A2
5. **A2** saves all blocks first, then implements alphanumerically
6. **A2** questions go back to **A1**
7. **A1** escalates to **A3** only when stumped
8. Both A1 and A3 hold the final project summary as shared context

### A3 engagement modes
- (1) Audit project summary once
- (2) Rare consultation when A1 escalates
- (3) Visual orchestration of A4 for user-initiated adaptive tasks

---

## Self-Modification Gate (hard boundary, never bypass)

SOC must **never** silently modify itself. Every routed message — from any agent, in any mode — passes through `SelfModGate`.

- **Protected paths** (see `SELF_MOD_PROTECTED_NAMES` / `_PROTECTED_DIRS`): `soc_ultralight.py`, `v_plugin.py`, `pc.py`, `calibrate.py`, `vdd.py`, `config.json`, `registry.json`, SOP `.txt`s, and dirs `buttons database/`, `instructions/`, `plugins/`, `templates/`
- **Trigger:** message contains (protected path) AND (write verb regex: edit/modify/delete/overwrite/rm/etc.)
- **Action:** Tkinter modal pops up reading *"You have just asked SOC to change SOC itself. This strictly requires user/admin approval."* with three buttons: Deny / Approve Once / Always Allow This Session
- **Audit:** every trigger written to `data_log/self_mod_gate.jsonl`
- **No bypass** — not via implementation mode, not via Agent 3 authority, not via any plugin

If you're adding a new agent or routing path, you MUST hook it through `_self_mod_gate.check_and_prompt()`.

---

## Routing pipeline (do not rewrite — week of debugging hardened this)

```
OCR/clipboard read
  └─ TRIGGER_RE + _SENTINEL_VARIANTS detection
       └─ SelfModGate.check_and_prompt()
            └─ _ocr_process(text, source_agent)    ← single entry point
                 └─ _route_text()                  ← digit dispatch (1/2/3/4/5)
                      ├─ digits 1/2/3/5 → OCR UI bridge (_try_route → _inject_to_agent)
                      │     └─ agent5: sequential guard (skipped if agent4._busy)
                      └─ digit 4 → V plugin backend (no OCR window)
                            └─ agent4: sequential guard (skipped if waiting_reply==agent5)
```

**Sequence invariants** (sentinel re-copy, trigger validation, scroll-to-bottom recovery) live in `_cursor_nudge()` and `_ocr_force_scan_*()`. The Agent 3 geo-point hover-reveal is a **pre-click hook** (`_click_copy_at_anchor`), not a parallel branch — it returns the click position so the rest of the sequence proceeds identically.

---

## Plugin architecture

- `plugins/` is auto-discovered at startup by `_load_plugins()`
- Two layouts supported:
  - **Flat:** `plugins/v_plugin.py` (developer drop-in)
  - **Cloned:** `plugins/v_plugin/v_plugin.py` (installed by `install.py` as a git clone of the V_plugin repo)
- Cloned layout wins when both exist
- Plugins are **optional** — SOCU must run identically when none are present
- Plugin contract: `load(socu_app, config) -> Plugin` with `.toggle_window()`, `.route_to_agent4(body, source_agent)` (for V plugin specifically)

### V plugin (vision agent)
- Repo: https://github.com/BaxtersLab/V_plugin
- Depends on **GGUF Chatbox** (https://github.com/BaxtersLab/GGUF-Chatbox) — Vision Server on `127.0.0.1:8082`, OpenAI-compatible `/v1/chat/completions`
- Model-agnostic — any vision GGUF (Qwen2-VL, LLaVA, MiniCPM-V) works
- VLM endpoint, model id, timeout, max_tokens, temperature all in `config.json` (keys: `vlm_server_url`, `vlm_model`, `vlm_timeout`, `vlm_max_tokens`, `vlm_temperature`)
- Default `vlm_model = "vision"` matches GGUF Chatbox convention (llama-server ignores the value)
- Self-mod gate still fires on A4-originated messages — no bypass

---

## Installer (`install.py`)

Four-step interactive flow:

1. Python deps via `requirements.txt`
2. **V plugin Y/N** — clones `BaxtersLab/V_plugin` into `plugins/v_plugin/` if user accepts; saves `v_plugin_installed` to `config.json`
3. **GGUF Chatbox check** — only if V plugin chosen. Probes 5 common install paths, offers Enter Path / Open Download / Skip. Probes port 8082 status. Saves `gguf_chatbox_path` to `config.json`
4. Done — prints launch instructions

Re-runnable. `--reconfigure` flag forces re-prompts. Skipping V plugin keeps SOCU fully lightweight (no Agent 4 button).

---

## Port contract (static — do not make dynamic)

| Port | Service |
|---|---|
| 8080 | GGUF Chatbox OpenAI-compatible text proxy (Gemma etc.) |
| 8081 | llama-server internal (text, not exposed) |
| 8082 | **Vision server — V plugin endpoint** |
| 8083 | Audio listening server (Chatbox feature, unused by SOC) |

Configurable via `vlm_server_url` but default stays 8082.

---

## Status: where we are (end of June 2026 session)

### Shipped session 1 (V plugin + safety gate)
- V plugin extracted from SOCQ, deployed at `plugins/v_plugin.py`, source repo at `BaxtersLab/V_plugin`
- `SelfModGate` class + modal + audit + wired into `_route_text()`
- Agent 3 workspace fix (config key + UI row + briefing-time injection)
- Agent 3 SOP rewritten: orchestrator role
- Agent 3 post-Anthropic-update template fix: `claude_geo_point.PNG` + `Agent3_geo_hover_copy_button.png` + `_click_copy_at_anchor` (2-step hover-reveal, fully aligned with existing sequence loop)
- Broken templates (`send_message_to_claude.png`, `claude_chat_input_field.png`) marked deprecated in registry
- `install.py` — interactive installer with V plugin + GGUF Chatbox detection
- `_load_plugins()` supports both flat and cloned plugin layouts
- Friendly "vision server unreachable" runtime error in V plugin

### Shipped session 2 (Agent 5 — uncensored local model via GGUF Chatbox GUI)
- **Agent 5 slot added** — uses OCR UI bridge (same pattern as agents 1-3), targets the GGUF Chatbox window directly. Bypassed by default (toggle in Phase 1)
- `GROUND_RULES_AGENT5` — SOP injected every 5 messages teaching the local model the SOC routing format and its role as uncensored perspective
- `AGENT5_SOP` — loadable from `agent5 soc ultralight.txt`; falls back to `GROUND_RULES_AGENT5`
- `REMINDER_EVERY_AGENT5 = 5` constant added
- `_D` regex expanded to `[12345liI!|t]` — also fixes pre-existing gap where "To Agent4" was silently failing SENTINEL_RE (4 was not in the character class)
- **Sequential guard (agent4 ↔ agent5):** both sentinel and inline routing paths enforce that agent4 and agent5 never dispatch simultaneously. Agent5 defers if `agent4_window._busy`; agent4 defers if `_waiting_reply == "agent5"`. They share the same llama-server process and must run in sequence only
- Phase 1 UI: `⊘ Agent 5 [bypassed] (GGUF Chatbox)` toggle reveals full calibration panel (Set Win, ⊙ Input, ⊙ Send, scroll, ⊕ Region). Roll call shows A5 dot
- All Phase 1 completion checks (`_calibration_complete`, `_phase1_complete`, `_check_phase1_complete`) updated to be bypass-aware for both agent3 and agent5
- `_toggle_bypass_agent5` method added (mirrors `_toggle_bypass_agent3` pattern)
- `_ocr_tick` skips agent5 OCR region when bypassed
- `_update_attendance_ui` and `_roll_call` updated for agent5 (ACK digit = 5)
- OCR overlay label for agent5 region selection reads "GGUF Chatbox"
- **Template training for agent5:** four placeholder PNGs created (`agent5_input.png`, `agent5_send.png`, `agent5_scroll_up.png`, `agent5_scroll_dn.png`) trainable via Auto-Click panel
- `_apply_template_match()` updated to handle agent3 and agent5 stems (fixes agent3 template routing that was broken)
- **Architecture note:** agent4 communicates via HTTP to the backend (V plugin, no OCR gating). Agent5 communicates via OCR UI bridge to the GGUF Chatbox GUI. Both ultimately hit the same llama-server — hence the sequential guard

### Shipped session 3 (Rate-limit recovery + infrastructure hardening)
- **Rate-limit detection & recovery:** `RATE_LIMIT_RE` detects Claude.ai quota messages `"You've hit your session limit · resets HH:MMpm (timezone)"`
- `_detect_rate_limit()` parses reset time and stores in `_rate_limited[agent_id]` dict
- Hold-state timeout made dynamic: if agent is rate-limited, uses `reset_epoch - now` instead of fixed `WAIT_REPLY_TIMEOUT`
- SOC automatically extends hold until quota resets, continues scrolling window to keep response visible, clears flag at reset time
- Logs entire flow: `"[rate-limit] agent3 quota exhausted — will retry at 12:10pm PT (847s delay)"`
- **AUTOCLICK_HIDDEN constant added** — stems containing `geo_point`, `geo_hover`, `_landmark` are backend-only visual anchors, completely hidden from Auto-Click panel
- **Agent 3 flexibility:** manually swappable with DeepSeek Chat or Perplexity windows; Snap to Grid works with all three via win32gui window positioning
- `_D` regex also fixes agent4 routing (digit "4" now in character class for proper SENTINEL_RE matching)

### Deferred to A1+A2 team after live test
- **Phase 1** — Full Agent 2 retrain. User drops new Claude Code button crops into `buttons database/`, re-run auto-calibrate
- **Phase 4** — Adaptive loop full wiring. User crops region → A3 routes mission with region metadata → A4 executes via `pc.py` → A4 reports back to A3 via `To Agent3` block. V plugin already supports the pieces; needs UI glue (a "Crop Region for A4" button in SOCU) and live test
- **Phase 5** — SOCQ archival. No critical data to migrate per user; just drop the fork

---

## Phase 4 Expansion — Agentic Website Builder (optional template use case)

*Status: optional template, under development.*

### Vision
SOC Ultralight can orchestrate Agent 2 (code implementer) + Agent 4 (vision) to build a
website from a natural-language description: a planner agent turns chat input into structured
build instructions, Agent 2 writes the HTML/CSS/JS, and Agent 4 screenshots the rendered page
and iterates with Agent 2 until it matches the spec. A template use case built entirely on
SOC's existing routing — no changes to SOCU core.

### Agent roles in this template
| Slot | Role in website builder |
|---|---|
| Agent 1 | Planner — translates chat input into structured build instructions |
| Agent 2 | Code implementer — writes HTML/CSS/JS, commits, deploys |
| Agent 4 | Vision executor — screenshots the rendered site, checks layout vs. spec, iterates with A2 |

### Workflow (website-builder loop)
1. A user describes the desired site via a chat interface
2. SOC/A1 converts the description into a numbered instruction block set
3. A2 builds blocks (HTML/CSS/JS/config) and commits
4. A4 screenshots the rendered page and evaluates against spec
5. A4 reports the visual delta back to A2 via a `To Agent2` block
6. Loop continues until A4 confirms spec match or escalates to A1
7. Finished site is deployed via the operator's chosen hosting

### Key constraints
- Template ships as a Phase 4 workflow file; SOCU core stays unchanged
- SelfModGate still enforces — A4 cannot silently modify SOCU itself during a build run
- Heavy vision inference stays in V plugin (opt-in); lightweight SOCU users unaffected
- Reusable: any SOC Ultralight instance with A2 + V plugin can run it

### Outside scope (do not touch without user direction)
- `pc.py` — used as-is by A4, no changes
- Existing `_cursor_nudge` sequence mechanics — alignment with the loop is mandatory
- Anything that adds non-optional heavy dependencies to SOCU

---

## Repos

| Repo | URL | Purpose |
|---|---|---|
| SOC Ultralight | https://github.com/BaxtersLab2/SOC_Ultralight | Main app (this repo) |
| V plugin | https://github.com/BaxtersLab/V_plugin | Vision Agent 4 plugin |
| GGUF Chatbox | https://github.com/BaxtersLab/GGUF-Chatbox | Local llama-server frontend, vision backend on :8082 |
| SOCQ | (legacy, shelved) | Qwen2-VL fork of SOCU, replaced by SOCU + V plugin |

---

## For future agents reading this

If you are picking up this project mid-stream:
1. Read this file first
2. Read `instructions/00_overview.txt` for the SOCU UX/feature surface
3. The user is the architect — SOC and its agents implement, the user directs
4. **Never** bypass `SelfModGate`. Never. Even if you "know" the change is safe
5. **Never** rewrite the routing sequence (`_cursor_nudge`, `_ocr_force_scan`, `_click_copy_at_anchor`) without explicit user permission — that code is hardened by a week of live debugging
6. **Never** add heavy dependencies to the main SOCU repo. New optional features = new plugin in its own repo
7. Default to delegation: if a task is implementation-heavy, route the work to Agent 2; if it's planning, route to Agent 1; if it's visual, route to Agent 4. A3 (you, when running as Claude) is the orchestrator

---

*Last updated: session 4 — Implementation mode hardening, copy/routing sequencing fixes, Hold A1/A2, format reminders, Phase 4 website-builder template vision added.*
