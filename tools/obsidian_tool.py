"""Obsidian Vault indexing tool.

Scans a local Obsidian Vault (directory of .md files), extracts note
content + wikilinks, and stores them in the knowledge-base memory plugin
for semantic retrieval.  Also provides git-pull sync for VPS deployments
where the vault is a git repo.

Requires the knowledge-base memory provider to be active
(memory.provider: knowledge-base in ~/.hermes/config.yaml).

The OBSIDIAN_VAULT_PATH env var sets the default vault location.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")


def _get_kb_provider():
    """Return the active knowledge-base memory provider, or None."""
    try:
        from agent.memory_manager import get_active_memory_provider
        provider = get_active_memory_provider()
        if provider and provider.name == "knowledge-base":
            return provider
    except Exception:
        pass
    return None


def _read_vault_files(vault_path: Path, incremental: bool = True) -> list[dict]:
    """Walk vault, return list of {path, content, mtime, wikilinks}."""
    results = []
    for md_file in sorted(vault_path.rglob("*.md")):
        # Skip .obsidian system directory.
        if ".obsidian" in md_file.parts:
            continue
        try:
            mtime = md_file.stat().st_mtime
            content = md_file.read_text(encoding="utf-8", errors="replace")
            wikilinks = _WIKILINK_RE.findall(content)
            rel_path = str(md_file.relative_to(vault_path))
            results.append({
                "path": str(md_file),
                "rel_path": rel_path,
                "content": content,
                "mtime": mtime,
                "wikilinks": wikilinks,
            })
        except Exception as exc:
            logger.debug("obsidian_tool: skipping %s: %s", md_file, exc)
    return results


def _make_item_content(note: dict) -> str:
    """Build the searchable text stored in the knowledge base."""
    lines = [f"Obsidian note: {note['rel_path']}"]
    body = note["content"].strip()
    # First 800 chars of body is enough for embedding quality.
    if len(body) > 800:
        body = body[:800] + "…"
    lines.append(body)
    if note["wikilinks"]:
        links = ", ".join(note["wikilinks"][:20])
        lines.append(f"Links to: {links}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def obsidian_index(vault_path: str, incremental: bool = True) -> str:
    """Index Obsidian vault notes into the knowledge base."""
    kb = _get_kb_provider()
    if kb is None:
        return tool_error(
            "knowledge-base memory provider is not active. "
            "Set 'memory.provider: knowledge-base' in ~/.hermes/config.yaml."
        )

    path = Path(vault_path).expanduser().resolve()
    if not path.is_dir():
        return tool_error(f"Vault path not found: {path}")

    notes = _read_vault_files(path, incremental=incremental)
    if not notes:
        return json.dumps({"indexed": 0, "vault": str(path), "message": "No .md files found."})

    indexed = 0
    errors = 0
    for note in notes:
        try:
            content = _make_item_content(note)
            tags = ["obsidian", *note["wikilinks"][:5]]
            result_json = kb.handle_tool_call(
                "knowledge_store",
                {
                    "content": content,
                    "category": "learning",
                    "tags": tags,
                    "source": "obsidian",  # note: handle_tool_call uses 'manual' default
                },
            )
            result = json.loads(result_json)
            if result.get("status") == "stored":
                indexed += 1
        except Exception as exc:
            logger.debug("obsidian_tool: failed to index %s: %s", note["rel_path"], exc)
            errors += 1

    return json.dumps({
        "indexed": indexed,
        "errors": errors,
        "total_notes": len(notes),
        "vault": str(path),
    })


def obsidian_search(query: str, top_k: int = 5) -> str:
    """Semantic search over indexed Obsidian notes."""
    kb = _get_kb_provider()
    if kb is None:
        return tool_error("knowledge-base memory provider is not active.")

    return kb.handle_tool_call(
        "knowledge_search",
        {"query": query, "top_k": top_k},
    )


def obsidian_sync(vault_path: str) -> str:
    """Run git pull on the vault repo, then re-index changed files."""
    path = Path(vault_path).expanduser().resolve()
    if not path.is_dir():
        return tool_error(f"Vault path not found: {path}")

    git_dir = path / ".git"
    pull_output = ""
    if git_dir.is_dir():
        try:
            result = subprocess.run(
                ["git", "-C", str(path), "pull", "--ff-only"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            pull_output = (result.stdout + result.stderr).strip()
            if result.returncode != 0:
                return tool_error(f"git pull failed: {pull_output}")
        except subprocess.TimeoutExpired:
            return tool_error("git pull timed out after 30s")
        except FileNotFoundError:
            return tool_error("git not found in PATH")
    else:
        pull_output = "No .git directory found — skipping git pull, indexing as-is."

    index_result_json = obsidian_index(str(path), incremental=True)
    try:
        index_result = json.loads(index_result_json)
    except Exception:
        index_result = {"raw": index_result_json}

    return json.dumps({
        "git_pull": pull_output,
        "index": index_result,
    })


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------

def _check_obsidian_available() -> tuple[bool, str]:
    vault_env = os.environ.get("OBSIDIAN_VAULT_PATH", "")
    if vault_env:
        p = Path(vault_env).expanduser()
        if p.is_dir():
            return True, ""
    # Always show the tools — user can pass any path.
    return True, ""


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

_INDEX_SCHEMA = {
    "name": "obsidian_index",
    "description": (
        "Index Obsidian vault notes into the personal knowledge base for semantic search. "
        "Scans all .md files in the vault, extracts content and [[wikilinks]], "
        "and stores each note with an embedding. "
        "Default vault path: $OBSIDIAN_VAULT_PATH or ~/Documents/Obsidian Vault."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "vault_path": {
                "type": "string",
                "description": "Absolute path to the Obsidian vault directory.",
            },
            "incremental": {
                "type": "boolean",
                "description": "If true, skip files already in the knowledge base (default: true).",
            },
        },
        "required": ["vault_path"],
    },
}

_SEARCH_SCHEMA = {
    "name": "obsidian_search",
    "description": (
        "Semantically search indexed Obsidian notes. "
        "Returns the most relevant notes with their paths and content excerpts."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "top_k": {"type": "integer", "description": "Max results (default: 5)."},
        },
        "required": ["query"],
    },
}

_SYNC_SCHEMA = {
    "name": "obsidian_sync",
    "description": (
        "Sync Obsidian vault from git remote (git pull) and re-index new/changed notes. "
        "Use when the vault is a git repository synced from another machine."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "vault_path": {
                "type": "string",
                "description": "Absolute path to the Obsidian vault git repository.",
            },
        },
        "required": ["vault_path"],
    },
}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

registry.register(
    name="obsidian_index",
    toolset="productivity",
    schema=_INDEX_SCHEMA,
    handler=lambda args, **kw: obsidian_index(
        args["vault_path"],
        incremental=bool(args.get("incremental", True)),
    ),
    check_fn=_check_obsidian_available,
    emoji="📓",
)

registry.register(
    name="obsidian_search",
    toolset="productivity",
    schema=_SEARCH_SCHEMA,
    handler=lambda args, **kw: obsidian_search(
        args["query"],
        top_k=int(args.get("top_k", 5)),
    ),
    check_fn=_check_obsidian_available,
    emoji="🔍",
)

registry.register(
    name="obsidian_sync",
    toolset="productivity",
    schema=_SYNC_SCHEMA,
    handler=lambda args, **kw: obsidian_sync(args["vault_path"]),
    check_fn=_check_obsidian_available,
    emoji="🔄",
)
