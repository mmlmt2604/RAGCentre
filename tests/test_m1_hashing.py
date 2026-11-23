from __future__ import annotations

import uuid

import pytest

from ragcentre import hashing, vectors
from ragcentre.hashing import CanonicalJsonError, canonical, chunk_id, collection_name, fp


def test_canonical_is_stable_under_key_reordering() -> None:
    a = {"b": 1, "a": {"y": [1, 2, {"q": None, "p": True}], "x": "é"}}
    b = {"a": {"x": "é", "y": [1, 2, {"p": True, "q": None}]}, "b": 1}
    assert canonical(a) == canonical(b)
    assert fp(a) == fp(b)
    assert canonical(a) == '{"a":{"x":"é","y":[1,2,{"p":true,"q":null}]},"b":1}'.encode()


def test_list_order_matters() -> None:
    assert fp([1, 2]) != fp([2, 1])


@pytest.mark.parametrize(
    "value",
    [(1, 2), {1, 2}, b"x", {1: "int key"}, float("nan"), float("inf"), object(), {"a": (1,)}, [uuid.uuid4()]],
)
def test_canonical_rejects_unsupported_types(value: object) -> None:
    with pytest.raises(CanonicalJsonError):
        canonical(value)


def test_fp_is_sha256_hex() -> None:
    assert len(fp({})) == 64
    assert fp({}) == hashing.sha256_bytes(b"{}")


def test_chunk_ids_are_deterministic_uuids() -> None:
    a = chunk_id("f" * 64, "doc", 3)
    assert a == chunk_id("f" * 64, "doc", 3)
    assert a != chunk_id("f" * 64, "doc", 4)
    assert uuid.UUID(a).version == 5


def test_provider_fp_ignores_nothing_it_is_given() -> None:
    a = hashing.provider_fp("embedder", "builtin_hash", {"impl_version": 1, "dimensions": 256})
    b = hashing.provider_fp("embedder", "builtin_hash", {"dimensions": 256, "impl_version": 1})
    c = hashing.provider_fp("embedder", "builtin_hash", {"impl_version": 1, "dimensions": 128})
    assert a == b != c


def test_node_fp_depends_on_inputs_order() -> None:
    a = hashing.node_fp("chunk", 1, {"x": 1}, ["a", "b"])
    b = hashing.node_fp("chunk", 1, {"x": 1}, ["b", "a"])
    assert a != b


def test_pipeline_fp_sorts_nodes() -> None:
    assert hashing.pipeline_fp(["b", "a"], {"top_k": 1}) == hashing.pipeline_fp(["a", "b"], {"top_k": 1})


def test_collection_name_rule() -> None:
    name = collection_name("1234abcd-0000-0000-0000-000000000000", "0123456789abcdef" * 4)
    assert name.startswith("rc_") and len(name) == 27
    assert name != collection_name("1234abcd-0000-0000-0000-000000000001", "0123456789abcdef" * 4)


def test_vectors_pack_roundtrip() -> None:
    v = [0.1, -2.5, 3.0]
    packed = vectors.pack(v)
    assert len(packed) == 12
    assert vectors.unpack(packed) == pytest.approx(v, rel=1e-6)
    assert packed[:4] == bytes.fromhex("cdcccc3d")  # little-endian float32 0.1


def test_vector_math() -> None:
    assert vectors.l2_normalise([3.0, 4.0]) == pytest.approx([0.6, 0.8])
    assert vectors.l2_normalise([0.0, 0.0]) == [0.0, 0.0]
    assert vectors.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert vectors.l2_distance([0.0, 0.0], [3.0, 4.0]) == 5.0
    assert vectors.dot([1.0, 2.0], [3.0, 4.0]) == 11.0
