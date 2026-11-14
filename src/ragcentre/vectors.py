"""Pure-Python vector helpers. Vectors are ``list[float]``, stored as little-endian float32."""

from __future__ import annotations

import math
import sys
from array import array
from collections.abc import Sequence

_BIG_ENDIAN = sys.byteorder == "big"


def pack(vector: Sequence[float]) -> bytes:
    arr = array("f", vector)
    if _BIG_ENDIAN:  # pragma: no cover - little-endian hosts
        arr.byteswap()
    return arr.tobytes()


def unpack(data: bytes) -> list[float]:
    arr = array("f")
    arr.frombytes(data)
    if _BIG_ENDIAN:  # pragma: no cover
        arr.byteswap()
    return arr.tolist()


def float32(vector: Sequence[float]) -> list[float]:
    """Round-trip through float32, so in-memory vectors equal what was stored."""
    return unpack(pack(vector))


def norm(vector: Sequence[float]) -> float:
    return math.sqrt(math.fsum(x * x for x in vector))


def l2_normalise(vector: Sequence[float]) -> list[float]:
    n = norm(vector)
    if n == 0.0:
        return [0.0 for _ in vector]
    return [x / n for x in vector]


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    return math.fsum(x * y for x, y in zip(a, b, strict=True))


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    na, nb = norm(a), norm(b)
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot(a, b) / (na * nb)


def l2_distance(a: Sequence[float], b: Sequence[float]) -> float:
    return math.sqrt(math.fsum((x - y) ** 2 for x, y in zip(a, b, strict=True)))
