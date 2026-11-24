from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from ragcentre.context import AppContext
from ragcentre.errors import ExecutionError, ValidationFailed
from ragcentre.providers import registry
from ragcentre.providers.base import LLM, BuildEnv, Completion, ImagePart, Message, ProviderConfig, ProviderTypeSpec
from ragcentre.testing.fakes import LLM_URL, FakeServices

CHAT = "/chat/completions"
CONFIG = {"base_url": LLM_URL, "model": "local-chat"}
PROMPT = [Message("user", "Hello")]


@pytest.fixture
def llm(ctx: AppContext) -> Iterator[LLM]:
    row = ctx.providers.create("local-chat", "llm", "openai_compatible", CONFIG)
    with ctx.providers.lease(row.id) as instance:
        assert isinstance(instance, LLM)
        yield instance


def test_text_request_and_response(llm: LLM, fakes: FakeServices) -> None:
    assert llm.complete(PROMPT, max_tokens=2048) == Completion("OK", "stop", "local-chat", 5, 1)
    request = fakes.requests[-1]
    assert request.method == "POST"
    assert str(request.url) == LLM_URL + CHAT
    assert "authorization" not in request.headers
    assert json.loads(request.content) == {
        "model": "local-chat",
        "messages": [{"role": "user", "content": "Hello"}],
        "stream": False,
        "temperature": 0,
        "max_tokens": 1024,
    }


def test_multimodal_json_request(ctx: AppContext, fakes: FakeServices, monkeypatch: pytest.MonkeyPatch) -> None:
    row = ctx.providers.create(
        "vision",
        "llm",
        "openai_compatible",
        {
            **CONFIG,
            "supports_images": True,
            "temperature": 0.5,
            "top_p": 0.9,
            "seed": 0,
            "extra_body": {"chat_template_kwargs": {"thinking": False}},
        },
    )
    monkeypatch.setenv("RAGCENTRE_PROVIDER_TEST_LLM_KEY", "fake-test-key")
    ctx.providers.set_secret(row.id, "api_key", env="RAGCENTRE_PROVIDER_TEST_LLM_KEY")
    with ctx.providers.lease(row.id) as instance:
        completion = instance.complete(
            [Message("system", "Answer in JSON."), Message("user", ["Caption this", ImagePart(b"img", "image/png")])],
            max_tokens=10,
            json_output=True,
        )
    assert json.loads(completion.text) == {"reply": "OK"}
    request = fakes.requests[-1]
    assert request.headers["authorization"] == "Bearer fake-test-key"
    assert json.loads(request.content) == {
        "model": "local-chat",
        "messages": [
            {"role": "system", "content": "Answer in JSON."},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Caption this"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,aW1n"}},
                ],
            },
        ],
        "temperature": 0.5,
        "top_p": 0.9,
        "seed": 0,
        "max_tokens": 10,
        "stream": False,
        "response_format": {"type": "json_object"},
        "chat_template_kwargs": {"thinking": False},
    }


def test_reasoning_model_request(ctx: AppContext, fakes: FakeServices) -> None:
    config = {**CONFIG, "temperature": None, "token_limit_field": "max_completion_tokens"}
    row = ctx.providers.create("reasoning", "llm", "openai_compatible", config)
    with ctx.providers.lease(row.id) as instance:
        instance.complete(PROMPT, max_tokens=100)
    assert json.loads(fakes.requests[-1].content) == {
        "model": "local-chat",
        "messages": [{"role": "user", "content": "Hello"}],
        "stream": False,
        "max_completion_tokens": 100,
    }


@pytest.mark.parametrize("content", [None, ""])
def test_limit_reached_before_answer(llm: LLM, fakes: FakeServices, content: str | None) -> None:
    message = {"content": content, "reasoning": "private"}
    fakes.script(
        "llm", CHAT, [httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "length"}]})]
    )
    assert llm.complete(PROMPT) == Completion("", "length", None, None, None)


def test_rejects_images_when_disabled(llm: LLM, fakes: FakeServices) -> None:
    with pytest.raises(ValidationFailed, match="does not support images"):
        llm.complete([Message("user", [ImagePart(b"img", "image/png")])])
    assert fakes.count("llm") == 0


@pytest.mark.parametrize(("reason", "expected"), [("length", "length"), ("content_filter", "other"), (None, "other")])
def test_finish_reason_and_missing_usage(llm: LLM, fakes: FakeServices, reason: str | None, expected: str) -> None:
    body = {"choices": [{"message": {"content": "Partial answer"}, "finish_reason": reason}]}
    fakes.script("llm", CHAT, [httpx.Response(200, json=body)])
    completion = llm.complete(PROMPT)
    assert completion.finish_reason == expected
    assert completion.model is completion.input_tokens is completion.output_tokens is None


@pytest.mark.parametrize(("status", "retryable"), [(429, True), (500, True), (400, False)])
def test_http_errors(llm: LLM, fakes: FakeServices, status: int, retryable: bool) -> None:
    fakes.script("llm", CHAT, [httpx.Response(status, headers={"Retry-After": "7"})])
    with pytest.raises(ExecutionError) as exc:
        llm.complete(PROMPT)
    assert exc.value.upstream_status == status
    assert exc.value.retryable is retryable
    assert exc.value.retry_after == (7 if status == 429 else None)
    assert fakes.count("llm", CHAT) == 1


def test_timeout(llm: LLM, fakes: FakeServices) -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    fakes.script("llm", CHAT, [timeout])
    with pytest.raises(ExecutionError) as exc:
        llm.complete(PROMPT)
    assert exc.value.retryable
    assert exc.value.upstream_code == "timeout"


@pytest.mark.parametrize(
    ("response", "detail"),
    [
        (httpx.Response(200, text="not JSON"), "did not return JSON"),
        (httpx.Response(200, json={"choices": []}), "non-empty 'choices'"),
        (httpx.Response(200, json={"choices": [{"message": {"content": None}}]}), "non-empty text"),
        (
            httpx.Response(200, json={"choices": [{"message": {"content": None, "reasoning_content": "private"}}]}),
            "returned reasoning without answer text",
        ),
        (
            httpx.Response(
                200, json={"choices": [{"message": {"content": "", "reasoning": "private"}, "finish_reason": "stop"}]}
            ),
            "returned reasoning without answer text",
        ),
    ],
)
def test_invalid_response(llm: LLM, fakes: FakeServices, response: httpx.Response, detail: str) -> None:
    fakes.script("llm", CHAT, [response])
    with pytest.raises(ExecutionError, match=detail) as exc:
        llm.complete(PROMPT)
    assert not exc.value.retryable
    assert "private" not in exc.value.message


def create(client: TestClient, config: dict[str, Any], type_: str = "openai_compatible") -> httpx.Response:
    return client.post("/v1/providers", json={"kind": "llm", "type": type_, "name": "chat", "config": config})


@pytest.mark.parametrize("reserved", ["model", "messages", "stream", "max_tokens", "max_completion_tokens"])
def test_rejects_reserved_extra_body_fields(client: TestClient, reserved: str) -> None:
    response = create(client, {**CONFIG, "extra_body": {reserved: None}})
    assert response.status_code == 422
    assert response.json()["error"]["details"]["problems"][0]["path"] == "config.extra_body"


def test_blocked_destination(client: TestClient, fakes: FakeServices) -> None:
    response = create(client, {**CONFIG, "base_url": "http://169.254.169.254/v1"})
    assert response.status_code == 422
    assert "blocked destination" in response.json()["error"]["message"]
    assert fakes.count("llm") == 0


def test_rejects_nonfinite_extra_body_number(ctx: AppContext) -> None:
    with pytest.raises(ValidationFailed, match="finite numbers"):
        ctx.providers.create(
            "chat", "llm", "openai_compatible", {**CONFIG, "extra_body": {"nested": {"value": float("inf")}}}
        )


def test_llm_provider_lifecycle(client: TestClient, fakes: FakeServices) -> None:
    items = client.get("/v1/provider-types").json()["items"]
    spec = next(item for item in items if (item["kind"], item["type"]) == ("llm", "openai_compatible"))
    assert spec["secret_keys"] == ["api_key"]
    assert set(spec["config_schema"]["required"]) == {"base_url", "model"}
    assert spec["config_schema"]["properties"]["extra_body"]["type"] == "object"

    unknown = create(client, CONFIG, "unknown")
    assert unknown.status_code == 422
    assert "Installed types: openai_compatible" in unknown.json()["error"]["message"]
    response = create(client, CONFIG)
    assert response.status_code == 201, response.text
    provider = response.json()
    assert provider["config"]["timeout_seconds"] == 120
    assert provider["config"]["extra_body"] == {}
    path = f"/v1/providers/{provider['id']}"

    fakes.script(
        "llm",
        CHAT,
        [
            httpx.Response(
                200,
                json={
                    "model": "served-model",
                    "choices": [{"message": {"content": "Hello"}, "finish_reason": "stop"}],
                },
            )
        ],
    )
    probe = client.post(f"{path}/test").json()
    assert probe["ok"]
    assert probe["service"] == {"name": "served-model", "version": "", "mime_types": []}
    assert probe["message"] is None
    assert json.loads(fakes.requests[-1].content)["max_tokens"] == 64

    reasoning_only = {"choices": [{"message": {"content": None, "reasoning": "..."}, "finish_reason": "length"}]}
    fakes.script("llm", CHAT, [httpx.Response(200, json=reasoning_only)])
    probe = client.post(f"{path}/test").json()
    assert probe["ok"]
    assert "64-token test budget" in probe["message"]

    fakes.script("llm", CHAT, [503])
    probe = client.post(f"{path}/test").json()
    assert not probe["ok"]
    assert "503" in probe["message"]

    immutable = client.patch(path, json={"config": {"temperature": 0.5}})
    assert immutable.status_code == 409
    assert immutable.json()["error"]["code"] == "immutable_field"
    patched = client.patch(path, json={"config": {"timeout_seconds": 5, "token_limit_field": "max_completion_tokens"}})
    assert patched.status_code == 200
    assert patched.json()["config"]["timeout_seconds"] == 5
    assert patched.json()["config"]["token_limit_field"] == "max_completion_tokens"
    assert patched.json()["fingerprint"] == provider["fingerprint"]
    assert client.delete(path).status_code == 204
    assert client.get(path).status_code == 404


def test_llm_entry_point(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    class DummyLLM:
        supports_images = False

        def complete(self, messages: object, *, max_tokens: int | None = None, json_output: bool = False) -> Completion:
            return Completion("Hello", "stop", None, None, None)

    def factory(config: object, secrets: dict[str, str], env: BuildEnv) -> LLM:
        return DummyLLM()

    class EntryPoint:
        name = "dummy_llm"

        def load(self) -> ProviderTypeSpec:
            return ProviderTypeSpec("llm", "dummy", "Dummy", "Test provider", ProviderConfig, (), (), factory)

    def entry_points(*, group: str) -> list[EntryPoint]:
        assert group == "ragcentre.provider_types"
        return [EntryPoint()]

    monkeypatch.setattr(registry, "entry_points", entry_points)
    registry.provider_types.cache_clear()
    try:
        response = create(client, {}, "dummy")
        assert response.status_code == 201, response.text
        provider = response.json()
        result = client.post(f"/v1/providers/{provider['id']}/test").json()
        assert result["ok"]
        assert result["service"]["name"] == "chat"
    finally:
        registry.provider_types.cache_clear()
