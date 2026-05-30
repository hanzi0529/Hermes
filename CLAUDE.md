# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## ⛔️ 部署铁律 (MUST READ — 最高优先级)

永远禁止用 `scp` 把代码部署到服务器。 任何代码 / 技能变更，一律且只能走以下流程：

1. 本地 `git commit`
2. `git push origin main`
3. `ssh root@<YOUR_VPS_IP>`
4. 服务器上 `cd /opt/we-assistant && git pull`
5. `systemctl restart Hermes`（核心文件必须重启；skills 可以热重载，但稳妥起见也重启）

严禁任何形式的 `scp vps/src/...`、`scp .../skills/...`、`scp dashboard/out` 绕过 git。

**Why：** 曾发生「半部署」事故——只 scp 了 `index.ts` 没 cron/skill，导致服务器与代码库设计不一致、动态定时任务静默失效且难以排查。`git pull` 保证服务器与 main 逐文件一致、可追溯、可回滚。

## Project Overview

Hermes is a self-improving AI agent framework with a closed learning loop. It exposes three entry points: `hermes` (interactive CLI), `hermes-agent` (programmatic runner), and `hermes-acp` (VS Code/Zed/JetBrains ACP server).

## Setup & Install

```bash
# Automated (recommended)
./setup-hermes.sh

# Manual
uv venv .venv --python 3.11
source .venv/bin/activate
uv pip install -e ".[all,dev]"
```

User config lives at `~/.hermes/config.yaml`; API keys at `~/.hermes/.env` (see `.env.example` for all provider keys).

## Testing

Canonical test runner (matches CI exactly):

```bash
scripts/run_tests.sh                          # all tests, 4 workers
scripts/run_tests.sh tests/agent/             # specific directory
scripts/run_tests.sh tests/foo.py -- --tb=long  # single file + pytest flags
```

**All tests require real API keys** configured in `~/.hermes/.env`. There is no offline mock mode — tests call live providers. The script sets `TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0` for determinism and uses per-file subprocess isolation to prevent module-level state leakage.

## Linting

```bash
ruff check .
```

Only one rule is active: `PLW1514` (unspecified-encoding). All other rules are disabled. Per-file ignores apply to `tests/`, `skills/`, and `optional-skills/`.

## Dependency Pinning

All direct dependencies in `pyproject.toml` use **exact pins** (`==X.Y.Z`, no ranges). This is intentional supply-chain hardening. After editing versions in `pyproject.toml`, regenerate the lockfile:

```bash
uv lock
```

Do not introduce version ranges (`>=`, `~=`, `^`) for new dependencies.

## Code Style

- Always use `encoding="utf-8"` on every `open()` call — this is enforced by the single active ruff rule and prevents Windows locale bugs.
- No opinionated formatter (no black/autopep8). Match the style of the surrounding code.
- Type hints are present but not strictly enforced (`ty` linter is configured with `unknown-argument="warn"`).

## Architecture: Import Chain (circular-safe)

The import order must not be violated:

```
tools/registry.py  (no deps — central tool registry)
      ↑
tools/*.py         (import registry at module level; auto-discovered)
      ↑
model_tools.py     (imports tools.registry + all tool modules)
      ↑
run_agent.py, cli.py, batch_runner.py, ...
```

New tools call `registry.register()` at import time and are auto-discovered — do not import them eagerly in `model_tools.py`.

## Lazy Dependencies

Optional backends (anthropic, telegram, discord, slack, edge-tts, faster-whisper, modal, etc.) are lazy-loaded at first use via `tools/lazy_deps.py`. Do not add eager imports for optional features.

## Skills

- `skills/` — bundled skills, broadly useful, always active.
- `optional-skills/` — niche/heavier skills, discoverable but not active by default.
- **New memory providers are no longer accepted upstream**; they must be published as standalone plugins.

## Session Database

`hermes_state.py` implements `SessionDB` backed by SQLite + FTS5. Session titles are auto-generated on the first turn. Full-text search is available over session history.

## Branch & Commit Conventions

- **Branch naming:** `type/short-description` (e.g., `feat/add-tool`, `fix/memory-leak`)
- **Commits:** Conventional commits — `feat:`, `fix:`, `perf:`, `test:`, `refactor:`, `docs:`
- **PRs:** Must link a related issue, include a type checkbox, list changed files with rationale, and provide testing instructions. See `.github/PULL_REQUEST_TEMPLATE.md`.

## Contribution Priority

Bug fixes > cross-platform compatibility > security > performance > skills/tools > docs.
