"""DashScope multimodal embedding API client.

Model: tongyi-embedding-vision-plus-2026-03-06
Dimensions: 1024
Supports: text and image (URL or base64)

The embedding vector is returned as a packed bytes object (1024 float32 values)
suitable for direct storage in SQLite BLOB columns.
"""

from __future__ import annotations

import base64
import logging
import os
import struct
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DASHSCOPE_EMBED_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/embeddings/"
    "multimodal-embedding/multimodal-embedding"
)
_MODEL = "tongyi-embedding-vision-plus-2026-03-06"
_DIMENSIONS = 1024


def _get_api_key() -> str:
    key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "DASHSCOPE_API_KEY is not set. "
            "Add it to ~/.hermes/.env to enable knowledge-base embeddings."
        )
    return key


def embed_text(text: str) -> Optional[bytes]:
    """Embed a text string. Returns 1024-dim float32 blob, or None on error."""
    return _call_api([{"text": text[:4096]}])


def embed_image(image_source: str) -> Optional[bytes]:
    """Embed an image from a URL or local file path.

    For local files, the image is base64-encoded automatically.
    Returns 1024-dim float32 blob, or None on error.
    """
    if image_source.startswith(("http://", "https://")):
        payload_input = [{"image": image_source}]
    else:
        path = Path(image_source)
        if not path.exists():
            logger.warning("knowledge-base: image file not found: %s", image_source)
            return None
        suffix = path.suffix.lower().lstrip(".")
        mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}.get(suffix, "jpeg")
        b64 = base64.b64encode(path.read_bytes()).decode()
        payload_input = [{"image": f"data:image/{mime};base64,{b64}"}]
    return _call_api(payload_input)


def embed_image_with_caption(caption: str, image_source: str) -> Optional[bytes]:
    """Embed image + caption together (dual-modal input)."""
    if image_source.startswith(("http://", "https://")):
        img_part: dict = {"image": image_source}
    else:
        path = Path(image_source)
        if not path.exists():
            return embed_text(caption)
        suffix = path.suffix.lower().lstrip(".")
        mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}.get(suffix, "jpeg")
        b64 = base64.b64encode(path.read_bytes()).decode()
        img_part = {"image": f"data:image/{mime};base64,{b64}"}
    return _call_api([img_part, {"text": caption[:2048]}])


def pack_vector(floats: list[float]) -> bytes:
    """Pack a list of floats into a compact bytes object."""
    return struct.pack(f"{len(floats)}f", *floats)


def unpack_vector(blob: bytes) -> list[float]:
    """Unpack a bytes blob back into a list of floats."""
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _call_api(inputs: list) -> Optional[bytes]:
    try:
        import httpx
    except ImportError:
        logger.error("knowledge-base: httpx is required for embeddings")
        return None
    try:
        api_key = _get_api_key()
    except RuntimeError as exc:
        logger.warning("knowledge-base: %s", exc)
        return None

    payload = {
        "model": _MODEL,
        "input": {"contents": inputs},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        resp = httpx.post(_DASHSCOPE_EMBED_URL, json=payload, headers=headers, timeout=30.0)
        resp.raise_for_status()
        data = resp.json()
        vector = (
            data.get("output", {})
            .get("embeddings", [{}])[0]
            .get("embedding", [])
        )
        if not vector or len(vector) != _DIMENSIONS:
            logger.warning(
                "knowledge-base: unexpected embedding dimension %d (expected %d)",
                len(vector), _DIMENSIONS,
            )
            return None
        return pack_vector(vector)
    except Exception as exc:
        logger.warning("knowledge-base: embedding API error: %s", exc)
        return None
