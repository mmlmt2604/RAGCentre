"""Request and response models. Response models mirror section 22 field for field."""

from __future__ import annotations

from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    WithJsonSchema,
    model_serializer,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema

T = TypeVar("T")

Metric = Literal["cosine", "l2", "dot"]
FieldType = Literal["str", "int", "float", "bool"]
Scalar = str | int | float | bool
ProviderKind = Literal["parser", "chunker", "embedder", "reranker", "vector_store", "llm", "connector"]


class RequestModel(BaseModel):
    """Base for request bodies: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


class ResponseModel(BaseModel):
    """Base for response bodies. Every field is always sent, so the OpenAPI schema marks fields
    with defaults as required too (the UI's generated types depend on it)."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class Page(ResponseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None


class Health(ResponseModel):
    status: Literal["ok"]
    version: str
    migration: int
    workers: int
    auth_required: bool
    secret_storage_enabled: bool


# ---------- Teams & auth ----------


class Team(ResponseModel):
    id: str
    name: str
    slug: str
    created_at: str


class TeamCreate(RequestModel):
    name: str
    slug: str | None = None  # derived from name (lowercased, slugified) if omitted
    owner_user_id: str | None = None


class TeamTokenCreate(RequestModel):
    kind: Literal["full", "read"]
    name: str


class TeamToken(ResponseModel):  # never carries the token value
    id: str
    team_id: str
    kind: Literal["full", "read"]
    name: str
    created_at: str
    last_used_at: str | None


class TeamTokenCreated(ResponseModel):  # returned exactly once, at creation
    id: str
    team_id: str
    kind: Literal["full", "read"]
    name: str
    token: str  # the raw opaque token; shown once, never stored
    created_at: str


class WhoAmI(ResponseModel):
    scope: Literal["admin", "team", "read", "none"]
    team_id: str | None
    team_name: str | None


class AuthConfig(ResponseModel):
    mode: Literal["legacy", "local"]
    setup_required: bool


class LocalLogin(RequestModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=128)


class AccountSetup(RequestModel):
    token: str = Field(min_length=20, max_length=128)
    password: str = Field(max_length=128)


class PasswordChange(RequestModel):
    current_password: str = Field(max_length=128)
    password: str = Field(max_length=128)


class User(ResponseModel):
    id: str
    username: str
    display_name: str
    status: Literal["pending", "active", "disabled"]
    is_platform_admin: bool
    created_at: str
    updated_at: str


class UserCreate(RequestModel):
    username: str = Field(max_length=64)
    display_name: str = Field(default="", max_length=200)


class UserPatch(RequestModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    status: Literal["active", "disabled"] | None = None
    is_platform_admin: bool | None = None


class UserSetupCreated(ResponseModel):
    user: User
    setup_token: str


class TeamMembership(ResponseModel):
    team_id: str
    team_name: str
    team_slug: str
    role: Literal["viewer", "editor", "owner"]


class TeamMember(ResponseModel):
    user_id: str
    username: str
    display_name: str
    status: Literal["pending", "active", "disabled"]
    role: Literal["viewer", "editor", "owner"]


class MembershipSet(RequestModel):
    role: Literal["viewer", "editor", "owner"]


class CurrentUser(ResponseModel):
    user: User
    memberships: list[TeamMembership]
    csrf_token: str


class CsrfToken(ResponseModel):
    csrf_token: str


class ProviderSummary(ResponseModel):
    id: str
    name: str
    kind: ProviderKind
    type: str
    builtin: bool
    team_id: str | None


# ---------- Data connectors (sync sources) ----------


class SourceCreate(RequestModel):
    name: str
    provider_id: str
    scope: dict[str, Any] = Field(default_factory=dict)
    schedule_seconds: int = Field(default=3600, ge=0)
    metadata_template: dict[str, Any] = Field(default_factory=dict)
    attributes_template: dict[str, Any] = Field(default_factory=dict)


class SourcePatch(RequestModel):
    scope: dict[str, Any] | None = None
    schedule_seconds: int | None = Field(default=None, ge=0)
    metadata_template: dict[str, Any] | None = None
    attributes_template: dict[str, Any] | None = None


class Source(ResponseModel):
    id: str
    corpus_id: str
    provider_id: str
    name: str
    scope: dict[str, Any]
    schedule_seconds: int
    cursor: str | None
    status: Literal["idle", "syncing", "error"]
    next_run_at: str | None
    last_run_at: str | None
    last_error: str | None
    metadata_template: dict[str, Any]
    attributes_template: dict[str, Any]
    created_at: str
    updated_at: str


class SourceItem(ResponseModel):
    source_id: str
    remote_id: str
    remote_version: str
    document_id: str | None
    remote_path: str | None
    remote_mime: str | None
    remote_size: int | None
    remote_modified: str | None
    skipped_reason: str | None
    last_seen_at: str


# ---------- Providers ----------


class StoreCapabilities(ResponseModel):
    metrics: list[Metric]
    max_batch: int
    multi_vector: bool
    native_hybrid: bool
    max_dim: int | None
    consistency: Literal["immediate", "eventual"]
    filter_exact: bool
    max_collections: int | None


class ProviderType(ResponseModel):
    kind: ProviderKind
    type: str
    label: str
    description: str
    config_schema: dict[str, Any]
    fingerprint_fields: list[str]
    secret_keys: list[str]
    capabilities: StoreCapabilities | None


class SecretState(ResponseModel):
    source: Literal["env", "stored"]
    set: bool
    env_var: str | None


class Provider(ResponseModel):
    id: str
    name: str
    kind: ProviderKind
    type: str
    config: dict[str, Any]
    fingerprint: str
    secrets: dict[str, SecretState]
    builtin: bool
    used_by_pipelines: int
    team_id: str | None  # None = built-in
    created_at: str
    updated_at: str


class ProviderCreate(RequestModel):
    name: str
    kind: ProviderKind
    type: str
    config: dict[str, Any] = Field(default_factory=dict)
    team_id: str | None = None


class ProviderPatch(RequestModel):
    config: dict[str, Any] = Field(default_factory=dict)


class SecretSet(RequestModel):
    env: str | None = None
    value: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> SecretSet:
        if (self.env is None) == (self.value is None):
            raise ValueError('send exactly one of {"env": "VAR_NAME"} or {"value": "..."}')
        return self


class ServiceDescription(ResponseModel):
    name: str
    version: str
    mime_types: list[str]


class VersionDrift(ResponseModel):
    pinned: str
    current: str


class ParserFinding(ResponseModel):
    level: Literal["error", "warn", "info"]
    check: str
    message: str
    lost: str | None


class ParserCheck(ResponseModel):
    """The result of a deep parser test (``?deep=true``): what the service returned for a sample
    document, and which RAGCentre features its output feeds."""

    ok: bool
    blocks: int
    pages: int
    seconds: float
    findings: list[ParserFinding]


class StoreFinding(ResponseModel):
    level: Literal["error", "warn", "info"]
    check: str
    message: str
    ms: float
    lost: str | None


class StoreCheck(ResponseModel):
    """The result of a deep vector-store test (``?deep=true``): a contract-lite run against a
    temporary collection, one finding per capability, always cleaned up afterwards."""

    ok: bool
    store_type: str
    collection: str
    records: int
    metrics: list[str]
    seconds: float
    findings: list[StoreFinding]


class ProviderTestResult(ResponseModel):
    ok: bool
    latency_ms: float
    message: str | None
    service: ServiceDescription | None
    dimensions: int | None
    version_drift: VersionDrift | None
    check: ParserCheck | StoreCheck | None = None


# ---------- OCR services ----------

OcrState = Literal[
    "looking",
    "detected",
    "checking",
    "ready",
    "failed",
    "engine_down",
    "needs_key",
    "changed",
    "offline",
    "no_ocr",
]
OcrStep = Literal["ok", "failed", "running", "unknown"]


class OcrSteps(ResponseModel):
    """The three facts behind a state. ``ready`` is all three ``ok``."""

    shim: OcrStep
    engine: OcrStep
    text: OcrStep


class OcrService(ResponseModel):
    id: str
    provider_id: str | None  # null: found, not added yet
    provider_name: str | None
    suggested_name: str | None  # a free provider name, for one that was found
    base_url: str
    name: str | None
    version: str | None
    state: OcrState
    steps: OcrSteps
    busy: bool  # it accepts connections but is slow to answer, as an engine at work is
    rechecking: bool  # a test is running and ``state`` is still the last result
    message: str
    detail: str | None
    checked_at: str | None
    check_seconds: float | None


class OcrStatus(ResponseModel):
    services: list[OcrService]
    scanning: bool  # some address has not been looked at yet
    discovery_enabled: bool
    in_container: bool


# ---------- Corpora ----------

RunState = Literal["pending", "running", "succeeded", "failed", "blocked"]
PipelineState = Literal["building", "ready", "degraded"]


class FieldSpec(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    type: FieldType
    default: Scalar


class MetadataSchema(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    fields: dict[str, FieldSpec] = Field(default_factory=dict)


class CorpusCreate(RequestModel):
    name: str
    metadata_schema: MetadataSchema = Field(default_factory=MetadataSchema)
    team_id: str | None = None  # admin may set; team principals are pinned to their own team


class CorpusPatch(RequestModel):
    name: str


class ActivePipelineSet(RequestModel):
    pipeline_id: str
    force: bool = False


class PipelineSummary(ResponseModel):
    id: str
    name: str
    fingerprint: str
    archived: bool
    status: PipelineState
    coverage: float
    created_at: str


class Corpus(ResponseModel):
    id: str
    name: str
    metadata_schema: MetadataSchema
    active_pipeline_id: str | None
    document_count: int
    pipelines: list[PipelineSummary]
    team_id: str
    created_at: str


# ---------- Pipelines ----------


class PipelineProblem(ResponseModel):
    node_id: str | None
    path: str
    message: str


class PlanNode(ResponseModel):
    node_id: str
    type: str
    node_fp: str
    reused: bool
    shared_with: list[str]
    runs_to_create: int


class PipelinePlan(ResponseModel):
    valid: bool
    problems: list[PipelineProblem]
    pipeline_fingerprint: str | None
    nodes: list[PlanNode]
    total_runs_to_create: int


# The pipeline configuration as submitted (section 5), returned in ``Pipeline.config``. It was
# validated when the pipeline was created; these models give it a precise schema. Optional settings
# the user left out are omitted from the response (never sent as null), matching the contract's
# ``field?: T``.


class _ConfigOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # No return annotation on purpose: pydantic then keeps the model's own schema for serialization
    # (an annotation such as ``Any`` would replace it).
    @model_serializer(mode="wrap")
    def _omit_unset(self, handler: SerializerFunctionWrapHandler):  # type: ignore[no-untyped-def]
        data = handler(self)
        return {k: v for k, v in data.items() if v is not None} if isinstance(data, dict) else data


class RouteOut(_ConfigOut):
    mime: str
    provider: str
    options: dict[str, Any] | SkipJsonSchema[None] = None


class ParseNodeOut(_ConfigOut):
    id: str
    type: Literal["parse"]
    routes: list[RouteOut]


ChunkStrategy = Annotated[
    str,
    # The built-in strategies; a third-party strategy would need the contract widened.
    WithJsonSchema({"type": "string", "enum": ["fixed", "recursive", "structure", "page", "http"]}),
]


class ChunkNodeOut(_ConfigOut):
    id: str
    type: Literal["chunk"]
    inputs: tuple[str]
    strategy: ChunkStrategy
    options: dict[str, Any] | SkipJsonSchema[None] = None
    provider: str | SkipJsonSchema[None] = None


class EnrichOptionsOut(_ConfigOut):
    mode: Literal["situate", "summary", "keywords", "questions"] | SkipJsonSchema[None] = None
    prompt: str | SkipJsonSchema[None] = None
    max_doc_chars: int | SkipJsonSchema[None] = None
    max_tokens: int | SkipJsonSchema[None] = None
    temperature: float | SkipJsonSchema[None] = None


class EnrichNodeOut(_ConfigOut):
    id: str
    type: Literal["enrich"]
    inputs: tuple[str]
    provider: str
    options: EnrichOptionsOut | SkipJsonSchema[None] = None


class EmbedNodeOut(_ConfigOut):
    id: str
    type: Literal["embed"]
    inputs: tuple[str]
    provider: str


class IndexVectorOptionsOut(_ConfigOut):
    metric: Metric | SkipJsonSchema[None] = None


class IndexVectorNodeOut(_ConfigOut):
    id: str
    type: Literal["index_vector"]
    inputs: tuple[str]
    provider: str
    options: IndexVectorOptionsOut | SkipJsonSchema[None] = None


class IndexFtsOptionsOut(_ConfigOut):
    tokenizer: Literal["unicode61", "trigram"] | SkipJsonSchema[None] = None


class IndexFtsNodeOut(_ConfigOut):
    id: str
    type: Literal["index_fts"]
    inputs: tuple[str]
    options: IndexFtsOptionsOut | SkipJsonSchema[None] = None


PipelineNodeOut = Annotated[
    ParseNodeOut | ChunkNodeOut | EnrichNodeOut | EmbedNodeOut | IndexVectorNodeOut | IndexFtsNodeOut,
    Field(discriminator="type"),
]


class SourceOut(_ConfigOut):
    id: str
    index: str
    top_k: int


class FusionOut(_ConfigOut):
    # method has a default when submitted; the response always includes it.
    model_config = ConfigDict(extra="ignore", json_schema_serialization_defaults_required=True)
    method: Literal["rrf", "weighted"] = "rrf"
    k: int | SkipJsonSchema[None] = None
    weights: dict[str, float] | SkipJsonSchema[None] = None


class RerankOut(_ConfigOut):
    provider: str
    top_n: int


class ExpandOut(_ConfigOut):
    model_config = ConfigDict(extra="ignore", json_schema_serialization_defaults_required=True)
    neighbors: int = 0


class RetrievalConfigOut(_ConfigOut):
    sources: list[SourceOut]
    fusion: FusionOut | SkipJsonSchema[None] = None
    rerank: RerankOut | SkipJsonSchema[None] = None
    expand: ExpandOut | SkipJsonSchema[None] = None
    top_k: int


class PipelineConfig(_ConfigOut):
    name: str
    nodes: list[PipelineNodeOut]
    retrieval: RetrievalConfigOut


class PipelineNodeRef(ResponseModel):
    node_id: str
    type: str
    node_fp: str
    shared_with: list[str]


class Pipeline(PipelineSummary):
    corpus_id: str
    config: PipelineConfig
    is_active: bool
    nodes: list[PipelineNodeRef]


class RunCounts(ResponseModel):
    pending: int
    running: int
    succeeded: int
    failed: int
    blocked: int


class NodeRunPipeline(ResponseModel):
    pipeline_id: str
    pipeline_name: str
    node_id: str


class NodeRun(ResponseModel):
    id: int
    corpus_id: str
    document_id: str
    document_filename: str
    node_fp: str
    node_type: str
    pipelines: list[NodeRunPipeline]
    status: RunState
    attempt: int
    max_attempts: int
    blocked_by_run_id: int | None
    error_code: str | None
    error_message: str | None
    stats: dict[str, Any] | None
    run_after: str
    started_at: str | None
    finished_at: str | None


class PipelineStatusNode(ResponseModel):
    node_id: str
    type: str
    node_fp: str
    counts: RunCounts


class PipelineStatus(ResponseModel):
    pipeline_id: str
    status: PipelineState
    coverage: float
    document_count: int
    nodes: list[PipelineStatusNode]
    recent_failures: list[NodeRun]


class ConfigChange(ResponseModel):
    path: str
    a: Any
    b: Any


class DiffNode(ResponseModel):
    node_id: str
    type: str
    in_a: bool
    in_b: bool
    same: bool
    changes: list[ConfigChange]


class PipelineDiff(ResponseModel):
    a: str
    b: str
    nodes: list[DiffNode]
    retrieval_same: bool
    retrieval_changes: list[ConfigChange]


class RetryResult(ResponseModel):
    retried: int


# ---------- Documents ----------


class DocumentIngestionNode(ResponseModel):
    node_id: str
    node_fp: str
    run_id: int
    status: RunState
    error_code: str | None


class DocumentIngestion(ResponseModel):
    pipeline_id: str
    state: RunState
    nodes: list[DocumentIngestionNode]


class Document(ResponseModel):
    id: str
    corpus_id: str
    external_id: str | None
    filename: str
    mime_type: str
    size_bytes: int
    content_sha256: str
    metadata: dict[str, Scalar]
    attributes: dict[str, Any]
    revision: int
    status: Literal["active", "deleting"]
    ingestion: list[DocumentIngestion]
    created_at: str
    updated_at: str


class Chunk(ResponseModel):
    id: str
    document_id: str
    ordinal: int
    text: str
    context: str | None
    enrichment: str | None
    page_start: int | None
    page_end: int | None
    heading_path: list[str]
    block_ids: list[str]
    char_count: int


class DocumentPatch(RequestModel):
    metadata: dict[str, Scalar] | None = None
    attributes: dict[str, Any] | None = None


# ---------- Query ----------


class QueryRequest(RequestModel):
    query: str
    pipeline_id: str | None = None
    top_k: int | None = Field(default=None, ge=1, le=1000)
    filter: dict[str, Any] | None = None
    allow_partial: bool = False
    debug: bool = False
    # False skips the pipeline's query transform for this request; unset or true uses it.
    transform: bool | None = None


class QuerySource(ResponseModel):
    rank: int
    score: float


class QueryDocument(ResponseModel):
    filename: str
    external_id: str | None
    metadata: dict[str, Scalar]
    attributes: dict[str, Any]


class QueryResult(ResponseModel):
    chunk_id: str
    document_id: str
    score: float
    rank: int
    text: str
    context: str | None
    # What the pipeline's enrich node wrote for the chunk; null without one (section 19).
    enrichment: str | None
    page_start: int | None
    page_end: int | None
    heading_path: list[str]
    document: QueryDocument
    context_before: list[str]
    context_after: list[str]
    sources: dict[str, QuerySource]
    # Indexes of the probes that retrieved this chunk. Only sent for a transformed query with debug on.
    probes: list[int] | None = None


class QueryProbe(ResponseModel):
    index: int
    text: str
    kind: Literal["original", "rewrite", "paraphrase", "hypothetical", "sub_question"]


class QueryTransformInfo(ResponseModel):
    mode: Literal["rewrite", "multi_query", "hyde", "decompose"]
    probes: list[QueryProbe]
    cached: bool


class QueryResponse(ResponseModel):
    pipeline_id: str
    pipeline_fingerprint: str
    partial: bool
    # Sources whose index holds the enriched text; empty when the pipeline has no enrich node.
    enriched_sources: list[str]
    results: list[QueryResult]
    timings_ms: dict[str, float]
    # What the language model produced from the query; null when the pipeline has no transform
    # or the request turned it off.
    transform: QueryTransformInfo | None = None


# ---------- Evaluation ----------


class EvalTarget(ResponseModel):
    document_id: str
    document_label: str
    pages: list[int] | None
    text_contains: str | None


class EvalItem(ResponseModel):
    id: str
    question: str
    targets: list[EvalTarget]
    filter: dict[str, Any] | None
    metadata: dict[str, Any]


class EvalDataset(ResponseModel):
    id: str
    corpus_id: str
    name: str
    item_count: int
    created_at: str


class EvalRunCreate(RequestModel):
    dataset_id: str
    pipeline_ids: list[str]
    k_values: list[int] | None = None
    allow_partial: bool = False


class EvalProgress(ResponseModel):
    done: int
    total: int


class EvalRun(ResponseModel):
    id: str
    corpus_id: str
    dataset_id: str
    dataset_name: str
    pipeline_ids: list[str]
    k_values: list[int]
    status: Literal["pending", "running", "finished", "failed"]
    progress: EvalProgress
    item_count: int
    created_at: str
    finished_at: str | None


class LatencySummary(ResponseModel):
    p50: float
    p95: float


class EvalSummary(ResponseModel):
    pipeline_id: str
    pipeline_name: str
    pipeline_fingerprint: str
    pipeline_archived: bool
    metrics: dict[str, float]
    latency_ms: LatencySummary
    error_count: int
    chunk_count: int
    mean_chunk_chars: float


class EvalComparison(ResponseModel):
    a: str
    b: str
    metric: str
    mean_a: float
    mean_b: float
    diff: float
    ci_low: float
    ci_high: float
    significant: bool


class EvalRunDetail(EvalRun):
    summaries: list[EvalSummary]
    comparisons: list[EvalComparison]


class EvalRetrieved(ResponseModel):
    rank: int
    chunk_id: str
    document_id: str
    document_label: str
    text: str
    page_start: int | None
    page_end: int | None
    matched_targets: list[int]


class EvalItemResult(ResponseModel):
    pipeline_id: str
    metrics: dict[str, float]
    latency_ms: float | None
    error: str | None
    retrieved: list[EvalRetrieved]


class EvalItemComparison(ResponseModel):
    item: EvalItem
    a: EvalItemResult
    b: EvalItemResult
    outcome: Literal["improved", "regressed", "unchanged"]


# ---- admin -----------------------------------------------------------------------------------


class GcRequest(RequestModel):
    dry_run: bool = True


class CacheClearRequest(RequestModel):
    transforms: bool = False


class CacheClearReport(ResponseModel):
    transforms_deleted: int


class GcIndex(ResponseModel):
    id: str
    kind: Literal["vector", "fts"]
    physical_name: str
    corpus_id: str


class GcReport(ResponseModel):
    dry_run: bool
    indexes: list[GcIndex]
    node_count: int
    blob_count: int
    blob_bytes: int
