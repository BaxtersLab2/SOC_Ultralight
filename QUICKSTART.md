# SOC Ultralight — Quickstart: Dual-Agent Module Block Workflow

This guide walks you through starting a project from scratch using the two-agent system.
Agent 1 (Bing Copilot) plans and delivers module blocks. Agent 2 (Claude Code) stores
them and implements in sequence. SOC Ultralight routes every message between them.

---

## Prerequisites

Before starting, confirm:

- SOC Ultralight is configured (both agents have Set Win, Cal, and OCR Region done)
- Agent 1 (Bing Copilot) is open in Edge and ready for a new conversation
- Agent 2 (Claude Code) is open in VS Code and ready for a new conversation
- You have filled out **`templates/PROJECT_SUMMARY_TEMPLATE.md`** with your project details

---

## Step 1 — Enter your project name in SOC

In the SOC Ultralight widget, find the **Project:** field and type your project name.
This gets prepended to every message sent to Agent 1 as an anti-drift reminder.

---

## Step 2 — Prime Agent 2 first

Click **▶ Agent 2 SOP** in SOC Ultralight.

This sends Agent 2 its operating instructions: what format to use, how to save blocks,
and the rule that it must NOT implement anything until explicitly authorized.

Wait for Agent 2 to acknowledge before continuing.

---

## Step 3 — Prime Agent 1

Click **▶ Agent 1 SOP** in SOC Ultralight.

This sends Agent 1 the workflow structure template — the module/block system overview,
naming conventions, and delivery format.

Wait for Agent 1 to acknowledge.

---

## Step 4 — Give Agent 1 your project and the block template

Copy the kickstart prompt below, fill in the two placeholders, and paste it directly
into Bing Copilot (Agent 1). Press Enter to send it manually — do NOT turn OCR on yet.

---

### Kickstart Prompt (paste to Agent 1)

```
I am going to give you a Project Summary and a module block format template.

Your job:
1. Read the Project Summary fully before writing any blocks.
2. Identify the modules (A = Scope, B = Scaffold, C/D/E... = feature crates in
   dependency order).
3. Decompose the project into module blocks using the format in the template below.
4. Deliver each block to Agent 2 via the relay using exactly this format:

To Agent2
[full block content]
end message now

5. After sending each block, WAIT for Agent 2's confirmation reply before sending
   the next one. Do not send two blocks without a confirmation between them.
6. When all blocks are delivered, send this exact phrase:

To Agent2
that is the final block you may begin implementation in alphanumeric order now
end message now

---

PROJECT SUMMARY:
[paste the contents of your filled-in PROJECT_SUMMARY_TEMPLATE.md here]

---

MODULE BLOCK FORMAT TEMPLATE:
[paste the contents of templates/GENERAL_MODULE_BLOCK_TEMPLATE.md here]
```

---

## Step 5 — Start OCR

Once Agent 1 has acknowledged the kickstart prompt and is ready to begin:

1. Click **▶ Start OCR** in SOC Ultralight.
2. Click **⏸ Hold A2** immediately after — this prevents Agent 2's first reply from
   firing before Agent 1 sends its first block. Release it once Agent 1's first block
   is on screen and you are happy with it.

SOC will now handle routing automatically.

---

## Step 6 — The automated loop

From here, the workflow runs on its own:

```
Agent 1 writes block A-1 (To Agent2 / content / end message now)
        ↓ SOC detects and routes
Agent 2 receives A-1, saves it, replies:
        To Agent1 / module block A-1 saved, ready for next block / end message now
        ↓ SOC detects and routes
Agent 1 sends block A-2
        ↓ ...continues until all blocks delivered
Agent 1 sends authorization phrase
        ↓ SOC detects phrase, switches to IMPLEMENTATION MODE
Agent 2 implements all blocks in alphanumeric order (A-1, A-2, B-1...)
Agent 2 reports each blocker or completion via the same relay format
```

You can watch progress in the **Diagnostics** panel. Each routed message appears as
`[→agentX] ✓ ...`.

---

## Intervening mid-session

If both agents have responses on screen at the same time and routing becomes chaotic:

1. Click **⏸ Hold A2** or **⏸ Hold A1** to gate one side.
2. Read both messages. Decide which one should go first.
3. Click **▶ Resume** on the side you want to let through.
4. The hold auto-releases after one message routes. Sequence resumes normally.

If a message fails to route (dedup block after timeout), click **↺ Release** to clear
the block and allow the same message to resend.

---

## Resuming after a restart

If you restart SOC mid-project:

1. Re-run **Set Win** for Agent 2 if VS Code changed workspaces.
2. Re-run **⌖ Cal** if any windows moved.
3. Start OCR.
4. Use **⏸ Hold A1** or **⏸ Hold A2** to gate one side while you check which
   agent has the next pending message, then release and let the sequence continue.

Agent 1 can see the full conversation history in Copilot and knows which block it was
on. Agent 2 has its saved block files. Both can re-sync without losing progress.

---

## Template files

| File | Purpose |
|---|---|
| `templates/PROJECT_SUMMARY_TEMPLATE.md` | Fill this in with your project details |
| `templates/GENERAL_MODULE_BLOCK_TEMPLATE.md` | The exact block format Agent 1 uses to structure each block |
| `agent1 soc ultralight .txt` | Agent 1 SOP — sent automatically by ▶ Agent 1 SOP |
| `agent 2 soc ultralight.txt` | Agent 2 SOP — sent automatically by ▶ Agent 2 SOP |
