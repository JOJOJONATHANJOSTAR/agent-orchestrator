# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/), and this
project adheres to [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-07-01

First public release — a distributable, cross-platform **"Claude plans + Codex implements"**
coding orchestrator (orchestrator-worker). Claude is the brain (read-only planning + review),
Codex is the hands (writes code), and an objective acceptance gate drives multi-round iteration.
Zero runtime dependencies (standard library only), Python ≥ 3.10.

### Added — core orchestration
- Plan → implement → gate chain → (gate-passing) review loop, with failure-mode routing
  (`empty_diff` / `gate_failed` / `review_revise`) generating targeted next-round instructions.
- Read-only isolation: Claude runs with `Read,Grep,Glob` only; code writing is Codex's job.
- Acceptance gate chain (`--gate name=cmd`, repeatable); structured review findings
  (file / locator / issue / fix); zero-dependency static-site smoke gate (`scripts/smoke_static.py`).

### Added — subtask DAG
- `--decompose`: split a large task into a dependency DAG, run in topological order.
- Per-subtask diff isolation; overall gate runs only on sink subtasks; failure propagates
  down the DAG (dependents skipped) with opt-in `--continue-on-fail`.

### Added — safety net & controls
- Per-round recoverable git snapshots; `--rollback-on-fail`.
- Cost / wall-clock budgets (`--budget-usd`, `--budget-seconds`); tiered timeouts.
- `stdin=DEVNULL` to prevent codex from hanging on input; guardrail so Codex does not
  self-commit (changes stay in the working tree for human review).
- Lightweight tiers `--no-plan` / `--no-review`; default is "review only when gates pass".

### Added — reporting
- Per-run `metrics.json` + self-contained `report.html` (inline SVG charts) + terminal summary.

### Added — distribution & packaging
- pip/pipx-installable with a console entry point `agent-orchestrate` (via `pyproject.toml`).
- Self-contained, redistributable Claude Code skill (`scripts/deploy.py`); conversational
  entry point (the assistant infers all CLI flags from a natural-language request).
- MIT license; cross-platform CI matrix (Linux / macOS / Windows × Python 3.10 / 3.12).

### Added — auth for managed sub-sessions
- `--auth-channel {auto,subscription,api}`, `--check-auth` preflight.
- `--setup-auth`: a **user-run** wizard (hidden `getpass` input, `chmod 600` / `icacls`) so the
  assistant never writes credential files or handles the secret — fixes distribution being
  blocked when another user's Claude (correctly) refuses to write a credential file.

### Added — subscription-friendly review
- `--review-context-budget` (default 40000): trims the review diff to a budget (drops
  lockfiles/generated/binary, keeps by acceptance relevance, rest as stats) so review runs
  within the subscription channel's context limits instead of degrading to "Codex alone".
- `--review-model`: a cheaper model for the review call only.

### Fixed — surfaced by real runs
- Codex flag: `--full-access` (nonexistent in codex-cli 0.142.0) → `--dangerously-bypass-approvals-and-sandbox --skip-git-repo-check`.
- `decompose` crash on model JSON shape variance (bare array / wrapper) — tolerant parsing.
- Windows log mojibake — unified subprocess decode (force UTF-8, fall back to GBK, strip ANSI).
- Diff missing newly-created (untracked) files — intent-to-add before diffing.
- Stale PATH on long-lived sessions (Windows) — refresh from registry in the skill entry.

### Engineering
- Layered `orchestrator/` package (cli → engine → planner → agents/gates/gitrepo →
  prompts/graph → process/config/budget/artifacts/util), dependency-injected, no mutable
  globals, interface-based fakes for offline dry-run; zero-dependency test suite.

[0.1.0]: https://github.com/JOJOJONATHANJOSTAR/agent-orchestrator/releases/tag/v0.1.0
