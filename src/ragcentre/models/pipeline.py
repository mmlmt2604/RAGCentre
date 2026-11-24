"""Pipeline configuration models (section 5), matching ``PipelineConfig`` in section 22.

These models check structure only (shapes and types). The twelve validation rules of section 5
are applied by ``ragcentre.pipeline.graph`` so every problem can name the node it concerns.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

NodeType = Literal["parse", "chunk", "embed", "index_vector", "index_fts"]
NODE_TYPES: tuple[str, ...] = ("parse", "chunk", "embed", "index_vector", "index_fts")
INDEX_TYPES = frozenset({"index_vector", "index_fts"})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Route(_Strict):
    mime: str = Field(min_length=1)
    provider: str
    options: dict[str, Any] = Field(default_factory=dict)


class ParseNode(_Strict):
    id: str
    type: Literal["parse"]
    routes: list[Route]
    inputs: list[str] = Field(default_factory=list)


class ChunkNode(_Strict):
    id: str
    type: Literal["chunk"]
    inputs: list[str] = Field(default_factory=list)
    strategy: str
    options: dict[str, Any] = Field(default_factory=dict)
    provider: str | None = None


class EmbedNode(_Strict):
    id: str
    type: Literal["embed"]
    inputs: list[str] = Field(default_factory=list)
    provider: str


class IndexVectorOptions(_Strict):
    metric: Literal["cosine", "l2", "dot"] = "cosine"


class IndexVectorNode(_Strict):
    id: str
    type: Literal["index_vector"]
    inputs: list[str] = Field(default_factory=list)
    provider: str
    options: IndexVectorOptions = Field(default_factory=IndexVectorOptions)


class IndexFtsOptions(_Strict):
    tokenizer: Literal["unicode61", "trigram"] = "unicode61"


class IndexFtsNode(_Strict):
    id: str
    type: Literal["index_fts"]
    inputs: list[str] = Field(default_factory=list)
    options: IndexFtsOptions = Field(default_factory=IndexFtsOptions)


Node = Annotated[ParseNode | ChunkNode | EmbedNode | IndexVectorNode | IndexFtsNode, Field(discriminator="type")]


class Source(_Strict):
    id: str
    index: str
    top_k: int


class Fusion(_Strict):
    method: Literal["rrf", "weighted"] = "rrf"
    k: int = 60
    weights: dict[str, float] | None = None


class Rerank(_Strict):
    provider: str
    top_n: int


class Expand(_Strict):
    neighbors: int = 0


class Transform(_Strict):
    mode: Literal["rewrite", "multi_query", "hyde", "decompose"]
    provider: str
    n: int = 3
    include_original: bool = True
    original_weight: float = 1.0
    k: int = 60
    instructions: str = ""


class RetrievalConfig(_Strict):
    transform: Transform | None = None
    sources: list[Source]
    fusion: Fusion | None = None
    rerank: Rerank | None = None
    expand: Expand | None = None
    top_k: int


class PipelineConfig(_Strict):
    name: str
    nodes: list[Node]
    retrieval: RetrievalConfig
