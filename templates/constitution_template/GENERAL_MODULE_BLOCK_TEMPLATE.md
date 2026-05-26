================================================================================
GENERAL MODULE BLOCK TEMPLATE
The Baxter Module/Block System — A Guide for Agents
================================================================================

Last updated: 2026-03-26
Version: 1.0

================================================================================
WHAT IS THE MODULE/BLOCK SYSTEM?
================================================================================

The Module/Block System is a structured way of breaking a software project's
Project Summary into a precise, ordered sequence of implementation units. Each
unit is called a BLOCK. Blocks are grouped into MODULES. This system exists to
give an implementing agent (or developer) everything they need to write the
correct code, in the correct order, without guessing — and without needing to
ask many clarifying questions.

The system has two core goals:

  1. PREVENT DRIFT — The agent always knows exactly what it is building next,
     what it depends on, and what the resulting state of the codebase should be.
     There is no ambiguity about scope.

  2. ENABLE STAGED TESTING — Every block is a testable, completable unit of
     work. The agent builds, compiles, tests, then moves to the next block.
     Nothing is assumed to work until it is verified.

--------------------------------------------------------------------------------
COORDINATE SYSTEM
--------------------------------------------------------------------------------

Every block has a coordinate in the form:   LETTER - NUMBER

  Letter = the Module (A, B, C, D, E...)
  Number = the Block within that module (1, 2, 3, 4...)

Examples:
  A-1 = Module A, first block
  C-3 = Module C, third block
  F-2 = Module F, second block

This coordinate is used in:
  - File names:    "module c block 3.txt"
  - DEPENDS ON:    lists which coordinates must be complete before this block
  - BUILD ORDER:   a global counter across all blocks in the project

When referencing blocks in conversation, always use the coordinate. It removes
all ambiguity about location in the build.

================================================================================
MODULE STRUCTURE
================================================================================

Modules are lettered A, B, C, D... in order of FUNDAMENTAL STABILITY. This
means the most foundational, least-dependent layer of the app is Module A, and
each subsequent module builds on top of the ones before it.

A common module layout pattern (adapt per project):

  Module A — Scope & Folder Tree
    What this project is, what it connects to, what it does NOT do.
    The directory structure and workspace layout.
    No code yet — pure orientation and structure.

  Module B — Scaffold
    Workspace manifests (Cargo.toml, package.json, pyproject.toml, etc.)
    Crate/package stubs — empty files that allow the workspace to compile/check.
    Build tooling, linting config, .gitignore, license files.
    After this module: the workspace checks/compiles clean with zero logic.

  Module C — Core/Shared Crate
    Error types, shared data types, config structs, constants.
    Everything that every other crate will import.
    No business logic — only the vocabulary of the project.

  Module D — (First Feature Crate)
    The next most foundational runtime dependency.
    Example: transport/IPC layer, database layer, hardware abstraction.

  Module E — (Second Feature Crate)
    The next layer up. Depends on C and D.
    Example: LLM adapter, vision pipeline, encoding pipeline.

  Module F — (Third Feature Crate)
    Depends on C, D, and E.
    Example: executor/action layer, business logic layer.

  Module G — (API / CLI / Entry Point)
    The outermost shell: HTTP server, CLI binary, UI.
    Wires together all lower crates.
    This is the last thing built.

The number of modules is determined by the project's crate count and
complexity. Add a module for each logically distinct crate or layer.
Never combine two unrelated responsibilities in one module.

================================================================================
BLOCK STRUCTURE — ANATOMY OF A SINGLE BLOCK
================================================================================

Every block file follows this exact template. Fields marked [REQUIRED] must
always be present. Fields marked [CONDITIONAL] are included when relevant.

--------------------------------------------------------------------------------

================================================================================
BLOCK [LETTER]-[NUMBER] — [SHORT TITLE IN CAPS]
Module [LETTER]: [Module Name] ([crate-name])
================================================================================

BLOCK ID:       [LETTER]-[NUMBER]
MODULE:         [LETTER] — [Module Name]
CRATE:          [crate or package name this block lives in]
DEPENDS ON:     [List of block coordinates that must be complete first, or "None"]
BUILD ORDER:    [N] of [TOTAL]

--------------------------------------------------------------------------------
PURPOSE
--------------------------------------------------------------------------------
[2–6 sentences. Explain what this block accomplishes in plain language.
Answer: what exists after this block that did not exist before?
State explicitly what this block does NOT include (saves content for a later
block). If relevant, name the downstream block that will build on this one.]

--------------------------------------------------------------------------------
CONTEXT — WHY THIS EXISTS
--------------------------------------------------------------------------------
[CONDITIONAL — include when the agent needs background to understand design
choices. Explain the role this piece plays in the larger system. Reference
parent app dependencies here if the crate connects to an external system.
For example: "This crate is the bridge between RemoteDexter's model loader
and the RDup2 orchestration layer. The model loader is owned by RDup1 — this
block wires into it via [method/trait/interface]."]

--------------------------------------------------------------------------------
EXTERNAL DEPENDENCIES & INTEGRATION POINTS
--------------------------------------------------------------------------------
[CONDITIONAL — include when this block touches another app, service, or crate
that is outside this workspace. List:
  - External app name
  - What interface/protocol is used to reach it
  - What data flows in each direction
  - Any assumptions about its state or availability]

--------------------------------------------------------------------------------
FILES TO CREATE / MODIFY
--------------------------------------------------------------------------------
[List every file this block touches. For each file, provide:
  - The file path relative to the workspace root
  - What the file's role is
  - Key structs, traits, functions, or types that must exist in it
  - The logical behavior of each item (not code — describe what it does,
    what it accepts, what it returns, what side effects it has)
  - Any important field names with their types and purpose
  - Any constants, enums, or error variants needed
  - The relationship between types in this file and types in other crates]

Example format for a single file entry:

  FILE: crates/rdup2-core/src/config.rs
  Role: Defines the top-level runtime configuration for the service.

  Structs:
    - AppConfig
        Fields: bind_addr (String), port (u16), storage_path (String),
                auth_secret (String), log_level (String),
                dry_run_default (bool)
        Purpose: Loaded once at startup from env vars and/or a TOML file.
                 Passed as shared state (Arc<AppConfig>) to all subsystems.

    - StorageConfig
        Fields: db_url (String), max_connections (u32)
        Purpose: Nested inside AppConfig. Controls the SQLite connection pool.

  Functions:
    - AppConfig::from_env() -> Result<AppConfig>
        Reads all fields from environment variables with sensible defaults.
        Returns an error if required secrets (auth_secret) are missing.

    - AppConfig::from_file(path: &Path) -> Result<AppConfig>
        Deserializes from a TOML file at the given path. Falls back to
        from_env() for any missing fields.

--------------------------------------------------------------------------------
DATA FLOW
--------------------------------------------------------------------------------
[CONDITIONAL — include when data moves through multiple systems or transforms.
Describe, in plain English or a simple diagram, how data enters this block,
what happens to it, and where it goes. Use arrows:
  RD -> named pipe -> IPC adapter -> normalized message -> API handler -> audit log]

--------------------------------------------------------------------------------
INTERFACES EXPOSED
--------------------------------------------------------------------------------
[List every public trait, struct, function, or endpoint this block exposes to
other blocks. These are the contracts that downstream blocks depend on. Be
precise about names, inputs, outputs, and error behavior. No code — describe
behavior.]

Example:
  Trait: TransportAdapter
    - send(msg: OutboundMessage) -> Result<()>
        Serializes and writes msg to the underlying transport channel.
        Returns TransportError::Disconnected if the channel is closed.
    - recv() -> Result<InboundMessage>
        Blocks until a message arrives. Returns TransportError::Timeout after
        the configured deadline.

--------------------------------------------------------------------------------
DESIGN DECISIONS
--------------------------------------------------------------------------------
[CONDITIONAL — include when a non-obvious choice was made. State the chosen
approach and the reason. Keeps the agent from re-litigating decisions.

Examples:
  - SQLite with WAL mode chosen over Postgres. Reason: local-first, no server
    required, sufficient for current throughput targets.
  - Named pipes (Windows) and UDS (Unix) are primary transports, not HTTP.
    Reason: same-host IPC avoids network stack overhead and auth complexity.
  - Dry-run is on by default for all destructive actions. Reason: safety-first;
    user must explicitly opt in to live execution.]

--------------------------------------------------------------------------------
SAFETY & SECURITY NOTES
--------------------------------------------------------------------------------
[CONDITIONAL — include when the block handles auth, secrets, user input,
destructive operations, privilege escalation, or external data.
List specific requirements:
  - HMAC tokens must be verified before any control action is dispatched.
  - Secrets must never be logged, even at TRACE level.
  - All SQL queries must use parameterized statements (no string interpolation).
  - The dry_run flag must default to true; live execution requires an explicit
    override AND a confirmed auth token.]

--------------------------------------------------------------------------------
UNIT TESTS — WHAT TO VERIFY
--------------------------------------------------------------------------------
[List the behaviors that must be tested. Describe each test by what it checks,
not by its code. The agent writes the actual test code.

Examples:
  - AppConfig::from_env() returns an error when RDUP2_AUTH_SECRET is not set.
  - AppConfig::from_env() uses port 8090 when RDUP2_PORT is not set.
  - AppConfig::from_file() merges file values with env var fallbacks correctly.
  - Serializing AppConfig to TOML and deserializing produces identical values.]

--------------------------------------------------------------------------------
BLOCK COMPLETE WHEN
--------------------------------------------------------------------------------
[ ] [Each item is a concrete, verifiable statement of completion.]
[ ] [Example: All structs in config.rs compile with zero warnings.]
[ ] [Example: cargo test -p rdup2-core passes with all tests green.]
[ ] [Example: AppConfig::from_env() correctly reads all env vars.]
[ ] [Example: No secrets are printed to stdout or log output in tests.]

================================================================================

================================================================================
HOW TO APPLY THIS SYSTEM TO A NEW PROJECT
================================================================================

Step 1 — Read the Project Summary
  Every project has a Project Summary document. Read it fully before writing
  any blocks. Understand the purpose, the dependencies, the technology choices,
  and the full scope. Ask for clarification on anything ambiguous before
  writing blocks.

Step 2 — Identify the Crates/Packages
  List every distinct crate or package the project needs. These become your
  modules C through G (or however many are needed). Assign each one a letter.
  Order them by dependency: the crate with no dependencies gets the lowest
  letter. A crate that depends on three others gets a higher letter.

Step 3 — Plan the Module Layout
  Write a simple map:
    A = Scope
    B = Scaffold
    C = [crate name and role]
    D = [crate name and role]
    ...etc

  Verify the dependency ordering is correct before proceeding. If crate D
  depends on crate C, module D must come after module C.

Step 4 — Break Each Module Into Blocks
  Each module should contain 2–5 blocks. A block is the right size when:
    - It can be implemented and tested independently.
    - It has a single clear outcome.
    - It is not so large that it spans multiple files with unrelated purposes.
    - It is not so small that it only adds one trivial type.

  Natural block boundaries within a module:
    Block 1 — Core types and data structures for this crate
    Block 2 — Primary logic / main trait or interface implementation
    Block 3 — Secondary implementation (e.g., platform variant, adapter)
    Block 4 — Integration wiring / cross-crate connections
    (Add more if needed; each must have a single clear outcome.)

Step 5 — Assign Build Order Numbers
  Number all blocks globally across all modules in the order they should be
  implemented. Block A-1 is always build order 1. Count up through all modules.
  This gives the agent a single linear implementation sequence.

Step 6 — Write the Blocks
  Write one .txt file per block. Name it:  module [letter] block [number].txt
  Fill in every required section. Omit conditional sections only when they
  genuinely do not apply to that block. Maximize detail in the FILES and
  INTERFACES sections — the agent should not need to guess about field names,
  types, or behaviors.

Step 7 — Review the Full Set
  Before handing blocks to an agent:
    - Confirm every block's DEPENDS ON list is accurate.
    - Confirm there are no gaps (nothing needed by a later block that wasn't
      defined in an earlier one).
    - Confirm the BLOCK COMPLETE WHEN checklist is specific enough to verify.

================================================================================
RULES FOR AGENTS USING THIS SYSTEM
================================================================================

1. Complete blocks in order. Do not skip ahead. Do not implement block C-3
   before C-1 and C-2 are verified complete.

2. Verify "BLOCK COMPLETE WHEN" before marking a block done. Every checkbox
   must be checkable as true. If a test is failing, the block is not complete.

3. Do not add scope. A block defines exactly what to implement. Do not add
   extra structs, extra endpoints, or extra logic beyond what the block
   specifies. Extra code outside the block's scope belongs in a future block.

4. If a dependency is missing, stop and report it. If block D-2 requires a
   type from C-1 that does not exist, flag it. Do not invent a substitute.
   Go back to C-1 and confirm it was completed correctly.

5. Design decisions in a block are final for that block. If the block says
   "use SQLite," use SQLite. If you believe a decision is wrong, note it and
   ask — but do not unilaterally override it.

6. Use block coordinates in all communication. When referencing work, always
   say the coordinate (e.g., "C-2 is complete" or "B-1 needs the workspace
   Cargo.toml"). This prevents confusion across long sessions.

7. High detail beats short descriptions. When writing block descriptions,
   err toward more detail about behavior, field names, and types. The agent
   implementing the code should be able to read the block and start writing
   without pausing to research.

================================================================================
FILE NAMING CONVENTION
================================================================================

Block files:    module [letter] block [number].txt
                Examples:
                  module a block 1.txt
                  module c block 3.txt
                  module g block 2.txt

Folders:        Create one folder per project named "BSM instruction blocks"
                or "[Project Name] instruction blocks"
                All block files live flat inside this folder.
                No subfolders per module — the letter in the filename is enough.

================================================================================
EXAMPLE — MINIMAL 3-MODULE PROJECT LAYOUT
================================================================================

Project: a simple Rust CLI tool with a config crate, a network crate, and a
         CLI binary.

  Module A — Scope & Folder Tree         (2 blocks)
    A-1: What the project does, folder tree
    A-2: External dependencies and integration points

  Module B — Scaffold                    (1 block)
    B-1: Cargo workspace, crate stubs, Cargo.toml

  Module C — my-config crate             (2 blocks)
    C-1: Config structs, defaults, TOML load/save, error types
    C-2: Environment variable overrides, validation

  Module D — my-net crate                (3 blocks)
    D-1: Connection trait, message types
    D-2: TCP implementation of the connection trait
    D-3: Reconnect logic and unit tests

  Module E — my-cli binary               (2 blocks)
    E-1: CLI argument parsing, entry point wiring
    E-2: End-to-end integration test and CI config

Total blocks: 10
Build order: A-1, A-2, B-1, C-1, C-2, D-1, D-2, D-3, E-1, E-2

================================================================================
END OF TEMPLATE
================================================================================
