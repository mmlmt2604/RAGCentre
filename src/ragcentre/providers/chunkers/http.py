"""HTTP chunker client for services speaking ``ragcentre.chunker/v1`` (section 8.4)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field

from ragcentre.providers.base import BuildEnv, HttpConfig, InfoMixin, ProviderTypeSpec, invalid_response

PROTOCOL = "ragcentre.chunker/v1"


class HttpChunkerConfig(HttpConfig):
    timeout_seconds: float = Field(default=120, gt=0, le=3600, description="Request timeout in seconds")
    revision: str = Field(default="", description="Optional revision tag; change it to force a new fingerprint")
    on_version_drift: Literal["fail", "warn"] = Field(
        default="fail", description="What to do when the service reports a different version"
    )


class HttpChunker(InfoMixin):
    protocol = PROTOCOL

    def chunk(self, document: Mapping[str, Any], options: Mapping[str, Any]) -> list[dict[str, Any]]:
        body = self.json("POST", "/chunk", json={"document": dict(document), "options": dict(options)})
        chunks = body.get("chunks") if isinstance(body, dict) else None
        if not isinstance(chunks, list) or not all(isinstance(c, dict) for c in chunks):
            raise invalid_response(self.name, "/chunk must return {'chunks': [ {...}, ... ]}")
        return chunks


def _factory(config: Any, secrets: dict[str, str], env: BuildEnv) -> HttpChunker:
    assert isinstance(config, HttpChunkerConfig)
    return HttpChunker(config, secrets, env)


SPEC = ProviderTypeSpec(
    kind="chunker",
    type="http",
    label="Chunker service (HTTP)",
    description="A custom chunking service speaking ragcentre.chunker/v1.",
    config_model=HttpChunkerConfig,
    fingerprint_fields=("service_name", "service_version", "revision"),
    secret_keys=("api_key",),
    factory=_factory,
    captured_fields=("service_name", "service_version"),
    info_protocol=PROTOCOL,
)
