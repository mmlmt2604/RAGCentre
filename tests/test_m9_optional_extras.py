"""The optional store extras: importing ragcentre never needs them, and using a store whose extra
is missing fails with an error naming the extra (RAGCentre never installs anything itself)."""

from __future__ import annotations

import subprocess
import sys

SCRIPT = r"""
import importlib, importlib.abc, pkgutil, sys

BLOCKED = ("qdrant_client", "chromadb", "psycopg", "pgvector", "lancedb", "redis", "weaviate", "elasticsearch", "opensearchpy", "pymilvus", "pinecone", "pymongo")

class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError(f"No module named {name!r} (blocked for this test)")
        return None

sys.meta_path.insert(0, Block())

import ragcentre
for mod in pkgutil.walk_packages(ragcentre.__path__, "ragcentre."):
    importlib.import_module(mod.name)

from ragcentre.providers.registry import get_type
from ragcentre.stores.base import StoreUnavailable
from ragcentre.stores.registry import vector_store_types

types = vector_store_types()
assert {"sqlite_vec", "qdrant", "pgvector", "chroma"} <= set(types), sorted(types)
for name, config, secrets in [("qdrant", {"path": ":memory:"}, {}), ("pgvector", {}, {"dsn": "postgresql://x"}),
                              ("chroma", {}, {}), ("lancedb", {"uri": "lance-test"}, {}),
                              ("redis", {"url": "redis://x:6379/0"}, {}), ("weaviate", {"url": "http://x:8080"}, {}),
                              ("elasticsearch", {"url": "http://x:9200"}, {}), ("opensearch", {"url": "http://x:9200"}, {}),
                              ("milvus", {"uri": "http://x:19530"}, {}),
                              ("pinecone", {"index_name": "x"}, {"api_key": "k"}),
                              ("mongodb", {}, {"uri": "mongodb://x:27017"})]:
    assert get_type("vector_store", name) is not None
    t = types[name]
    try:
        t.factory(t.config_model.model_validate(config), secrets, __import__("pathlib").Path("."))
    except StoreUnavailable as exc:
        print(f"{name}: {exc}")
    else:
        raise AssertionError(f"{name} did not fail")
"""


def test_stores_without_extras() -> None:
    out = subprocess.run([sys.executable, "-c", SCRIPT], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    lines = dict(line.split(": ", 1) for line in out.stdout.strip().splitlines())
    assert set(lines) == {
        "qdrant",
        "pgvector",
        "chroma",
        "lancedb",
        "redis",
        "weaviate",
        "elasticsearch",
        "opensearch",
        "milvus",
        "pinecone",
        "mongodb",
    }
    for extra, message in lines.items():
        assert f"'{extra}' extra" in message and f"ragcentre[{extra}]" in message, message
        assert "pip" not in message and "install " not in message.replace("installed", "")
