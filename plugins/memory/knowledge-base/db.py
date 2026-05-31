"""SQLite persistence layer for the knowledge-base memory provider.

Uses WAL mode with jittered-retry writes, matching the pattern in
hermes_state.py.  All embeddings are stored as packed Float32 blobs.
"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
import struct
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
_WRITE_MAX_RETRIES = 8
_WRITE_RETRY_MIN_S = 0.020
_WRITE_RETRY_MAX_S = 0.150
_CHECKPOINT_EVERY_N_WRITES = 50

# Categories that users can assign to knowledge items.
CATEGORIES = ("work", "life", "idea", "meeting", "learning", "general")

_WAL_INCOMPAT_MARKERS = ("locking protocol", "not authorized")


class KnowledgeDB:
    """Thread-safe SQLite store for knowledge items."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        self._write_count = 0
        self._conn = self._open()

    # ------------------------------------------------------------------
    # Connection / schema
    # ------------------------------------------------------------------

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        self._apply_wal(conn)
        conn.execute("PRAGMA foreign_keys = ON")
        self._migrate(conn)
        return conn

    def _apply_wal(self, conn: sqlite3.Connection) -> None:
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()
            if mode and mode[0] == "wal":
                return
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if any(m in msg for m in _WAL_INCOMPAT_MARKERS):
                conn.execute("PRAGMA journal_mode=DELETE")
                logger.warning("knowledge-base: WAL unsupported on this filesystem, using DELETE mode")
            else:
                raise

    def _migrate(self, conn: sqlite3.Connection) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_items (
                id          TEXT PRIMARY KEY,
                content     TEXT NOT NULL,
                category    TEXT NOT NULL DEFAULT 'general',
                source      TEXT NOT NULL DEFAULT 'manual',
                embedding   BLOB,
                media_url   TEXT,
                tags        TEXT,
                created_at  INTEGER NOT NULL,
                updated_at  INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts
            USING fts5(content, content=knowledge_items, content_rowid=rowid)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_kb_category
            ON knowledge_items(category)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_kb_source
            ON knowledge_items(source)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_kb_created
            ON knowledge_items(created_at DESC)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kb_meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.commit()

    # ------------------------------------------------------------------
    # Write helper
    # ------------------------------------------------------------------

    def _execute_write(self, fn) -> Any:
        last_err: Optional[Exception] = None
        for attempt in range(_WRITE_MAX_RETRIES):
            try:
                with self._lock:
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        result = fn(self._conn)
                        self._conn.commit()
                    except BaseException:
                        try:
                            self._conn.rollback()
                        except Exception:
                            pass
                        raise
                self._write_count += 1
                if self._write_count % _CHECKPOINT_EVERY_N_WRITES == 0:
                    self._try_checkpoint()
                return result
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    last_err = exc
                    if attempt < _WRITE_MAX_RETRIES - 1:
                        time.sleep(random.uniform(_WRITE_RETRY_MIN_S, _WRITE_RETRY_MAX_S))
                        continue
                raise
        raise last_err or sqlite3.OperationalError("database is locked after max retries")

    def _try_checkpoint(self) -> None:
        try:
            self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def insert(
        self,
        content: str,
        *,
        category: str = "general",
        source: str = "manual",
        embedding: Optional[bytes] = None,
        media_url: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> str:
        item_id = str(uuid.uuid4())
        now = int(time.time())
        tags_json = json.dumps(tags or [])

        def _do(conn: sqlite3.Connection) -> str:
            conn.execute(
                """
                INSERT INTO knowledge_items
                    (id, content, category, source, embedding, media_url, tags, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (item_id, content, category, source, embedding, media_url, tags_json, now, now),
            )
            conn.execute(
                "INSERT INTO knowledge_fts(rowid, content) "
                "SELECT rowid, content FROM knowledge_items WHERE id = ?",
                (item_id,),
            )
            return item_id

        return self._execute_write(_do)

    def update_embedding(self, item_id: str, embedding: bytes) -> None:
        now = int(time.time())

        def _do(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE knowledge_items SET embedding = ?, updated_at = ? WHERE id = ?",
                (embedding, now, item_id),
            )

        self._execute_write(_do)

    def delete(self, item_id: str) -> bool:
        def _do(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                "SELECT rowid FROM knowledge_items WHERE id = ?", (item_id,)
            ).fetchone()
            if not row:
                return False
            conn.execute(
                "DELETE FROM knowledge_fts WHERE rowid = ?", (row[0],)
            )
            conn.execute("DELETE FROM knowledge_items WHERE id = ?", (item_id,))
            return True

        return self._execute_write(_do)

    def fetch_all_with_embeddings(
        self, category: Optional[str] = None, source: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Return all items that have embeddings (for cosine search)."""
        where_parts = ["embedding IS NOT NULL"]
        params: list = []
        if category:
            where_parts.append("category = ?")
            params.append(category)
        if source:
            where_parts.append("source = ?")
            params.append(source)
        where = " AND ".join(where_parts)
        rows = self._conn.execute(
            f"SELECT id, content, category, source, embedding, media_url, tags, created_at "
            f"FROM knowledge_items WHERE {where}",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def fts_search(
        self,
        query: str,
        *,
        category: Optional[str] = None,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """FTS5 fallback for items without embeddings."""
        fts_q = " OR ".join(f'"{w}"' for w in query.split() if w)
        if not fts_q:
            return []
        where_extra = ""
        params: list = [fts_q]
        if category:
            where_extra = " AND ki.category = ?"
            params.append(category)
        params.append(limit)
        rows = self._conn.execute(
            f"""
            SELECT ki.id, ki.content, ki.category, ki.source,
                   ki.media_url, ki.tags, ki.created_at
            FROM knowledge_fts kf
            JOIN knowledge_items ki ON ki.rowid = kf.rowid
            WHERE knowledge_fts MATCH ?{where_extra}
            ORDER BY rank
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def list_items(
        self,
        *,
        category: Optional[str] = None,
        source: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        where_parts: list[str] = []
        params: list = []
        if category:
            where_parts.append("category = ?")
            params.append(category)
        if source:
            where_parts.append("source = ?")
            params.append(source)
        where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT id, content, category, source, media_url, tags, created_at "
            f"FROM knowledge_items {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM knowledge_items").fetchone()[0]

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
