"""The parts of RAGCentre's API responses this server reads, and the tools' structured outputs.

API models ignore fields they don't name, so the server keeps working when RAGCentre adds fields.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Scalar = str | int | float | bool


class _Api(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ---- API responses ------------------------------------------------------------------------------


class Health(_Api):
    version: str
    auth_required: bool = False


class WhoAmI(_Api):
    scope: Literal["full", "read", "none"]


class FieldSpec(_Api):
    type: str


class MetadataSchema(_Api):
    fields: dict[str, FieldSpec] = Field(default_factory=dict)


class Corpus(_Api):
    id: str
    name: str
    metadata_schema: MetadataSchema = Field(default_factory=MetadataSchema)
    active_pipeline_id: str | None = None
    document_count: int = 0


class ResultDocument(_Api):
    filename: str
    external_id: str | None = None
    metadata: dict[str, Scalar] = Field(default_factory=dict)


class QueryResult(_Api):
    chunk_id: str
    document_id: str
    score: float
    rank: int
    ordinal: int | None = None
    text: str
    page_start: int | None = None
    page_end: int | None = None
    heading_path: list[str] = Field(default_factory=list)
    document: ResultDocument


class QueryProbe(_Api):
    index: int
    text: str
    kind: str


class QueryTransform(_Api):
    mode: str
    probes: list[QueryProbe] = Field(default_factory=list)


class QueryResponse(_Api):
    results: list[QueryResult]
    truncated: bool = False
    transform: QueryTransform | None = None


class ApiChunk(_Api):
    id: str
    document_id: str
    ordinal: int
    text: str
    page_start: int | None = None
    page_end: int | None = None
    heading_path: list[str] = Field(default_factory=list)


class WindowDocument(ResultDocument):
    id: str
    corpus_id: str


class ChunkWindow(_Api):
    chunk: ApiChunk
    before: list[ApiChunk] = Field(default_factory=list)
    after: list[ApiChunk] = Field(default_factory=list)
    document: WindowDocument


class OutlineSection(_Api):
    heading_path: list[str] = Field(default_factory=list)
    first_chunk_id: str
    page_start: int | None = None
    page_end: int | None = None
    chunk_count: int


class OutlineDocument(_Api):
    id: str
    corpus_id: str
    filename: str
    external_id: str | None = None
    metadata: dict[str, Scalar] = Field(default_factory=dict)


class Outline(_Api):
    document: OutlineDocument
    chunk_count: int
    page_count: int | None = None
    sections: list[OutlineSection]
    truncated: bool = False


# ---- Tool outputs (published as each tool's outputSchema) ---------------------------------------


class CorpusInfo(BaseModel):
    name: str
    id: str
    document_count: int
    searchable: bool = Field(description="False when the corpus has no active pipeline yet.")
    filter_fields: dict[str, str] = Field(description="Metadata fields usable in a search filter, with their types.")


class ListCorporaOutput(BaseModel):
    corpora: list[CorpusInfo]


class SearchHit(BaseModel):
    index: int
    chunk_id: str
    document_id: str
    filename: str
    page_start: int | None
    page_end: int | None
    heading_path: list[str]
    score: float
    text: str


class SearchOutput(BaseModel):
    corpus: str
    query: str
    results: list[SearchHit]
    truncated: bool = Field(description="True when results or their text were cut to fit the size limit.")
    searched_as: list[str] = Field(
        default_factory=list,
        description="Other queries the knowledge base generated from yours and searched too. Empty when it does not rewrite queries.",
    )


class FetchedChunk(BaseModel):
    chunk_id: str
    ordinal: int
    role: Literal["before", "hit", "after"]
    page_start: int | None
    page_end: int | None
    heading_path: list[str]
    text: str


class FetchedSection(BaseModel):
    heading_path: list[str]
    first_chunk_id: str
    page_start: int | None
    page_end: int | None
    chunk_count: int


class FetchedDocument(BaseModel):
    document_id: str
    filename: str
    external_id: str | None
    metadata: dict[str, Any]


class FetchOutput(BaseModel):
    kind: Literal["chunk", "document"]
    document: FetchedDocument
    chunks: list[FetchedChunk] = Field(default_factory=list, description="Set when kind is 'chunk'.")
    sections: list[FetchedSection] = Field(default_factory=list, description="Set when kind is 'document'.")
    chunk_count: int | None = None
    truncated: bool = False
