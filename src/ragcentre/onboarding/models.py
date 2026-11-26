"""Setup contracts deliberately accept only non-secret settings, never arbitrary provider JSON."""

from __future__ import annotations

from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ragcentre.models import api

StepName = Literal["parser", "embedder", "store", "corpus", "pipeline", "active", "check_services"]
STEPS: tuple[StepName, ...] = ("parser", "embedder", "store", "corpus", "pipeline", "active", "check_services")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# Only settings used by the existing guided wizard. Other services are created with the full
# provider form and referenced by name, preserving plugins without persisting their arbitrary input.
SAFE_CONFIG: dict[tuple[str, str], set[str]] = {
    ("parser", "http"): {"base_url"},
    ("embedder", "builtin_hash"): {"dimensions"},
    ("embedder", "openai_compatible"): {"base_url", "model"},
    ("vector_store", "sqlite_vec"): set(),
    ("vector_store", "qdrant"): {"url"},
    ("vector_store", "chroma"): {"host", "port"},
}


class ProviderDraft(StrictModel):
    reuse: str | None = Field(default=None, min_length=1, max_length=63, pattern=r"^[a-z][a-z0-9-]*$")
    create: api.ProviderCreate | None = None

    @model_validator(mode="after")
    def safe(self) -> Self:
        if (self.reuse is None) == (self.create is None):
            raise ValueError("Choose an existing provider or non-secret creation settings.")
        if self.create is not None:
            c = self.create
            keys = SAFE_CONFIG.get((c.kind, c.type))
            if keys is None or set(c.config) - keys or c.team_id is not None:
                raise ValueError("Use the Services form for this provider, then select the saved provider.")
            for key, value in c.config.items():
                if key in {"base_url", "url"}:
                    if not isinstance(value, str) or len(value) > 2048:
                        raise ValueError("Enter a service URL without credentials.")
                    parts = urlsplit(value)
                    if (
                        parts.scheme not in {"http", "https"}
                        or not parts.hostname
                        or parts.username
                        or parts.password
                        or parts.query
                        or parts.fragment
                    ):
                        raise ValueError(
                            "Service URLs in setup cannot contain credentials, query strings or fragments."
                        )
                elif key == "host":
                    if (
                        not isinstance(value, str)
                        or not value
                        or len(value) > 253
                        or any(ch in value for ch in "/@?#:")
                    ):
                        raise ValueError("Enter a hostname without credentials.")
                elif key == "model":
                    if not isinstance(value, str) or not value.strip() or len(value) > 256:
                        raise ValueError("Enter a model identifier.")
                elif key in {"dimensions", "port"} and (type(value) is not int or not 1 <= value <= 65535):
                    raise ValueError("Enter a positive integer up to 65535.")
        return self


class SetupDraft(StrictModel):
    mode: Literal["trial", "custom"] = "trial"
    corpus_name: str = Field(default="", max_length=200)
    pipeline_name: str = Field(default="first-pipeline", min_length=1, max_length=200)
    retrieval: Literal["hybrid", "dense"] = "hybrid"
    parser: ProviderDraft | None = None
    embedder: ProviderDraft | None = None
    store: ProviderDraft | None = None
    dismissed: bool = False
    choice_step: int = Field(default=0, ge=0, le=4)

    @model_validator(mode="after")
    def kinds(self) -> Self:
        for field, kind in (("parser", "parser"), ("embedder", "embedder"), ("store", "vector_store")):
            pick = getattr(self, field)
            if pick and pick.create and pick.create.kind != kind:
                raise ValueError("The provider kind does not match its setup step.")
        if self.mode == "trial" and any((self.parser, self.embedder, self.store)):
            raise ValueError("Trial setup uses the built-in text, hash and SQLite components.")
        return self


class DraftWrite(StrictModel):
    expected_version: int = Field(ge=0)
    draft: SetupDraft


class DraftState(StrictModel):
    version: int
    draft: SetupDraft


class OperationCreate(StrictModel):
    draft_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    input_hash: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]+$")


class SetupStep(StrictModel):
    step: StepName
    state: Literal["pending", "running", "succeeded", "failed"]
    resource_id: str | None = None
    attempts: int = 0
    error_code: str | None = None
    lease_expires_at: str | None = None


class SetupOperation(StrictModel):
    id: str
    input_version: int
    input_hash: str
    draft: SetupDraft
    steps: list[SetupStep]
    created_at: str
    updated_at: str


class ProviderReadiness(StrictModel):
    id: str
    name: str
    credentials: dict[str, bool]
    verified: bool


class SetupReadiness(StrictModel):
    owner: bool
    corpus_id: str | None = None
    pipeline_id: str | None = None
    pipeline_fingerprint: str | None = None
    services: bool = False
    providers: list[ProviderReadiness] = Field(default_factory=list)
    indexed_documents: int = 0
    first_result_at: str | None = None
    ready: bool = False


class SetupState(StrictModel):
    draft: DraftState
    draft_hash: str
    operation: SetupOperation | None
    readiness: SetupReadiness
