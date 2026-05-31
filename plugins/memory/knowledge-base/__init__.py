"""Knowledge-base memory provider plugin.

Activated via ``memory.provider: knowledge-base`` in ~/.hermes/config.yaml.
Requires DASHSCOPE_API_KEY in ~/.hermes/.env for semantic embeddings;
falls back to FTS5 text search without it.
"""

from .provider import KnowledgeBaseProvider


def register(ctx) -> None:
    """Register the knowledge-base memory provider with the plugin system."""
    ctx.register_memory_provider(KnowledgeBaseProvider())
