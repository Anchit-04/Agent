# Plan: Cloud-Based Coding Harness

## Product vision

A cloud-hosted coding agent harness with a **master orchestrator / executor**
architecture:

- The user drops 3-4 API keys into an **API Vault**.
- The **strongest** model available (e.g. Claude Opus) acts as the
  **orchestrator** — it plans and directs the work.
- **Cheaper models** (Kimi, DeepSeek, etc.) act as **executors** — they carry
  out the orchestrator's individual tasks.
- All agents (orchestrator + executors) read/write a **shared memory space**
  so work stays coordinated across models instead of each one operating
  blind.

## Working arrangement

Anchit (junior engineer) hand-writes code on their own branch and raises a
PR. Claude (senior engineer) reviews and approves/merges. Claude does not
write code directly on the feature work covered by this arrangement — Claude
picks up infra/plumbing/verification work between PRs, and reviews when a PR
lands.

> `gh` CLI is installed (portable, user-level — see
> `AppData\Local\Programs\gh-cli`) and authenticated as Anchit-04, so PR
> review/merge goes through `gh` directly now. One wrinkle: Claude and Anchit
> share the same GitHub account, so GitHub blocks a formal "approved" review
> status on Claude's own PRs (self-approval) — review verdicts get posted as
> a PR comment instead, not a real review-gate. Fine for now; would need a
> second account/bot token if that gate needs to mean something at the
> platform level later.

## Status legend

✅ done · 🟡 partial · ⬜ not started

## The 7 phases

### 1. ✅ Sandboxing — merged (PR #1, `68df41e`; hardened further in PR #2, `3de6705`)
Real OS-level containment for tool execution, so the agent can't touch
anything outside the project or run away with resources.
- `loom/tools/file_tools.py` — `_resolve()` enforces WORKDIR containment (blocks
  `..` traversal and absolute-path escapes) for all file tools.
- `loom/sandbox/sandboxd/` — Go binary using Windows Job Objects: hard memory
  ceiling + guaranteed whole-process-tree kill on timeout. Verified via
  `main_test.go` (timeout kills the tree) and `memlimit_test.go` (memory cap
  actually enforced). Output capture is now bounded (2MB) so a chatty
  command can't grow sandboxd's own memory unboundedly — that growth wasn't
  covered by the Job Object limit, which only bounds the child process.
- `loom/sandbox/sandbox_client.py` — long-lived Python↔sandboxd bridge (one process
  per session, restarts transparently if it dies). Every sandboxd failure
  mode is now handled explicitly rather than crashing the agent: missing
  binary, a hung sandboxd (client-side timeout backstop), a malformed
  response line, and a real wait()/kill() on shutdown instead of just
  closing stdin.
- Fixed along the way: Go's Windows argv escaping was mangling any `"` in
  commands before `cmd.exe` saw them — silently broke `git commit -m "..."`,
  `python -c "..."`, etc. Fixed via `SysProcAttr.CmdLine`.
- First PR through the real review/fix/merge cycle: Anchit opened PR #1,
  Claude's high-effort review surfaced 6 substantive bugs (4 crash-risk,
  2 robustness gaps), Claude pushed fix commits to the same branch, and it
  merged into `main` as a regular merge (`68df41e`) preserving the atomic
  commit history.
- PR #2: two more bugs surfaced by actually running the merged agent live
  against the real Gemini API — `loom/paths.py` now centralizes `WORKDIR`
  so it's the repo root regardless of which directory `agent.py` is
  launched from (previously relative to invocation cwd, so running from
  `loom/` silently broke every file path); `list_directory` added to
  `SAFE_TOOLS` and `confirm()` now fails safe on `EOFError` instead of
  crashing when there's no interactive stdin to ask on. Filed and fixed by
  Claude as verification follow-up, same self-review/self-merge pattern as
  PR #1 (still can't post a formal GitHub "approved" review — shared
  account, self-approval blocked).

### 2. ✅ Provider abstraction (multi-model support)
`loom/providers/` — neutral `Turn`/`ToolCall`/`ToolResult` types
(`base.py`), adapters for Anthropic, Gemini, and OpenAI-compatible
(`anthropic.py`, `gemini.py`, `openai_compatible.py`), and a single registry
(`config.py`) mapping short keys → provider + model id + env var. `agent.py`
never imports a provider SDK directly — swapping/adding a model is a config
change, not a rewrite. This is the precondition for both the API Vault and
the orchestrator/executor split. Hardened as part of PR #1's review: the
OpenAI-compatible adapter (DeepSeek/Kimi) no longer crashes the whole agent
if a cheaper model returns truncated/malformed tool-call JSON — it surfaces
as a recoverable tool error instead.

### 3. ✅ Context management
`loom/context.py` — `MAX_ITERATIONS` (hard stop) and `COMPACT_EVERY` (soft
reset: abandon the growing turn history, replace it with a compact summary
built from the todo checklist + most recent tool results). Needed because
some providers (Gemini's Interactions API) manage history server-side with
no way to selectively drop old turns.

### 4. 🟡 API Vault & Routing — primitives merged (PR #3, `19b6657`)
- **Vault (working, still local-only by design):** `loom/config/vault.py` —
  `get_key()`/`is_present()` (cheap, local, no network) vs `validate_key()`
  (real API auth check via the new `Provider.validate()` in
  `providers/base.py`, cached per-session). `list_models()` /
  `available_models(require_validated=...)` give a real status view instead
  of trusting "env var is non-empty" as "key works". Keys still live in
  `.env` — deliberately not building encrypted/multi-user storage yet,
  since there's no backend to protect it *for* until phase 7. Still only
  `GEMINI_API_KEY` actually populated right now.
- **Routing (working, role-based, not task-based):** `loom/config/routing.py` —
  `MODEL_REGISTRY` entries now carry a `tier` ("strong"/"cheap").
  `pick_orchestrator()` fails loud if no strong-tier key validates (no
  silent downgrade of the orchestrator role). `pick_executor()`/
  `pick_executors(n)` prefer cheap-tier, degrade to any present key if
  none exists, and never return duplicates (`exclude` set grows each
  pick). Deliberately role-based only — task-based/cost-aware routing has
  no orchestrator yet to consume it, so building it now would be
  speculative; that's phase 5's problem once phase 5 exists.
- **Remaining work:** nothing wires `agent.py`'s CLI to actually use
  routing yet (it still takes a manual `model_key` arg) — that's UX, not
  design, and deliberately deferred until phase 5 needs it. Real
  encrypted/multi-user key storage and any vault UI are phase 7 concerns.

### 5. ✅ Orchestrator, executors & shared memory — merged (PR #4, `429c4d8`)
- **Orchestrator (`loom/orchestrator.py`):** `run_orchestrator()` — same
  loop shape as `agent.py`'s `run_agent()`, but tool-restricted to
  `delegate_task`/`todo_write`/`memory_write`/`memory_read` only, enforced
  at the tool-list level (no bash/file access) so it can only plan, never
  execute directly. Picks its own model via `routing.pick_orchestrator()`.
- **Dispatch (`loom/dispatch.py`):** `ScopeScheduler` serializes tasks with
  overlapping declared file `scope` (via `threading.Condition`, not
  busy-polling); disjoint scope runs genuinely concurrently. `DependencyGraph`
  handles *logical* ordering (`depends_on`) separately from scope —
  same-batch dependents block until the real dependency completes and
  receive its result automatically; earlier-turn dependencies resolve
  straight from shared memory, no waiting needed. Cycle detection fails
  loud rather than deadlocking. A crashed executor thread still releases
  its scope reservation (try/finally) and surfaces as a clean tool-error,
  never an uncaught exception killing the batch — caught and fixed a real
  bug here mid-build where `pick_executor()`/`scheduler.acquire()` sat
  outside the try/except.
- **Shared memory (`loom/memory.py`, file: `SHARED_MEMORY.md`):** one
  central markdown log, not per-agent/embedding-based — every completed
  task's result is logged automatically; `memory_write`/`memory_read` let
  any agent leave/pull notes manually. Every agent gets the file's header
  lines (cheap) folded into its prompt every turn; full entries are pulled
  in on demand, not force-fed.
- **Model specialization (`loom/config/preferences.py`):** two ways to route a
  specific sub-task to a specific model — a one-off `model_key` on
  `delegate_task` (stated in the task itself), or a durable specialty tag
  (`set_specialty("kimi-k2", "frontend / motion")`) that's folded into the
  orchestrator's prompt every turn. Both funnel through the same
  `pick_executor(preferred=...)` path, which fails loud (not silent
  fallback) if the specifically-requested model isn't actually available.
  Gap: `set_specialty()` has no CLI/UI caller yet — set it by calling the
  function directly until phase 6 gives it a real home.
- Verified with real induced failures (overlap-vs-concurrency timing,
  crash-releases-lock, 40-way concurrent memory writes with zero
  corruption, cycle detection) plus one live run against the real Gemini
  API — genuine concurrent dispatch confirmed by two independent
  rate-limit hits, output verified on disk, not just self-reported. That
  live run used one real model in both roles (only `GEMINI_API_KEY` is
  configured) — the mechanism is proven, a true multi-model split isn't
  yet, since that needs a strong-tier key (Anthropic) plus at least one
  more cheap-tier key beyond Gemini.

### 6. 🟡 Terminal UI — in progress, NOT YET COMMITTED (working tree only, branch `anchit/sandboxd`)
Design fully worked out in the plan-mode file at
`C:\Users\Lenovo\.claude\plans\let-s-go-with-markdown-quirky-stream.md`
(client/server split over WebSocket, multi-session backend, hub-and-spoke +
Hybrid A peer channels, Go+Bubble Tea client). Backend event-sourcing
refactor done: `agent.py`/`orchestrator.py`/`dispatch.py` now take an
`event_sink` callback and emit structured events (`agent_turn`, `tool_call`,
`tool_result`, `task_created`, `task_status_changed`, `todo_updated`,
`memory_entry_added`, `human_message_injected`, `error`) instead of just
`print()`ing or staying silent. `todo_tool.py`/`memory.py` converted from
module-level globals to per-instance `TodoManager`/`Memory` classes (fixes a
real multi-session corruption bug). New `loom/injection.py`
(`InjectionQueue`) lets a human message reach a running orchestrator or a
specific executor mid-flight, drained once per loop iteration. New
`loom/server.py` — persistent multi-session WebSocket backend
(`websockets` — first real external pip dependency; **no
requirements.txt/pyproject.toml exists yet, flagged not fixed**), with
continuous per-session JSON persistence to `sessions/`. New `tui/` — a
separate Go module (Bubble Tea v2), currently a compiling rendering
scaffold only (chat window + discrete card-cycle + input bar, matching
Anchit's sketch) with **no WebSocket wiring to server.py yet** and no
command/keybinding scheme designed yet (deliberately deferred). Personas
and MCP connectivity (`loom/mcp_client.py`) are designed on paper in the
plan file but explicitly deferred, not started.

### 7. ⬜ Cloud deployment
Not started. No Dockerfile, no infra/CI config, nothing cloud-facing yet —
this is currently a local CLI script (`python loom/agent.py "..."`) run
against a local `.env`.

## Open questions for Anchit
- Any ordering preference, e.g. does Terminal UI need to land before or
  after the orchestrator so you can actually watch it work?
