"""Canonical JSON, hashes, deterministic ids and fingerprints (IMPLEMENTATION_PLAN.md section 6).

Fingerprints are the mechanism behind work sharing, caching and the mismatch guard, so every
fingerprint in the system is computed by a function in this module.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

# Fixed namespace for deterministic chunk ids. Never change it: chunk ids are record ids in
# every vector store.
NAMESPACE_RAGCENTRE = uuid.UUID("6f1f5a4e-2c7b-5d0e-9a43-52a7c3e0b1d8")


class CanonicalJsonError(TypeError):
    """The value contains a type that canonical JSON does not allow."""


def _check(value: Any, path: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalJsonError(f"{path or '<root>'}: non-finite float {value!r} is not allowed")
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise CanonicalJsonError(f"{path or '<root>'}: dict key {k!r} is not a string")
            _check(v, f"{path}.{k}" if path else k)
        return
    if isinstance(value, list):
        for i, v in enumerate(value):
            _check(v, f"{path}[{i}]")
        return
    raise CanonicalJsonError(f"{path or '<root>'}: type {type(value).__name__} is not allowed in canonical JSON")


def canonical(obj: Any) -> bytes:
    """Sorted-key, compact, UTF-8 JSON. Only dict, list, str, int, float, bool and None."""
    _check(obj, "")
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fp(obj: Any) -> str:
    """Fingerprint: sha256 of the canonical JSON."""
    return sha256_bytes(canonical(obj))


def new_id() -> str:
    return str(uuid.uuid4())


def chunk_id(chunk_node_fp: str, document_id: str, ordinal: int) -> str:
    return str(uuid.uuid5(NAMESPACE_RAGCENTRE, f"{chunk_node_fp}:{document_id}:{ordinal}"))


def provider_fp(kind: str, type_: str, fields: Mapping[str, Any]) -> str:
    return fp({"kind": kind, "type": type_, "fields": dict(fields)})


def node_fp(type_: str, impl_version: int, config: Mapping[str, Any], inputs: Sequence[str]) -> str:
    return fp({"type": type_, "impl_version": impl_version, "config": dict(config), "inputs": list(inputs)})


def pipeline_fp(node_fps: Sequence[str], retrieval: Mapping[str, Any]) -> str:
    return fp({"nodes": sorted(node_fps), "retrieval": dict(retrieval)})


def parse_cache_key(parser_fp: str, options: Mapping[str, Any], content_sha256: str) -> str:
    return fp({"parser": parser_fp, "options": dict(options), "content": content_sha256})


def collection_name(corpus_id: str, index_node_fp: str) -> str:
    """``rc_{first 8 hex of corpus id}_{first 16 hex of index node_fp}`` (section 10.1)."""
    corpus_hex = corpus_id.replace("-", "")[:8].lower()
    return f"rc_{corpus_hex}_{index_node_fp[:16].lower()}"
