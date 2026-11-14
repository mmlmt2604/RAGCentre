"""HTTP parser client for services speaking ``ragcentre.parser/v1`` (section 8.1)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field

from ragcentre.providers.base import BuildEnv, HttpConfig, InfoMixin, ProviderTypeSpec, invalid_response

PROTOCOL = "ragcentre.parser/v1"


class HttpParserConfig(HttpConfig):
    timeout_seconds: float = Field(default=600, gt=0, le=3600, description="Request timeout in seconds")
    revision: str = Field(default="", description="Optional revision tag; change it to force a new fingerprint")
    on_version_drift: Literal["fail", "warn"] = Field(
        default="fail", description="What to do when the service reports a different version"
    )


class HttpParser(InfoMixin):
    protocol = PROTOCOL

    def parse(self, content: bytes, filename: str, mime_type: str, options: Mapping[str, Any]) -> Any:
        body = self.json(
            "POST",
            "/parse",
            files={"file": (filename or "document", content, mime_type)},
            data={"options": json.dumps(dict(options))},
        )
        if not isinstance(body, dict):
            raise invalid_response(self.name, "/parse must return a Document IR object")
        return body


def _factory(config: Any, secrets: dict[str, str], env: BuildEnv) -> HttpParser:
    assert isinstance(config, HttpParserConfig)
    return HttpParser(config, secrets, env)


SPEC = ProviderTypeSpec(
    kind="parser",
    type="http",
    label="Parser service (HTTP)",
    description="Your own parser or OCR service speaking ragcentre.parser/v1.",
    config_model=HttpParserConfig,
    fingerprint_fields=("service_name", "service_version", "revision"),
    secret_keys=("api_key",),
    factory=_factory,
    captured_fields=("service_name", "service_version"),
    info_protocol=PROTOCOL,
)
