"""KnowledgeBaseProvider — MemoryProvider implementation.

Stores and retrieves fragmented knowledge points via SQLite + DashScope
multimodal embeddings.  Works alongside the built-in MEMORY.md tool:
  - MEMORY.md  → always-on short-term context (env facts, preferences)
  - knowledge-base → long-term semantic store (arbitrary knowledge points)
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

from . import db as _db_module
from . import embedding as _emb
from . import retrieval as _ret

logger = logging.getLogger(__name__)

_CATEGORIES = ("work", "life", "idea", "meeting", "learning", "general")

# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

_KNOWLEDGE_STORE_SCHEMA = {
    "name": "knowledge_store",
    "description": (
        "Save a knowledge point to the personal knowledge base. "
        "Use this to remember facts, decisions, notes, or any information "
        "the user would want to recall later. "
        "The content is embedded for semantic search — be descriptive.\n\n"
        "Categories: work, life, idea, meeting, learning, general.\n"
        "For images, pass the file path or URL as media_url; "
        "a text description should go in content."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The knowledge point to store (text description).",
            },
            "category": {
                "type": "string",
                "enum": list(_CATEGORIES),
                "description": "Category for organisation (default: general).",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tags for filtering.",
            },
            "media_url": {
                "type": "string",
                "description": "Optional image path or URL associated with this item.",
            },
        },
        "required": ["content"],
    },
}

_KNOWLEDGE_SEARCH_SCHEMA = {
    "name": "knowledge_search",
    "description": (
        "Semantically search the personal knowledge base. "
        "Use BEFORE answering questions about stored information. "
        "Returns the most relevant items ranked by semantic similarity."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to search for.",
            },
            "category": {
                "type": "string",
                "enum": list(_CATEGORIES),
                "description": "Restrict search to a category (optional).",
            },
            "top_k": {
                "type": "integer",
                "description": "Maximum results to return (default: 5).",
            },
        },
        "required": ["query"],
    },
}

_KNOWLEDGE_LIST_SCHEMA = {
    "name": "knowledge_list",
    "description": "List recently stored knowledge items, optionally filtered by category.",
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": list(_CATEGORIES),
                "description": "Filter by category (optional).",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum items to return (default: 20).",
            },
        },
    },
}

_KNOWLEDGE_DELETE_SCHEMA = {
    "name": "knowledge_delete",
    "description": "Delete a knowledge item by its ID.",
    "parameters": {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": "The item ID to delete.",
            },
        },
        "required": ["id"],
    },
}


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class KnowledgeBaseProvider(MemoryProvider):
    """Personal knowledge base with semantic search via DashScope embeddings."""

    def __init__(self) -> None:
        self._db: Optional[_db_module.KnowledgeDB] = None
        self._prefetch_result: str = ""
        self._prefetch_thread: Optional[threading.Thread] = None
        self._hermes_home: str = ""

    @property
    def name(self) -> str:
        return "knowledge-base"

    def is_available(self) -> bool:
        # SQLite always available; DASHSCOPE_API_KEY needed only for embeddings
        # (falls back to FTS5 without it).
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        hermes_home = kwargs.get("hermes_home", "")
        if not hermes_home:
            from hermes_constants import get_hermes_home
            hermes_home = str(get_hermes_home())
        self._hermes_home = hermes_home
        db_path = os.path.join(hermes_home, "knowledge_store.db")
        self._db = _db_module.KnowledgeDB(db_path)
        logger.info(
            "knowledge-base: initialized, %d items in store", self._db.count()
        )

    def system_prompt_block(self) -> str:
        if not self._db:
            return ""
        count = self._db.count()
        has_key = bool(os.environ.get("DASHSCOPE_API_KEY", "").strip())
        embed_status = "semantic search active" if has_key else "FTS search only (set DASHSCOPE_API_KEY for semantic)"
        return (
            f"# Personal Knowledge Base\n"
            f"Active. {count} items stored. {embed_status}.\n"
            f"ALWAYS call knowledge_search before answering questions about stored information.\n"
            f"Call knowledge_store to save any fact, note, or decision worth remembering.\n"
            f"Categories: {', '.join(_CATEGORIES)}."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        # Return the result queued by queue_prefetch() in the previous turn,
        # or do a synchronous search if none is queued.
        if self._prefetch_result:
            result = self._prefetch_result
            self._prefetch_result = ""
            return result
        if not self._db or not query:
            return ""
        return self._do_search(query, top_k=3, threshold=0.45)

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Kick off background embedding + search for the next turn."""
        if not self._db or not query:
            return

        def _bg():
            self._prefetch_result = self._do_search(query, top_k=3, threshold=0.45)

        self._prefetch_thread = threading.Thread(target=_bg, daemon=True)
        self._prefetch_thread.start()

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages=None,
    ) -> None:
        # Knowledge is stored explicitly via knowledge_store tool calls.
        # on_memory_write mirrors built-in memory writes automatically.
        pass

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            _KNOWLEDGE_STORE_SCHEMA,
            _KNOWLEDGE_SEARCH_SCHEMA,
            _KNOWLEDGE_LIST_SCHEMA,
            _KNOWLEDGE_DELETE_SCHEMA,
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if not self._db:
            return tool_error("Knowledge base not initialized")
        try:
            if tool_name == "knowledge_store":
                return self._handle_store(args)
            elif tool_name == "knowledge_search":
                return self._handle_search(args)
            elif tool_name == "knowledge_list":
                return self._handle_list(args)
            elif tool_name == "knowledge_delete":
                return self._handle_delete(args)
            return tool_error(f"Unknown tool: {tool_name}")
        except KeyError as exc:
            return tool_error(f"Missing argument: {exc}")
        except Exception as exc:
            logger.exception("knowledge-base tool error")
            return tool_error(str(exc))

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Mirror built-in memory writes into the knowledge base."""
        if action != "add" or not content or not self._db:
            return
        try:
            category = "general"
            item_id = self._db.insert(content, category=category, source="memory")
            self._embed_async(item_id, content)
        except Exception as exc:
            logger.debug("knowledge-base: on_memory_write failed: %s", exc)

    def shutdown(self) -> None:
        if self._db:
            self._db.close()
            self._db = None

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "DASHSCOPE_API_KEY",
                "description": "DashScope API key for multimodal embeddings",
                "secret": True,
                "required": False,
                "env_var": "DASHSCOPE_API_KEY",
                "url": "https://dashscope.aliyuncs.com",
            }
        ]

    # ------------------------------------------------------------------
    # Tool handlers
    # ------------------------------------------------------------------

    def _handle_store(self, args: dict) -> str:
        content = args["content"].strip()
        if not content:
            return tool_error("content cannot be empty")
        category = args.get("category", "general")
        if category not in _CATEGORIES:
            category = "general"
        tags = args.get("tags") or []
        media_url = args.get("media_url") or None
        # 'source' is not in the public schema but accepted from internal callers
        # (e.g. obsidian_tool passes source='obsidian').
        source = args.get("source", "manual")

        item_id = self._db.insert(
            content,
            category=category,
            source=source,
            media_url=media_url,
            tags=tags,
        )
        # Embed asynchronously so the tool call returns immediately.
        if media_url:
            self._embed_async(item_id, content, media_url=media_url)
        else:
            self._embed_async(item_id, content)

        return json.dumps({"id": item_id, "status": "stored", "category": category})

    def _handle_search(self, args: dict) -> str:
        query = args.get("query", "").strip()
        if not query:
            return tool_error("query cannot be empty")
        category = args.get("category") or None
        top_k = int(args.get("top_k", 5))

        results = self._do_search_raw(query, category=category, top_k=top_k)
        return json.dumps({"results": results, "count": len(results)})

    def _handle_list(self, args: dict) -> str:
        category = args.get("category") or None
        limit = int(args.get("limit", 20))
        items = self._db.list_items(category=category, limit=limit)
        # Strip raw embedding blob if present.
        clean = [{k: v for k, v in item.items() if k != "embedding"} for item in items]
        return json.dumps({"items": clean, "count": len(clean)})

    def _handle_delete(self, args: dict) -> str:
        item_id = args.get("id", "").strip()
        if not item_id:
            return tool_error("id is required")
        removed = self._db.delete(item_id)
        return json.dumps({"removed": removed})

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _do_search(self, query: str, *, top_k: int = 5, threshold: float = 0.40) -> str:
        results = self._do_search_raw(query, top_k=top_k, threshold=threshold)
        return _ret.format_results(results)

    def _do_search_raw(
        self,
        query: str,
        *,
        category: Optional[str] = None,
        top_k: int = 5,
        threshold: float = 0.40,
    ) -> List[Dict[str, Any]]:
        if not self._db:
            return []
        # Try semantic search first.
        query_blob = _emb.embed_text(query)
        if query_blob:
            candidates = self._db.fetch_all_with_embeddings(category=category)
            results = _ret.search(query_blob, candidates, top_k=top_k, threshold=threshold)
            if results:
                return results
        # Fall back to FTS5.
        return self._db.fts_search(query, category=category, limit=top_k)

    def _embed_async(self, item_id: str, content: str, *, media_url: Optional[str] = None) -> None:
        """Compute and store an embedding in a background thread."""
        db = self._db

        def _bg():
            try:
                if media_url:
                    blob = _emb.embed_image_with_caption(content, media_url)
                else:
                    blob = _emb.embed_text(content)
                if blob and db:
                    db.update_embedding(item_id, blob)
            except Exception as exc:
                logger.debug("knowledge-base: async embed failed for %s: %s", item_id, exc)

        threading.Thread(target=_bg, daemon=True).start()
