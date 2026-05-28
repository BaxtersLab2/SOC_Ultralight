# PROJECT SUMMARY — [Project Name]

> This document is produced by Agent 1 at the end of the Phase 1a brainstorm session.
> Agent 1 walks the user through each section below during the brainstorm, then
> assembles this completed document when all areas are covered.
>
> If you are loading a pre-written summary: ensure every section is filled in,
> including Section 5 (Security Requirements), before injecting it into Agent 1.
> Agent 1 will review it and flag any gaps before proceeding to block decomposition.

---

## 1. PROJECT NAME
[Short name — used in block file naming and SOC's Project field]

## 2. PURPOSE
[What does this project do and why does it exist? What problem does it solve?
One to three sentences. No implementation details yet.]

## 3. CORE FEATURES
[The major functional components the finished product must have.
One bullet per feature. Describe behaviour, not code.]

- 
- 
- 

## 4. TECHNICAL STACK
[Language, framework, key libraries, target platform(s), build system.
Be specific — version numbers if relevant.]

- Language:
- Framework / Engine:
- Key libraries:
- Target platform(s):
- Build system:

## 5. SECURITY REQUIREMENTS
[Mandatory. Agent 1 must cover all sub-points during the brainstorm.
This section drives both the security architecture of the block plan
and the Phase 2a Security Audit checklist.]

- Authentication model: [None / API key / OAuth / session token / other]
- External services and credentials needed:
  - [service name]: [what credential / how it is obtained]
- Sensitive data handled: [None / user PII / financial / health / credentials / other]
- External input surfaces and required validation:
  - [HTTP endpoints / CLI args / file reads / IPC / WebSocket / etc.]
- Behaviour on auth failure or invalid input:
- Compliance or privacy requirements: [None / GDPR / HIPAA / other]

## 6. FOLDER / WORKSPACE LAYOUT
[The intended directory and crate/package structure.
If unknown at brainstorm time, leave blank — Agent 1 will define it in Module A.]

```
project-root/
├── 
└── 
```

## 7. EXTERNAL DEPENDENCIES & INTEGRATION POINTS
[Other apps, services, hardware, or databases this project talks to.
For each: what interface is used and what data flows in each direction.]

- 

## 8. CONSTRAINTS AND DESIGN DECISIONS
[Anything the implementing agent must NOT deviate from.
Examples: Windows-only, SQLite not Postgres, no external APIs, specific performance targets.]

- 

## 9. SAVE PATH FOR BLOCK FILES
[Where on the implementing machine should module block instruction files be saved?
Example: C:\Users\Name\Desktop\MyProject\instruction_blocks]

Path: 

---

> After this document is complete and the user confirms they are satisfied,
> Agent 1 hands it to Claude for the summary improvement stage before block decomposition begins.
