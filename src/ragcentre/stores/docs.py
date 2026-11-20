"""The README "Vector stores" table, generated from the store registry.

``ragcentre store list --markdown`` prints it; ``tests/test_v7_store_docs.py`` fails when the README
differs from it. Capabilities come from each type's registry entry, so they are the type's best case;
an instance can report less (``http`` reads its capabilities from ``/info``, Pinecone's depend on the
index, Chroma Cloud accepts 300 records per write). The per-type extra, config example and example
file below are maintained by hand, and the tests check that every built-in type has an entry, that
each config example validates against the type's ``config_model``, and that each extra exists.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ragcentre.stores.registry import BUILTIN, VectorStoreType, vector_store_types

START = "<!-- vector-stores:start -->"
END = "<!-- vector-stores:end -->"


@dataclass(frozen=True)
class StoreDoc:
    extra: str | None  # ``pip install "ragcentre[<extra>]"``; None when the store needs nothing extra
    config: dict[str, Any]  # a one-line example for the table; must validate against the config model
    example: str  # file name under examples/providers/
    doc: str | None = None  # a further link (relative to the repository root), when there is one


# Keep these in step with examples/providers/*.yaml; the tests validate both.
STORE_DOCS: dict[str, StoreDoc] = {
    "sqlite_vec": StoreDoc(None, {"prefix": "staging_"}, "sqlite-vec-prefixed.yaml"),
    "qdrant": StoreDoc("qdrant", {"url": "https://HOST:6333", "quantization": "scalar"}, "qdrant-cloud.yaml"),
    "pgvector": StoreDoc("pgvector", {"vector_type": "halfvec", "hnsw_m": 16}, "pgvector-halfvec.yaml"),
    "chroma": StoreDoc("chroma", {"mode": "http", "host": "localhost", "port": 8000}, "chroma-http.yaml"),
    "lancedb": StoreDoc("lancedb", {"uri": "lancedb", "index_type": "IVF_FLAT"}, "lancedb-local.yaml"),
    "redis": StoreDoc("redis", {"url": "redis://localhost:6379/0", "algorithm": "hnsw"}, "redis-hnsw.yaml"),
    "weaviate": StoreDoc("weaviate", {"url": "http://localhost:8080"}, "weaviate.yaml"),
    "elasticsearch": StoreDoc(
        "elasticsearch", {"url": "https://localhost:9200", "index_options": "hnsw"}, "elasticsearch.yaml"
    ),
    "opensearch": StoreDoc("opensearch", {"url": "https://localhost:9200", "engine": "faiss"}, "opensearch.yaml"),
    "milvus": StoreDoc("milvus", {"uri": "milvus.db", "index_type": "FLAT"}, "milvus-lite.yaml"),
    "pinecone": StoreDoc("pinecone", {"index_name": "ragcentre"}, "pinecone.yaml"),
    "mongodb": StoreDoc("mongodb", {"database": "ragcentre", "exact": False}, "mongodb-atlas.yaml"),
    "http": StoreDoc(
        None, {"url": "http://127.0.0.1:8702"}, "http-vector-store.yaml", doc="docs/protocols/vector-store-v1.md"
    ),
}

FOOTNOTE = (
    "The guarantees are each type's best case. An instance can report less: `http` reads them from the "
    "service's `/info`, Pinecone's depend on the index, Chroma Cloud takes 300 records per write, "
    "LanceDB Cloud (`db://`) is eventually consistent without exact filters, and MongoDB with `exact: true` "
    "filters exactly. "
    "`ragcentre store check` shows what a live store gives. "
    "Filters are exact when a filtered search returns `k` matches whenever `k` exist; "
    "“eventual” consistency means a write may not be searchable at once."
)

HEADERS = (
    "Type",
    "Install",
    "Metrics",
    "Exact filters",
    "Consistency",
    "Max dim",
    "Max batch",
    "Config example",
    "Details",
)


def doc_types() -> list[VectorStoreType]:
    """The built-in store types, in registry order."""
    types = vector_store_types()
    return [types[name] for name in BUILTIN if name in types]


def config_line(doc: StoreDoc) -> str:
    return json.dumps(doc.config)


def _rows() -> list[list[str]]:
    rows = []
    for t in doc_types():
        d = STORE_DOCS[t.type]
        caps = t.capabilities
        links = [f"[example](examples/providers/{d.example})"]
        if d.doc:
            links.append(f"[protocol]({d.doc})")
        rows.append(
            [
                f"`{t.type}`",
                f"`ragcentre[{d.extra}]`" if d.extra else "built in",
                ", ".join(sorted(caps.metrics)),
                "yes" if caps.filter_exact else "**no**",
                caps.consistency if caps.consistency == "immediate" else f"**{caps.consistency}**",
                f"{caps.max_dim:,}" if caps.max_dim is not None else "none known",
                f"{caps.max_batch:,}",
                f"`{config_line(d)}`",
                ", ".join(links),
            ]
        )
    return rows


def markdown_table() -> str:
    """The README table and its footnote, without the marker comments."""
    rows = _rows()
    lines = [
        "| " + " | ".join(HEADERS) + " |",
        "|" + "|".join("---" for _ in HEADERS) + "|",
        *("| " + " | ".join(r) + " |" for r in rows),
        "",
        FOOTNOTE,
    ]
    return "\n".join(lines)


def readme_block() -> str:
    return f"{START}\n{markdown_table()}\n{END}"


def plain_rows() -> tuple[list[str], list[list[str]]]:
    """Rows for ``ragcentre store list``: every registered type, including third-party ones."""
    rows = []
    types = vector_store_types()
    names = [n for n in BUILTIN if n in types] + sorted(n for n in types if n not in BUILTIN)
    for name in names:
        t = types[name]
        caps = t.capabilities
        d = STORE_DOCS.get(name)
        rows.append(
            [
                name,
                f"ragcentre[{d.extra}]" if d and d.extra else ("built in" if d else "plugin"),
                ",".join(sorted(caps.metrics)),
                "yes" if caps.filter_exact else "no",
                caps.consistency,
                f"{caps.max_dim}" if caps.max_dim is not None else "-",
                str(caps.max_batch),
                ",".join(t.secret_keys) or "-",
            ]
        )
    return ["TYPE", "INSTALL", "METRICS", "EXACT FILTERS", "CONSISTENCY", "MAX DIM", "MAX BATCH", "SECRETS"], rows
