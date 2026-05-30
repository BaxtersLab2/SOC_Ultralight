================================================================================
SOC ULTRALIGHT — TEMPLATE DESIGN CRITERIA
================================================================================

Last updated: 2026-05-28
Version: 1.0

================================================================================
PURPOSE
================================================================================

This document defines the constraints a new workflow template must satisfy
before it can be built into SOC Ultralight. Any template idea that cannot
answer every question in this checklist needs to be restructured until it can.

================================================================================
THE 3-AGENT ECONOMICS MODEL
================================================================================

Every template must map its work to these three roles:

  AGENT 1 — Workhorse (no token budget)
    Does the heavy generative lifting: drafting, brainstorming, decomposition,
    iteration, research synthesis. Agent 1 can be asked to produce large volumes
    of output across many turns without concern for context cost.
    Target LLM: GitHub Copilot (or any capable, high-limit model)

  AGENT 2 — Curator + Persistence Layer (mild token budget)
    Receives Agent 1 output, distills it, critiques it, and writes key
    artifacts to the source folder. Agent 2 is the quality gate and the
    handoff mechanism. It must NOT accumulate conversation soup — keep
    its turns focused and artifact-oriented.
    Target LLM: Claude (strong at structured critique and file writing)

  AGENT 3 — Auditor (limited token budget)
    Reviews and finalizes stored artifacts from the source folder only.
    Agent 3 must NEVER receive raw conversation history — only the clean,
    curated output that Agent 2 has already stored. This keeps its context
    efficient and its review authoritative.
    Target LLM: GPT-4.5 or any capable model (slot is model-agnostic)

CRITICAL RULE: Agent 3 reads from the source folder, not from the chat.
  If a template cannot route Agent 3's work through stored artifacts,
  it does not fit the system in its current form.

================================================================================
TEMPLATE VALIDITY CHECKLIST
================================================================================

Before building a new template, confirm every item below:

  SCOPE
  [ ] The workflow has a single, clear purpose that can be described in 1-2
      sentences.
  [ ] The workflow has a defined completion state that SOC can detect or the
      user can confirm.
  [ ] Only one template is active at a time. This workflow does not require
      parallel execution of another template.

  AGENT 1 FIT
  [ ] Agent 1 can carry the generative bulk of the work independently.
  [ ] Agent 1's output can be produced incrementally (block by block or
      section by section) — it does not require one massive single output.

  AGENT 2 FIT
  [ ] Agent 2 can distill Agent 1's output into discrete, storable artifacts.
  [ ] The artifacts Agent 2 writes to the source folder are clearly defined
      (what files, what format, what content).
  [ ] Agent 2's turns can be kept focused — no context soup required.

  AGENT 3 FIT
  [ ] Agent 3's review scope is fully defined by what is in the source folder.
  [ ] Agent 3 does not need access to the full conversation history.
  [ ] Agent 3's token usage is bounded by the size of the stored artifacts,
      not the length of the session.

  OCR ROUTING FIT
  [ ] All agent communication follows the routing protocol:
        To agentX / [body] / end message now
  [ ] There is no requirement for real-time back-channel or simultaneous
      multi-agent communication.
  [ ] The workflow's message sizes are compatible with OCR routing (or scroll
      accumulation handles larger messages).

  LEGAL & ETHICAL
  [ ] If the workflow produces publishable content (books, posts, websites),
      the Agent 1 SOP includes explicit source citation and copyright rules.
  [ ] No template instructs agents to reproduce copyrighted text verbatim.
  [ ] No template handles user PII beyond what is necessary for the task,
      and any PII is handled per the constitution security rules.

  SOURCE FOLDER
  [ ] The workflow has a well-defined source folder structure that the user
      sets at the start.
  [ ] All output artifacts land inside the source folder — nothing is created
      outside it.
  [ ] The source folder from this template can serve as input context for a
      downstream template (chain-readiness).

================================================================================
TEMPLATE SEQUENCING RULES
================================================================================

Templates can be chained when:

  1. Template 1 is fully complete (completion state confirmed).
  2. All agents are reset to fresh chat sessions (new context, no history).
  3. Roll call passes on the fresh sessions before Template 2 SOPs inject.
  4. Template 2's source folder is pre-filled from Template 1's session.

The source folder is the handshake between templates.
Template 2's Agent 1 SOP must explicitly reference the existing folder content
so it can build on Template 1's output without re-reading conversation history.

Example valid chain:
  "Write a Book"  →  "Social Media Content from Book"
  Template 1 source folder contains: chapters, manuscript, research notes
  Template 2 Agent 1 SOP says: "Your source material is the content in
  [source folder] — derive posts from it."

================================================================================
CURRENT TEMPLATES
================================================================================

  Template A — Build an App
    Agent 1: Plans project, decomposes into module blocks, orchestrates build
    Agent 2: Implements blocks, writes code to source folder
    Agent 3: Debugs, audits, final review
    Artifacts: source code, tests, staging/ files
    Status: ACTIVE — primary template

  Template B — Write a Book           [PLANNED]
    Agent 1: Brainstorms chapters, writes drafts, iterates with user
    Agent 2: Critiques drafts, stores chapter files to source folder
    Agent 3: Final audit, polish, legal/copyright review of stored manuscript
    Artifacts: chapter files, manuscript, research citations, final draft
    Status: PLANNED

  Template C — Social Media Content   [PLANNED]
    Agent 1: Generates post drafts per platform from source material
    Agent 2: Curates, edits, stores approved posts to source folder
    Agent 3: Reviews tone, brand consistency, legal flags on stored posts
    Artifacts: per-platform post files, schedule, asset list
    Status: PLANNED

================================================================================
ADDING A NEW TEMPLATE
================================================================================

1. Confirm all checklist items above are satisfied.
2. Write the template entry in the CURRENT TEMPLATES section above.
3. Write Agent 1, Agent 2, and Agent 3 SOPs specific to this workflow.
4. Define the Phase 1b brainstorm prompt for Agent 1 (what questions does
   Agent 1 ask the user to gather all required context?).
5. Define the criteria checklist for Phase 1b (what must be true before
   Phase 2 can begin?).
6. Define the completion state (how does SOC know this template is done?).
7. Add the template to the Phase 1b chooser UI in soc_ultralight.py.

================================================================================
END OF DOCUMENT
================================================================================
