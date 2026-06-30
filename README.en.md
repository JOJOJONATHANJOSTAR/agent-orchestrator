# Claude + Codex Orchestrator

[中文](README.md) · **English**

A minimal, working **"Claude plans + Codex implements"** orchestrator (orchestrator-worker
pattern). A single script wires two command-line AI agents into an automated loop that edits
code: Claude is the brain (read-only, planning + review), Codex is the hands (writes code), and
the script in the middle passes structured messages, runs acceptance gates, controls the loop,
and guards against infinite cycles.

```
requirement ──▶ Claude plans ──▶ ┌──────────── loop (≤ max-rounds) ────────────┐
                                 │ Codex edits ──▶ gates(tests/lint) ──▶ review │
                                 └──── passed & review pass ? ─yes─▶ done / no─▶ next ─┘
```

## Roles

| Role | Responsibility | Permissions |
|------|----------------|-------------|
| **Claude** (brain) | Break down the requirement → produce an implementation brief + acceptance criteria; review each round's diff | Read-only `Read,Grep,Glob`, never touches code |
| **Codex** (hands) | Edit code per the brief | `--dangerously-bypass-approvals-and-sandbox` |
| **Orchestrator** (this package) | Pass structured messages, run gates, control rounds, budget, snapshots | — |

## Project layout

Code is split by responsibility into the `orchestrator/` package, with a single top-down
acyclic dependency direction. External agents and gates are abstracted behind interfaces
(`LLM` / `Coder` / `Gates`); `--dry-run` injects `fakes`, and there is no mutable global state:

```
claude_codex_orchestrator.py   # backward-compatible entry point (= python -m orchestrator)
orchestrator/
├── cli.py        Composition layer: parse args, dependency injection, run the whole flow
├── engine.py     Orchestration: SubtaskRunner (multi-round loop per subtask) + DagEngine (DAG driver)
├── planner.py    Domain layer: Planner (plan/decompose), Reviewer (review)
├── agents.py     Adapters: LLM/Coder interfaces + ClaudeClient/CodexClient/JsonAgent
├── gates.py      Adapters: GateRunner (acceptance-gate chain) + summary/detail formatting
├── gitrepo.py    Adapters: GitRepo (git snapshot / rollback)
├── fakes.py      Adapter doubles: used by dry-run, implement the same interfaces
├── prompts.py    Pure logic: system prompts, review-checklist rendering, failure-mode routing
├── graph.py      Pure logic: DAG topological sort, context assembly
├── process.py    Infrastructure: unified subprocess exec/decode (force UTF-8, fall back to GBK, strip ANSI)
├── config.py     Infrastructure: Config dataclass + command-line parsing
├── budget.py     Infrastructure: cost/time budget ledger
├── artifacts.py  Infrastructure: persist each round's artifacts to disk
└── util.py       Infrastructure: console encoding, balanced-bracket JSON parsing
```

## Prerequisites

- Python ≥ 3.10
- [`claude`](https://docs.claude.com/claude-code) (Claude Code) and `codex` (Codex CLI) installed and logged in
- Run inside the target **git repository** (non-git repos also work, but you lose per-round snapshot/rollback)
- A clean working tree is preferred, so `git diff` cleanly shows each round's changes
- An acceptance-gate command configured (default `pytest -q`)

## Usage

```bash
# Simplest
python claude_codex_orchestrator.py "Switch the user module's passwords to salted bcrypt hashing, with tests"

# Specify repo / gate / rounds / model
python claude_codex_orchestrator.py "requirement…" \
    --repo ../app --test-cmd "pytest -q" --max-rounds 4 --model opus

# Gate chain: multiple independent gates, run one by one with separate feedback (any failure = not done)
python claude_codex_orchestrator.py "requirement…" \
    --gate lint="ruff check ." --gate types="mypy ." --gate tests="pytest -q"

# Subtask DAG: break a large requirement into dependent subtasks, implement in topological order
python claude_codex_orchestrator.py "Build a todo app with login" --decompose

# Static-site smoke gate: after build, verify every local asset reference in HTML/CSS actually exists
# (catches "build passes but 404s at runtime")
python claude_codex_orchestrator.py "Rework the landing page" \
    --gate build="npm run build" --gate smoke="python scripts/smoke_static.py dist"

# Add cost and time budgets; roll back the working tree on final failure
python claude_codex_orchestrator.py "requirement…" \
    --budget-usd 2.0 --budget-seconds 1800 --rollback-on-fail

# No real model calls — run the whole flow with fake agents (self-test / demo)
python claude_codex_orchestrator.py "write whatever" --dry-run
```

### `--dry-run`: offline self-test

When Codex isn't installed, or you're not in a git repo, `--dry-run` injects **deterministic
fake agents** (fake Claude / fake Codex / fake gates) and runs the full **orchestration
skeleton** — plan → multi-round implement → review → done — without calling any real model. Use it to:

- Quickly regression-check the control flow after editing the script
- Demo what the framework looks like without spending tokens
- See multi-round flow: the fake script is designed to "revise on round 1, pass on round 2"

## Command-line arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `task` (positional) | — | The requirement to complete (required) |
| `--repo` | `.` | Target repository path |
| `--test-cmd` | `pytest -q` | Single gate command (used when `--gate` is not given) |
| `--gate name=cmd` | — | One link in the gate chain, repeatable; any failure = not done. Overrides `--test-cmd` |
| `--max-rounds` | `3` | Max implement→review rounds, guards against infinite loops |
| `--model` | default | The `--model` passed to `claude` (not Codex!) |
| `--codex-model` | default | The model for `codex` (`codex exec -m`), e.g. `gpt-5.5-codex` |
| `--codex-config k=v` | — | Config override passed to `codex` (`codex exec -c`), repeatable, e.g. `model_reasoning_effort=medium` to speed up |
| `--json-retries` | `2` | Extra retries when an agent fails to return valid JSON |
| `--claude-timeout` | `600` | Timeout for a single claude call (seconds) |
| `--codex-timeout` | `600` | Timeout for a single codex call (seconds) |
| `--gate-timeout` | `1200` | Timeout for a single gate command (seconds); timeout is treated as a failure |
| `--budget-usd` | `0` (unlimited) | Cumulative cost cap (USD, per claude's reported cost); stops early when exceeded |
| `--budget-seconds` | `0` (unlimited) | Cumulative time cap (seconds); stops early when exceeded |
| `--rollback-on-fail` | off | On final failure, roll the working tree back to the last gate-passing snapshot (otherwise the start) |
| `--continue-on-fail` | off | When a subtask fails, still attempt its downstream (warn only, don't skip the whole branch) |
| `--decompose` | off | First break the requirement into a subtask DAG, implement in topological order (failure only affects downstream) |
| `--dry-run` | off | Run the flow with fake agents, no real model calls |

## Artifacts and recoverability

- **Per-round logs** are written to `runs/<timestamp>/`: the task, plan JSON, and each round's
  instruction / Codex stdout·stderr / diff / gate output / review reply (including every raw
  reply from JSON retries). This is the first place to look when debugging.
- **Per-round snapshots** (git repos only): `git stash create` produces a detached commit,
  tagged `orch/<run-id>/round<N>_after`, **without touching the working tree**. Any round can be
  restored:
  ```bash
  git stash apply orch/<run-id>/round2_after
  ```
- **Auto-rollback** (`--rollback-on-fail`): on final failure, restores tracked files to the "last
  gate-passing" snapshot; before rolling back it tags `orch/<run-id>/pre_rollback`, so the
  rollback itself is reversible.

On success, changes **stay in the working tree** (not auto-committed) — review them manually
before committing.

## Robustness by design

- **Forced structured handoff**: the two agents exchange only JSON. `extract_json` scans for the
  first balanced JSON value, tolerating surrounding text / markdown fences / `}` inside strings /
  multiple blocks; on a parse failure it appends a correction prompt and **retries automatically**
  (`--json-retries`) instead of crashing.
- **Read-only isolation**: Claude gets only `Read,Grep,Glob`, guaranteeing that "writing code" is
  done solely by Codex.
- **Gate chain**: `--gate` splits tests / lint / type checks into independent gates, run one by
  one with pass/fail recorded separately; any failure = not done, and only the **failing** gates'
  output is fed back to Codex for sharper focus.
- **Static-site smoke gate** (`scripts/smoke_static.py`): a ready-made, dependency-free
  (stdlib-only) gate that walks all HTML/CSS in the build output and verifies local asset
  references (`img/script/link/source` + `url()` + `srcset`) all resolve to real files — covering
  the "build passes but 404s at runtime" blind spot (external/anchor refs are skipped). Use it as
  a `--gate`.
- **Structured review**: the reviewer outputs a `findings` list (file / locator / issue / fix
  instruction), rendered into itemized instructions for Codex rather than free-form prose.
- **Failure-mode routing**: each round's failure is classified as `empty_diff` (no changes) /
  `gate_failed` (gates didn't pass) / `review_revise` (gates passed but review wants changes), and
  a targeted next-round instruction is generated for each.
- **Subtask DAG** (`--decompose`): planning breaks a large requirement into subtasks with `deps`;
  after topological sort each runs the implement→gates→review loop; downstream subtasks receive
  the context of completed upstream ones; when a subtask fails, **only the subtasks depending on
  it are skipped** (unless `--continue-on-fail`), and independent branches proceed normally.
  Validates id uniqueness / dependency existence / acyclicity.
- **Max rounds + budget**: dual guards against infinite loops / runaway cost (the budget is shared
  across the whole DAG).
- **Cross-platform output**: at startup stdout/stderr are reconfigured to UTF-8 (line-buffered),
  avoiding crashes when a Windows GBK console hits characters like `▶ ✅ 🎉`.

## Progress / future directions

Done: "runs reliably end-to-end + engineering safety net + gate chain / structured review /
failure routing / subtask DAG". The larger vision (echoing the repo name
`agent_corporation_framework`):

- **Layer 3**:
  - **Parallel workers**: implement mutually independent subtasks in a DAG concurrently (currently
    serial in topological order).
  - **Pluggable roles**: workers needn't be Codex (swap in Aider / another Claude / a local model).
  - **Shared blackboard state**: a `state.json` recording tasks / decisions / history so agents
    truly share context.
  - **Human-in-the-loop (HITL) checkpoints**: pause for confirmation at critical points (before
    merging, before deleting files).
  - **Web/TUI dashboard**: visualize what each agent is doing, DAG progress, diffs, gate status.

## Compatibility / fixed issues

- Fixed garbled logs by forcing subprocesses to use UTF-8 in the current setup
- Captures raw bytes, decoding UTF-8 first and falling back to the system locale encoding (GBK)
- Fixed a `WinError 206` (command line too long) crash by passing the (diff-heavy) review prompt
  to `claude` via stdin instead of as a command-line argument

## ⚠️ Safety note

`codex exec … --dangerously-bypass-approvals-and-sandbox` skips step-by-step approval and has full
read/write/execute access to the working tree. Run it only in a **controlled / isolated
repository**, and review all changes manually before committing.
