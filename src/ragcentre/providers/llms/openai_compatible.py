"""Non-streaming chat completions from an OpenAI-compatible endpoint."""

from __future__ import annotations

from base64 import b64encode
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import Field, JsonValue, field_validator

from ragcentre.errors import ValidationFailed
from ragcentre.hashing import CanonicalJsonError, canonical
from ragcentre.providers.base import (
    BuildEnv,
    Completion,
    ContentPart,
    HttpConfig,
    HttpService,
    Message,
    ProviderTypeSpec,
    invalid_response,
)

_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
_RESERVED_FIELDS = frozenset({
    "model", "messages", "stream", "temperature", "top_p", "seed", "max_tokens",
    "max_completion_tokens", "response_format",
})  # fmt: skip


class OpenAICompatibleLLMConfig(HttpConfig):
    timeout_seconds: float = Field(default=120, gt=0, le=3600, description="Request timeout in seconds")
    model: str = Field(min_length=1, description="Chat model name")
    temperature: float | None = Field(
        default=0.0, ge=0, le=2, description="Sampling temperature; empty to use the server default"
    )
    top_p: float | None = Field(default=None, ge=0, le=1, description="Nucleus sampling probability")
    seed: int | None = Field(default=None, description="Sampling seed")
    max_tokens: int = Field(default=1024, ge=1, le=131072, description="Maximum output tokens per request")
    token_limit_field: Literal["max_tokens", "max_completion_tokens"] = Field(
        default="max_tokens",
        description="Request field for the token limit; OpenAI reasoning models need max_completion_tokens",
    )
    supports_images: bool = Field(default=False, description="Allow PNG, JPEG and WebP input")
    extra_body: dict[str, JsonValue] = Field(
        default_factory=dict, description="Additional request parameters as a JSON object"
    )
    revision: str = Field(default="", description="Optional revision tag for the model")

    @field_validator("extra_body")
    @classmethod
    def validate_extra_body(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        reserved = _RESERVED_FIELDS.intersection(value)
        if reserved:
            raise ValueError(f"Cannot override request fields: {', '.join(sorted(reserved))}")
        try:
            canonical(value)
        except (CanonicalJsonError, UnicodeEncodeError) as exc:
            raise ValueError("Additional request parameters must be valid UTF-8 JSON with finite numbers.") from exc
        return value


class OpenAICompatibleLLM(HttpService):
    def __init__(self, config: OpenAICompatibleLLMConfig, secrets: dict[str, str], env: BuildEnv) -> None:
        super().__init__(config, secrets, env)
        self.config = config
        self.supports_images = config.supports_images

    def _part(self, part: ContentPart) -> dict[str, Any]:
        if isinstance(part, str):
            return {"type": "text", "text": part}
        if not self.supports_images:
            raise ValidationFailed("This language model provider does not support images.")
        if part.mime_type not in _IMAGE_TYPES:
            raise ValidationFailed("Language model images must use image/png, image/jpeg or image/webp.")
        encoded = b64encode(part.data).decode("ascii")
        return {"type": "image_url", "image_url": {"url": f"data:{part.mime_type};base64,{encoded}"}}

    def complete(
        self,
        messages: Sequence[Message],
        *,
        max_tokens: int | None = None,
        json_output: bool = False,
    ) -> Completion:
        limit = self.config.max_tokens
        if max_tokens is not None:
            if max_tokens < 1:
                raise ValidationFailed("max_tokens must be at least 1.")
            limit = min(max_tokens, limit)
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {
                    "role": msg.role,
                    "content": msg.content
                    if isinstance(msg.content, str)
                    else [self._part(part) for part in msg.content],
                }
                for msg in messages
            ],
            self.config.token_limit_field: limit,
            "stream": False,
            **self.config.extra_body,
        }
        if self.config.temperature is not None:
            payload["temperature"] = self.config.temperature
        if self.config.top_p is not None:
            payload["top_p"] = self.config.top_p
        if self.config.seed is not None:
            payload["seed"] = self.config.seed
        if json_output:
            payload["response_format"] = {"type": "json_object"}

        body = self.json("POST", "/chat/completions", json=payload)
        choices = body.get("choices") if isinstance(body, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise invalid_response(self.name, "expected a non-empty 'choices' list with an object as the first choice")
        choice = choices[0]
        message = choice.get("message")
        reason = choice.get("finish_reason")
        content = message.get("content") if isinstance(message, dict) else None
        if content is None and reason == "length":
            content = ""  # the limit was reached before any answer text, typically while reasoning
        if not isinstance(content, str) or not (content.strip() or reason == "length"):
            detail = "expected non-empty text in choices[0].message.content"
            if isinstance(message, dict) and (message.get("reasoning_content") or message.get("reasoning")):
                detail += "; the model returned reasoning without answer text"
            raise invalid_response(self.name, detail)

        usage = body.get("usage")
        input_tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
        output_tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
        model = body.get("model")
        return Completion(
            text=content,
            finish_reason=reason if reason in ("stop", "length") else "other",
            model=model if isinstance(model, str) and model else None,
            input_tokens=input_tokens if type(input_tokens) is int else None,
            output_tokens=output_tokens if type(output_tokens) is int else None,
        )


def _factory(config: Any, secrets: dict[str, str], env: BuildEnv) -> OpenAICompatibleLLM:
    assert isinstance(config, OpenAICompatibleLLMConfig)
    return OpenAICompatibleLLM(config, secrets, env)


SPEC = ProviderTypeSpec(
    kind="llm",
    type="openai_compatible",
    label="OpenAI-compatible chat completions",
    description="A language model server exposing POST /chat/completions.",
    config_model=OpenAICompatibleLLMConfig,
    fingerprint_fields=(
        "model",
        "temperature",
        "top_p",
        "seed",
        "max_tokens",
        "supports_images",
        "extra_body",
        "revision",
    ),
    secret_keys=("api_key",),
    factory=_factory,
)
