"""``http`` strategy: chunks come from a chunker provider (section 8.4). The core fills in the
ordinal, pages and heading path from the returned block ids."""

from __future__ import annotations

from typing import Any, ClassVar

from ragcentre.chunking.base import ChunkDraft, HttpOptions
from ragcentre.models.ir import DocumentIR
from ragcentre.providers.base import invalid_response

# Bump whenever a change alters how service output is turned into chunks.
IMPL_VERSION = 1


class HttpStrategy:
    name: ClassVar[str] = "http"
    impl_version: ClassVar[int] = IMPL_VERSION
    options_model: ClassVar[type[HttpOptions]] = HttpOptions
    needs_provider: ClassVar[bool] = True

    def chunk(self, doc: DocumentIR, options: Any, provider: Any = None) -> list[ChunkDraft]:
        if provider is None:
            raise ValueError("the http chunk strategy needs a chunker provider")
        name = getattr(provider, "name", "chunker")
        payload = options.model_dump(mode="json") if isinstance(options, HttpOptions) else dict(options or {})
        raw = provider.chunk(doc.without_page_images(), payload)
        known = {str(b.id) for b in doc.blocks}
        drafts: list[ChunkDraft] = []
        for i, item in enumerate(raw):
            text = item.get("text")
            context = item.get("context")
            block_ids = item.get("block_ids", [])
            metadata = item.get("metadata") or {}
            if not isinstance(text, str):
                raise invalid_response(name, f"chunk {i}: 'text' must be a string")
            if context is not None and not isinstance(context, str):
                raise invalid_response(name, f"chunk {i}: 'context' must be a string or null")
            if not isinstance(block_ids, list) or not all(isinstance(b, str) for b in block_ids):
                raise invalid_response(name, f"chunk {i}: 'block_ids' must be a list of strings")
            unknown = [b for b in block_ids if b not in known]
            if unknown:
                raise invalid_response(name, f"chunk {i}: unknown block ids {unknown[:5]}")
            if not isinstance(metadata, dict):
                raise invalid_response(name, f"chunk {i}: 'metadata' must be an object")
            drafts.append(ChunkDraft(text=text, block_ids=block_ids, context=context, metadata=metadata))
        return drafts
