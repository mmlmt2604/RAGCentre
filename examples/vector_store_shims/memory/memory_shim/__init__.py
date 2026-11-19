"""A reference ``ragcentre.vector_store/v1`` service over an in-memory brute-force index.

``uvicorn memory_shim:app`` builds and runs the app; ``create_app`` builds it in-process (the test
suite uses it with an ASGI transport).
"""

from __future__ import annotations

from typing import Any

from memory_shim.service import PROTOCOL, ApiError, MemoryShim, create_app, error

__all__ = ["PROTOCOL", "ApiError", "MemoryShim", "create_app", "error"]


def __getattr__(name: str) -> Any:
    # ``uvicorn memory_shim:app`` builds the real app lazily on first use.
    if name == "app":
        return create_app()
    raise AttributeError(name)
