"""Built-in hashing embedder (section 8.2): lexical similarity with no model.

Lowercase the text, take ``\\w+`` tokens plus character bigrams for CJK runs, hash each token
with SHA-1 to pick a dimension and a sign, accumulate, apply square-root damping and
L2-normalise. Queries and documents are embedded the same way.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterator, Sequence
from typing import Any

from pydantic import Field

from ragcentre.providers.base import BuildEnv, ProviderConfig, ProviderTypeSpec, Role

# Bump whenever a change alters the vectors produced for some input (it is a fingerprint field).
IMPL_VERSION = 1

_WORD = re.compile(r"\w+")
_CJK = re.compile("([\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff\U00020000-\U0002ffff]+)")


def tokens(text: str) -> Iterator[str]:
    for word in _WORD.findall(text.lower()):
        if not _CJK.search(word):
            yield word
            continue
        for part in _CJK.split(word):
            if not part:
                continue
            if _CJK.fullmatch(part):
                if len(part) == 1:
                    yield part
                else:
                    for i in range(len(part) - 1):
                        yield part[i : i + 2]
            else:
                yield part


def hash_embed(text: str, dimensions: int) -> list[float]:
    acc = [0.0] * dimensions
    for token in tokens(text):
        digest = hashlib.sha1(token.encode("utf-8"), usedforsecurity=False).digest()
        index = int.from_bytes(digest[:4], "little") % dimensions
        acc[index] += 1.0 if digest[4] & 1 else -1.0
    damped = [math.copysign(math.sqrt(abs(x)), x) for x in acc]
    norm = math.sqrt(math.fsum(x * x for x in damped))
    if norm == 0.0:
        return damped
    return [x / norm for x in damped]


class BuiltinHashConfig(ProviderConfig):
    dimensions: int = Field(default=256, ge=8, le=8192, description="Vector dimensions")


class BuiltinHashEmbedder:
    batch_size = 256
    max_input_chars: int | None = None

    def __init__(self, dimensions: int) -> None:
        self.dimensions = dimensions

    def embed(self, texts: Sequence[str], role: Role) -> list[list[float]]:
        return [hash_embed(t, self.dimensions) for t in texts]


def _factory(config: Any, secrets: dict[str, str], env: BuildEnv) -> BuiltinHashEmbedder:
    assert isinstance(config, BuiltinHashConfig)
    return BuiltinHashEmbedder(config.dimensions)


SPEC = ProviderTypeSpec(
    kind="embedder",
    type="builtin_hash",
    label="Built-in hash embedder",
    description="Lexical hashing embedder for tests and demos; no model needed.",
    config_model=BuiltinHashConfig,
    fingerprint_fields=("impl_version", "dimensions"),
    secret_keys=(),
    factory=_factory,
    fixed_config={"impl_version": IMPL_VERSION},
)
