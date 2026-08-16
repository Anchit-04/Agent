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

### 1. ✅ Sandboxing — merged (PR #1, `68df41e`)
Real OS-level containment for tool execution, so the agent can't touch
anything outside the project or run away with resources.
- `core/file_tools.py` — `_resolve()` enforces WORKDIR containment (blocks
  `..` traversal and absolute-path escapes) for all file tools.
- `core/sandboxd/` — Go binary using Windows Job Objects: hard memory
  ceiling + guaranteed whole-process-tree kill on timeout. Verified via
  `main_test.go` (timeout kills the tree) and `memlimit_test.go` (memory cap
  actually enforced). Output capture is now bounded (2MB) so a chatty
  command can't grow sandboxd's own memory unboundedly — that growth wasn't
  covered by the Job Object limit, which only bounds the child process.
- `core/sandbox_client.py` — long-lived Python↔sandboxd bridge (one process
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

### 2. ✅ Provider abstraction (multi-model support)
`core/providers/` — neutral `Turn`/`ToolCall`/`ToolResult` types
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
`core/context.py` — `MAX_ITERATIONS` (hard stop) and `COMPACT_EVERY` (soft
reset: abandon the growing turn history, replace it with a compact summary
built from the todo checklist + most recent tool results). Needed because
some providers (Gemini's Interactions API) manage history server-side with
no way to selectively drop old turns.

### 4. 🟡 API Vault & Routing
- **Vault (partial):** `providers/config.py`'s `MODEL_REGISTRY` is the
  config surface — each entry names the env var holding its key, real
  values live in `.env`. This is a developer-facing vault, not a
  user-facing one: it's a plaintext local `.env` file, not secure per-user
  cloud storage, and only `GEMINI_API_KEY` is actually populated right now
  (Anthropic/DeepSeek/Moonshot are registered but keyless).
- **Routing (not started):** model selection today is a manual CLI arg
  (`python agent.py "task" deepseek-chat`). No logic yet to route different
  steps of a task to different models automatically.
- **Remaining work:** real per-user encrypted key storage; a
  vault UI/API; routing logic (rule-based or orchestrator-directed) to pick
  which model handles which step.

### 5. ⬜ Orchestrator, executors & shared memory
Not started. This is the core multi-agent feature:
- Orchestrator agent (strongest model) plans and delegates.
- Executor agents (cheaper models) receive individual tasks and execute
  using the existing tool set (bash, file tools, todo).
- A shared memory space all agents read/write so executors' work stays
  visible to the orchestrator and to each other, instead of each subagent
  working from an isolated context.
- Builds directly on #2 (provider abstraction) and #4 (routing).

### 6. ⬜ Terminal UI
Not started. Currently just `print()` statements with ANSI color codes in
`agent.py` (colored role labels, inline confirmation prompts via `input()`).
No real TUI framework (e.g. a `curses`/`rich`/`textual`-based interface) —
no persistent layout, no multi-agent view (which matters once #5 lands and
there are several subagents running concurrently to show).

### 7. ⬜ Cloud deployment
Not started. No Dockerfile, no infra/CI config, nothing cloud-facing yet —
this is currently a local CLI script (`python core/agent.py "..."`) run
against a local `.env`.

## Open questions for Anchit
- Any ordering preference, e.g. does Terminal UI need to land before or
  after the orchestrator so you can actually watch it work?
