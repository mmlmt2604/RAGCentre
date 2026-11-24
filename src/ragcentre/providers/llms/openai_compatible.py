"""LLM client for any server exposing an OpenAI-compatible ``POST /chat/completions``.

Covers llama-server, vLLM, Ollama, LM Studio and hosted APIs. Request:
``{"model", "messages", "max_tokens", "temperature"}``; the document (when there is one) is the
system message so that servers with prefix caching reuse it across a document's chunks. The
answer is ``choices[0].message.content``; ``usage.prompt_tokens`` and ``usage.completion_tokens``
are read when present.

The two budget fields are operational: they never change what is written, only whether a run is
allowed to spend the tokens, so they stay out of the fingerprint and can be edited later.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ragcentre.providers.base import BuildEnv, Completion, HttpConfig, HttpService, ProviderTypeSpec, invalid_response


class OpenAICompatibleLlmConfig(HttpConfig):
    model: str = Field(min_length=1, description="Model name")
    revision: str = Field(default="", description="Optional revision tag; change it to force a new fingerprint")
    max_tokens_per_run: int | None = Field(
        default=None,
        ge=1,
        description="Fail a document's enrichment when its estimated tokens (prompt and answer) exceed this",
    )
    daily_token_budget: int | None = Field(
        default=None,
        ge=1,
        description="Stop enriching for the rest of the UTC day once this many tokens have been used",
    )


def _token_count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


class OpenAICompatibleLlm(HttpService):
    def __init__(self, config: OpenAICompatibleLlmConfig, secrets: dict[str, str], env: BuildEnv) -> None:
        super().__init__(config, secrets, env)
        self.config = config
        self.max_concurrency = config.max_concurrency

    def complete(self, system: str | None, prompt: str, max_tokens: int, temperature: float) -> Completion:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body = self.json(
            "POST",
            "/chat/completions",
            json={
                "model": self.config.model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
        )
        choices = body.get("choices") if isinstance(body, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise invalid_response(self.name, "missing 'choices' list")
        message = choices[0].get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise invalid_response(self.name, "choices[0].message.content must be a string")
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        return Completion(
            text=content,
            input_tokens=_token_count(usage.get("prompt_tokens")),
            output_tokens=_token_count(usage.get("completion_tokens")),
        )


def _factory(config: Any, secrets: dict[str, str], env: BuildEnv) -> OpenAICompatibleLlm:
    assert isinstance(config, OpenAICompatibleLlmConfig)
    return OpenAICompatibleLlm(config, secrets, env)


SPEC = ProviderTypeSpec(
    kind="llm",
    type="openai_compatible",
    label="OpenAI-compatible chat completions",
    description="Any server exposing POST /chat/completions (llama-server, vLLM, Ollama, hosted APIs).",
    config_model=OpenAICompatibleLlmConfig,
    fingerprint_fields=("model", "revision"),
    secret_keys=("api_key",),
    factory=_factory,
)
