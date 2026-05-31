"""Cosine similarity retrieval over packed float32 embedding blobs.

Pure Python + struct — no numpy required.  Mirrors the queryVectors()
logic from we-assistant (cosine dot-product, top-K ranking).
"""

from __future__ import annotations

import math
import struct
from typing import Any, Dict, List, Optional


def _cosine(a: bytes, b: bytes) -> float:
    """Cosine similarity between two packed float32 blobs."""
    n = len(a) // 4
    va = struct.unpack(f"{n}f", a)
    vb = struct.unpack(f"{n}f", b)
    dot = sum(x * y for x, y in zip(va, vb))
    norm_a = math.sqrt(sum(x * x for x in va))
    norm_b = math.sqrt(sum(x * x for x in vb))
    denom = norm_a * norm_b
    if denom < 1e-10:
        return 0.0
    return dot / denom


def search(
    query_embedding: bytes,
    candidates: List[Dict[str, Any]],
    *,
    top_k: int = 5,
    threshold: float = 0.40,
) -> List[Dict[str, Any]]:
    """Return top-K candidates sorted by cosine similarity.

    Each candidate dict must have an ``"embedding"`` key (bytes blob).
    Results include a ``"score"`` key (0.0–1.0) and exclude the raw blob.
    """
    scored: list[tuple[float, dict]] = []
    for item in candidates:
        blob = item.get("embedding")
        if not blob or not isinstance(blob, bytes):
            continue
        score = _cosine(query_embedding, blob)
        if score >= threshold:
            result = {k: v for k, v in item.items() if k != "embedding"}
            result["score"] = round(score, 4)
            scored.append((score, result))

    scored.sort(key=lambda t: t[0], reverse=True)
    return [r for _, r in scored[:top_k]]


def format_results(results: List[Dict[str, Any]], header: str = "## Knowledge Base") -> str:
    """Format retrieved results as a context block for the system prompt."""
    if not results:
        return ""
    lines = [header]
    for r in results:
        score_pct = int(r.get("score", 0) * 100)
        cat = r.get("category", "")
        content = r.get("content", "")
        prefix = f"[{cat}, {score_pct}%]" if cat else f"[{score_pct}%]"
        lines.append(f"- {prefix} {content}")
        if r.get("media_url"):
            lines.append(f"  media: {r['media_url']}")
    return "\n".join(lines)
