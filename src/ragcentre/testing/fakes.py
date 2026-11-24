"""Fake parser, embedder, reranker, chunker and LLM services on ``httpx.MockTransport``.

Use ``FakeServices().client_factory`` as the context's ``http_client_factory``; every HTTP
provider then talks to these fakes. Each service counts its calls, and failures can be scripted
per endpoint::

    fakes = FakeServices()
    fakes.script("parser", "/parse", [503, 503, 503])   # three 503s, then normal behaviour
    fakes.fail_on("embedder", "/embeddings", {3})        # the third call returns 500
"""

from __future__ import annotations

import json
import re
import threading
from collections import Counter
from collections.abc import Callable, Iterable
from email.parser import BytesParser
from email.policy import HTTP
from typing import Any

import httpx

from ragcentre.providers.embedders.builtin_hash import hash_embed
from ragcentre.providers.parsers.builtin_text import decode, parse_text

PARSER_URL = "http://parser.test"
EMBEDDER_URL = "http://embedder.test/v1"
RERANKER_URL = "http://reranker.test"
CHUNKER_URL = "http://chunker.test"
LLM_URL = "http://llm.test/v1"

HOSTS = {
    "parser.test": "parser",
    "embedder.test": "embedder",
    "reranker.test": "reranker",
    "chunker.test": "chunker",
    "llm.test": "llm",
}

Step = int | httpx.Response | Callable[[httpx.Request], httpx.Response]


def _error(status: int, message: str, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": "fake_error", "message": message}}, headers=headers)


def paged_ir(text: str) -> dict[str, Any]:
    """Pages split on form feeds; each page's paragraphs become blocks with a page number."""
    blocks: list[dict[str, Any]] = []
    pages = text.split("\f")
    for number, page in enumerate(pages, start=1):
        for para in re.split(r"\n\s*\n", page):
            if para.strip():
                blocks.append({"type": "paragraph", "text": para.strip(), "page": number})
    return {
        "schema_version": "1.0",
        "metadata": {"page_count": len(pages)},
        "pages": [{"page": n, "width": 612.0, "height": 792.0} for n in range(1, len(pages) + 1)],
        "blocks": blocks,
    }


def _default_llm_reply(system: str | None, prompt: str) -> str:
    """A deterministic stand-in answer: names the chunk's first words."""
    match = re.search(r"<chunk>\n(.*?)\n</chunk>", prompt, re.S)
    words = (match.group(1) if match else prompt).split()[:6]
    return "About: " + " ".join(words)


def _multipart(request: httpx.Request) -> dict[str, tuple[str | None, str, bytes]]:
    """Parse a multipart/form-data body into {name: (filename, content type, bytes)}."""
    ctype = request.headers.get("content-type", "")
    raw = b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + request.content
    msg = BytesParser(policy=HTTP).parsebytes(raw)
    out: dict[str, tuple[str | None, str, bytes]] = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        payload = part.get_payload(decode=True)
        out[str(name)] = (part.get_filename(), part.get_content_type(), payload if isinstance(payload, bytes) else b"")
    return out


class FakeServices:
    def __init__(self, embed_dim: int = 64) -> None:
        self.lock = threading.Lock()
        self.calls: Counter[tuple[str, str]] = Counter()
        self.requests: list[httpx.Request] = []
        self._scripts: dict[tuple[str, str], list[Step]] = {}
        self._fail_on: dict[tuple[str, str], set[int]] = {}
        self.embed_dim = embed_dim
        self.embedded_texts: list[str] = []
        self.parser_name = "fake-parser"
        self.parser_version = "1.0.0"
        self.parser_mime_types = ["application/pdf", "text/*"]
        self.chunker_name = "fake-chunker"
        self.chunker_version = "0.3.0"
        self.echo_auth_in_errors = False
        self.parse_override: Callable[[bytes, str, dict[str, Any]], Any] | None = None
        # Async mode (parser protocol 1.1): the fake advertises capabilities.async, answers
        # POST /parse?mode=async with a job, and reports it "running" for ``job_running_polls``
        # status requests before it succeeds. ``job_failure`` makes jobs end in ``failed`` with
        # that ``{"http_status", "code", "message"}`` error.
        self.parser_async = False
        self.parser_options_schema: dict[str, Any] | None = None  # published as /info options_schema
        self.job_running_polls = 0
        self.job_failure: dict[str, Any] | None = None
        self.jobs: dict[str, dict[str, Any]] = {}
        # LLM: every chat request is recorded; ``llm_reply(system, prompt)`` writes the answer.
        self.llm_requests: list[dict[str, Any]] = []
        self.llm_reply: Callable[[str | None, str], str] = _default_llm_reply
        self.llm_usage = True  # report usage token counts

    # ---- scripting ----------------------------------------------------------------------------

    def script(self, service: str, path: str, steps: Iterable[Step]) -> None:
        with self.lock:
            self._scripts.setdefault((service, path), []).extend(steps)

    def fail_on(self, service: str, path: str, call_numbers: set[int], status: int = 500) -> None:
        with self.lock:
            self._fail_on[(service, path)] = set(call_numbers)
        self._fail_status = status

    def count(self, service: str, path: str | None = None) -> int:
        with self.lock:
            if path is not None:
                return self.calls[(service, path)]
            return sum(n for (s, _), n in self.calls.items() if s == service)

    def reset_counts(self) -> None:
        with self.lock:
            self.calls.clear()
            self.requests.clear()
            self.embedded_texts.clear()

    # ---- transport ----------------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client_factory(self, **kwargs: Any) -> httpx.Client:
        return httpx.Client(transport=self.transport(), **kwargs)

    def handle(self, request: httpx.Request) -> httpx.Response:
        service = HOSTS.get(request.url.host)
        if service is None:
            raise httpx.ConnectError(f"no fake service at {request.url.host}", request=request)
        path = request.url.path
        if service in ("embedder", "llm") and path.startswith("/v1"):
            path = path[3:]
        if service == "parser" and path.startswith("/jobs/"):
            return self._parser_job(request, path)
        key = (service, path)
        with self.lock:
            self.calls[key] += 1
            self.requests.append(request)
            number = self.calls[key]
            steps = self._scripts.get(key)
            step = steps.pop(0) if steps else None
            failing = number in self._fail_on.get(key, set())
        if step is not None:
            if isinstance(step, int):
                headers = {"Retry-After": "0"} if step in (429, 503) else None
                return _error(step, f"scripted {step}", headers)
            if isinstance(step, httpx.Response):
                return step
            return step(request)
        if failing:
            return _error(getattr(self, "_fail_status", 500), f"scripted failure on call {number}")
        if self.echo_auth_in_errors and request.headers.get("authorization"):
            return _error(401, f"bad credentials: {request.headers['authorization']}")
        handler = getattr(self, f"_{service}_{path.strip('/').replace('/', '_')}", None)
        if handler is None:
            return _error(404, f"no route {request.method} {path}")
        response: httpx.Response = handler(request)
        return response

    # ---- parser -------------------------------------------------------------------------------

    def _parser_info(self, request: httpx.Request) -> httpx.Response:
        extra = {"options_schema": self.parser_options_schema} if self.parser_options_schema is not None else {}
        return httpx.Response(
            200,
            json={
                **extra,
                "protocol": "ragcentre.parser/v1",
                "name": self.parser_name,
                "version": self.parser_version,
                "mime_types": self.parser_mime_types,
                "capabilities": {"page_images": False, **({"async": True} if self.parser_async else {})},
            },
        )

    def _parser_parse(self, request: httpx.Request) -> httpx.Response:
        form = _multipart(request)
        _, mime, content = form.get("file", (None, "application/octet-stream", b""))
        options = json.loads(form.get("options", (None, "", b"{}"))[2] or b"{}")
        if self.parse_override is not None:
            ir = self.parse_override(content, mime, options)
        elif mime.startswith("text/"):
            ir = parse_text(content, mime)
        else:
            ir = paged_ir(decode(content))
        if self.parser_async and request.url.params.get("mode") == "async":
            with self.lock:
                job_id = f"job-{len(self.jobs) + 1}"
                self.jobs[job_id] = {"polls": 0, "ir": ir}
            return httpx.Response(202, json={"job_id": job_id, "poll_after_seconds": 0.5})
        return httpx.Response(200, json=ir)

    def forget_job(self, job_id: str) -> None:
        """Make the service lose a job, as a restart would."""
        with self.lock:
            self.jobs.pop(job_id, None)

    def _parser_job(self, request: httpx.Request, path: str) -> httpx.Response:
        parts = path.strip("/").split("/")  # jobs, <id>[, result]
        suffix = "/jobs/result" if parts[2:] == ["result"] else "/jobs"
        with self.lock:
            self.calls[("parser", suffix)] += 1
            self.requests.append(request)
            job = self.jobs.get(parts[1])
            if request.method == "DELETE":
                self.jobs.pop(parts[1], None)
                return httpx.Response(204)
            if job is None:
                return _error(404, "unknown or expired job")
            if suffix == "/jobs/result":
                if self.job_failure is not None:
                    err = self.job_failure
                    return httpx.Response(err["http_status"], json={"error": err})
                return httpx.Response(200, json=job["ir"])
            job["polls"] += 1
            if job["polls"] <= self.job_running_polls:
                return httpx.Response(200, json={"status": "running"})
            if self.job_failure is not None:
                return httpx.Response(200, json={"status": "failed", "error": self.job_failure})
            return httpx.Response(200, json={"status": "succeeded"})

    # ---- embedder -----------------------------------------------------------------------------

    def _embedder_embeddings(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        inputs = body["input"]
        dim = body.get("dimensions") or self.embed_dim
        with self.lock:
            self.embedded_texts.extend(inputs)
        data = [{"object": "embedding", "index": i, "embedding": hash_embed(t, dim)} for i, t in enumerate(inputs)]
        data.reverse()  # exercise ordering by index
        return httpx.Response(200, json={"object": "list", "data": data, "model": body["model"]})

    # ---- llm ----------------------------------------------------------------------------------

    def _llm_chat_completions(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        with self.lock:
            self.llm_requests.append(body)
        messages = body["messages"]
        system = next((m["content"] for m in messages if m["role"] == "system"), None)
        prompt = messages[-1]["content"]
        text = self.llm_reply(system, prompt)
        out: dict[str, Any] = {
            "object": "chat.completion",
            "model": body["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        }
        if self.llm_usage:
            out["usage"] = {
                "prompt_tokens": (len(system or "") + len(prompt)) // 4,
                "completion_tokens": len(text) // 4 + 1,
            }
        return httpx.Response(200, json=out)

    # ---- reranker -----------------------------------------------------------------------------

    def _reranker_rerank(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        query_words = set(re.findall(r"\w+", body["query"].lower()))
        scored = []
        for i, doc in enumerate(body["documents"]):
            words = set(re.findall(r"\w+", doc.lower()))
            overlap = len(query_words & words) / (len(query_words) or 1)
            scored.append({"index": i, "relevance_score": round(overlap + 1.0 / (1000 + len(doc)), 6)})
        scored.sort(key=lambda r: -r["relevance_score"])
        return httpx.Response(200, json={"results": scored[: body.get("top_n") or len(scored)]})

    # ---- chunker ------------------------------------------------------------------------------

    def _chunker_info(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"protocol": "ragcentre.chunker/v1", "name": self.chunker_name, "version": self.chunker_version},
        )

    def _chunker_chunk(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        blocks = [b for b in body["document"]["blocks"] if b["type"] not in ("page_header", "page_footer")]
        per = int(body.get("options", {}).get("blocks_per_chunk", 2))
        chunks = []
        for start in range(0, len(blocks), per):
            group = blocks[start : start + per]
            chunks.append(
                {
                    "text": "\n\n".join(b["text"] for b in group),
                    "context": None,
                    "block_ids": [b["id"] for b in group],
                    "metadata": {"fake": True},
                }
            )
        return httpx.Response(200, json={"chunks": chunks})
