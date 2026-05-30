# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## ⛔️ 部署铁律 (MUST READ — 最高优先级)

**永远禁止用 `scp` / `rsync` 直接传文件到服务器。** 任何代码或 skills 变更，一律走以下流程：

```bash
# 1. 本地提交
git add -p && git commit -m "feat/fix: ..."
git push origin main

# 2. 登录服务器更新
ssh ubuntu@122.51.188.133
cd /opt/hermes && git pull

# 3a. 只改了 Python 文件 / skills / config → 直接重启
sudo systemctl restart hermes

# 3b. 改了 pyproject.toml / uv.lock（新增依赖）→ 先重新安装再重启
uv sync --frozen --extra all
sudo systemctl restart hermes
```

**严禁**任何形式的 `scp skills/...`、`scp .env`、`rsync src/` 绕过 git。

**Why：** 半部署事故——只传了单个文件却没同步依赖/skill/config，导致服务器与代码库不一致、定时任务静默失效、难以回滚。`git pull` 保证服务器与 main 逐文件一致、可追溯、可回滚。

### 服务管理速查

```bash
sudo systemctl status hermes      # 查看服务状态
sudo systemctl restart hermes     # 重启服务
sudo systemctl stop hermes        # 停止服务
journalctl -u hermes -f           # 实时查看日志
journalctl -u hermes -n 100       # 查看最近 100 行日志
```

服务使用 `Restart=always` + `RestartSec=5`，崩溃后 5 秒自动重启。API Key 等敏感配置存于 `/home/ubuntu/.hermes/.env`，**不进 git**。

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
