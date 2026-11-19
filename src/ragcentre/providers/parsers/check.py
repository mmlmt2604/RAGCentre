"""Check that a parser service speaks ``ragcentre.parser/v1`` well enough to use (D1).

``ragcentre parser check <base_url>`` and ``POST /v1/providers/{id}/test?deep=true`` run this. It
depends on nothing but ``httpx`` and the IR validation, so it works for a service written in any
language and never imports an engine.

A finding is an ``error`` (RAGCentre cannot use the service as it is), a ``warn`` (it works, but a
named RAGCentre feature will not) or ``info``. Warnings say what is lost.
"""

from __future__ import annotations

import contextlib
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from ragcentre.models.ir import DocumentIR, IRError, normalise_ir
from ragcentre.providers.parsers.sample import EXPECTED_HEADINGS, EXPECTED_ORDER, HEADER, sample_pdf

PROTOCOL = "ragcentre.parser/v1"
Level = Literal["error", "warn", "info"]
Sleep = Callable[[float], None]

# How many times one request is re-sent when a connection is dropped mid-flight (the server restarted,
# or closed an idle keep-alive connection). The retry goes over a fresh connection: the client is
# closed first, so the pooled connection is not reused. ``retry_sleep`` is patched in tests.
REQUEST_RETRIES = 2
RETRY_BACKOFF = 0.25


def retry_sleep(seconds: float) -> None:
    time.sleep(seconds)


@dataclass
class Finding:
    level: Level
    check: str
    message: str
    lost: str | None = None  # the RAGCentre feature a warning costs

    def as_dict(self) -> dict[str, Any]:
        return {"level": self.level, "check": self.check, "message": self.message, "lost": self.lost}


@dataclass
class CheckReport:
    base_url: str
    findings: list[Finding] = field(default_factory=list)
    service: dict[str, Any] | None = None
    blocks: int = 0
    pages: int = 0
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return not any(f.level == "error" for f in self.findings)

    def add(self, level: Level, check: str, message: str, lost: str | None = None) -> None:
        self.findings.append(Finding(level, check, message, lost))

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "base_url": self.base_url,
            "service": self.service,
            "blocks": self.blocks,
            "pages": self.pages,
            "seconds": round(self.seconds, 2),
            "findings": [f.as_dict() for f in self.findings],
        }


class _Failed(Exception):
    """A check that ends the run (nothing more can be tested)."""


def check_parser(
    client: httpx.Client,
    base_url: str,
    *,
    file: tuple[str, bytes, str] | None = None,
    job_timeout: float = 600.0,
    probe_errors: bool = True,
    sleep: Sleep = time.sleep,
) -> CheckReport:
    """Check the service at ``base_url`` with ``client`` (which carries any credentials).

    ``file`` is ``(filename, content, mime_type)``; the default is a small built-in PDF whose
    structure is known, which also lets the reading order and headings be checked."""
    report = CheckReport(base_url=base_url.rstrip("/"))
    started = time.monotonic()
    try:
        info = _check_info(client, report)
        upload = file or ("pump-manual.pdf", sample_pdf(), "application/pdf")
        _check_mime(info, upload[2], report)
        ir = _check_parse(client, report, upload, info, job_timeout, sleep)
        _check_ir(report, ir, builtin=file is None)
        if probe_errors:
            _check_error_handling(client, report)
    except _Failed:
        pass
    report.seconds = time.monotonic() - started
    return report


# --- /info ------------------------------------------------------------------------------------


def _retryable(exc: httpx.HTTPError) -> bool:
    """A connection failure where resending on a fresh connection can only help."""
    if isinstance(exc, (httpx.ReadError, httpx.RemoteProtocolError)):
        return True
    text = str(exc).lower()
    return ("connection" in text and ("reset" in text or "closed" in text or "broken" in text)) or ("eof" in text)


def _fresh_client(client: httpx.Client) -> httpx.Client:
    """A new client with the same settings but an empty connection pool, so a retry dials afresh.

    ``httpx.Client`` cannot be reused after ``close()``, so a closed pooled connection is replaced by
    an ephemeral client instead. The caller's client stays open. ``base_url`` is dropped because the
    checker always requests absolute URLs.
    """
    return httpx.Client(
        headers=client.headers,
        timeout=client.timeout,
        transport=getattr(client, "_transport", None),  # httpx keeps the transport private
    )


def _send(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    auth: bool = True,
    retries: int = REQUEST_RETRIES,
    **kwargs: Any,
) -> httpx.Response:
    """Send one request, retrying a dropped connection on a fresh one.

    The checker sends several requests in a row over one client (and one connection pool). A server
    that stops an idle keep-alive connection after the first request makes the next request die with
    a connection reset; the retry goes over a new client, so the connection is dialed again.
    """
    sender = client
    attempt = 0
    while True:
        try:
            request = sender.build_request(method, url, **kwargs)
            if not auth:
                request.headers.pop("authorization", None)
            return sender.send(request)
        except httpx.HTTPError as exc:
            if attempt >= retries or not _retryable(exc):
                raise
            attempt += 1
            # An idle connection was dropped: abandon the pool and dial again on a fresh client.
            sender.close()
            sender = _fresh_client(client)
            retry_sleep(RETRY_BACKOFF * attempt)


def _check_info(client: httpx.Client, report: CheckReport) -> dict[str, Any]:
    url = f"{report.base_url}/info"
    try:
        resp = _send(client, "GET", url)
    except httpx.HTTPError as exc:
        report.add("error", "info", f"cannot reach {url}: {exc}")
        raise _Failed from None
    if resp.status_code in (401, 403):
        report.add("error", "info", f"GET /info returned {resp.status_code}: the credentials were rejected")
        raise _Failed
    if resp.status_code != 200:
        report.add("error", "info", f"GET /info returned {resp.status_code}; expected 200")
        raise _Failed
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        report.add("error", "info", "GET /info must return a JSON object")
        raise _Failed
    if body.get("protocol") != PROTOCOL:
        report.add("error", "info", f"protocol is {body.get('protocol')!r}; it must be exactly {PROTOCOL!r}")
    for key in ("name", "version"):
        if not isinstance(body.get(key), str) or not body[key]:
            report.add("error", "info", f"{key!r} must be a non-empty string")
    if not report.ok:
        raise _Failed
    report.service = {"name": body["name"], "version": body["version"]}
    caps = body.get("capabilities")
    if caps is not None and not isinstance(caps, dict):
        report.add("warn", "info", "'capabilities' should be an object; it is ignored")
    mime_types = body.get("mime_types")
    if mime_types is None:
        report.add("info", "info", "no 'mime_types': the UI cannot warn about routes the service will never match")
    elif not isinstance(mime_types, list) or not all(isinstance(m, str) for m in mime_types):
        report.add("warn", "info", "'mime_types' should be a list of strings")
    if _async(body):
        report.add("info", "info", "capabilities.async is on: documents are parsed as jobs, with no request time limit")
    # RAGCentre reads /info without credentials when it creates the provider.
    try:
        bare = _send(client, "GET", url, auth=False)
    except httpx.HTTPError:
        bare = None
    if bare is not None and bare.status_code in (401, 403) and "authorization" in client.headers:
        report.add(
            "error",
            "info",
            f"GET /info needs credentials ({bare.status_code}), but RAGCentre reads it without them when it "
            "creates the provider, so the provider cannot be registered. Leave /info open.",
        )
    report.add("info", "info", f"{body['name']} {body['version']}")
    return body


def _async(info: dict[str, Any]) -> bool:
    caps = info.get("capabilities")
    return isinstance(caps, dict) and caps.get("async") is True


def _check_mime(info: dict[str, Any], mime: str, report: CheckReport) -> None:
    declared = info.get("mime_types")
    if not isinstance(declared, list) or not declared:
        return
    if not any(_mime_match(str(pattern), mime) for pattern in declared):
        report.add("warn", "info", f"'mime_types' does not list {mime}, which the check is about to send")


def _mime_match(pattern: str, mime: str) -> bool:
    if pattern in ("*", "*/*") or pattern == mime:
        return True
    return pattern.endswith("/*") and mime.startswith(pattern[:-1])


# --- /parse -----------------------------------------------------------------------------------


def _check_parse(
    client: httpx.Client,
    report: CheckReport,
    upload: tuple[str, bytes, str],
    info: dict[str, Any],
    job_timeout: float,
    sleep: Sleep,
) -> DocumentIR:
    name, content, mime = upload
    form: dict[str, Any] = {"files": {"file": (name, content, mime)}, "data": {"options": "{}"}}
    use_async = _async(info)
    try:
        resp = _send(
            client, "POST", f"{report.base_url}/parse", params={"mode": "async"} if use_async else None, **form
        )
        if use_async and resp.status_code == 202:
            raw = _wait_for_job(client, report, resp, job_timeout, sleep)
        else:
            raw = _read_ir_response(report, resp, "POST /parse")
    except httpx.TimeoutException:
        report.add(
            "error", "parse", "POST /parse timed out. A slow engine should offer async jobs (capabilities.async)."
        )
        raise _Failed from None
    except httpx.HTTPError as exc:
        report.add("error", "parse", f"POST /parse failed: {exc}")
        raise _Failed from None
    try:
        ir = normalise_ir(raw)
    except IRError as exc:
        report.add("error", "ir", f"the Document IR is invalid: {exc}")
        raise _Failed from None
    report.blocks, report.pages = len(ir.blocks), len(ir.pages)
    report.add("info", "ir", f"{len(ir.blocks)} blocks on {len(ir.pages) or 'no declared'} pages")
    return ir


def _read_ir_response(report: CheckReport, resp: httpx.Response, what: str) -> Any:
    if resp.status_code != 200:
        report.add("error", "parse", f"{what} returned {resp.status_code}: {_error_text(resp)}")
        raise _Failed
    try:
        return resp.json()
    except ValueError:
        report.add("error", "parse", f"{what} did not return JSON")
        raise _Failed from None


def _wait_for_job(
    client: httpx.Client, report: CheckReport, submitted: httpx.Response, timeout: float, sleep: Sleep
) -> Any:
    try:
        body = submitted.json()
        job_id, interval = body["job_id"], float(body.get("poll_after_seconds", 2))
    except (ValueError, KeyError, TypeError):
        report.add("error", "async", 'POST /parse?mode=async must answer 202 with {"job_id", "poll_after_seconds"}')
        raise _Failed from None
    deadline = time.monotonic() + timeout
    while True:
        sleep(min(max(interval, 0.2), 30.0))
        try:
            status = _send(client, "GET", f"{report.base_url}/jobs/{job_id}")
        except httpx.HTTPError as exc:
            report.add("error", "async", f"GET /jobs/{{id}} failed: {exc}")
            raise _Failed from None
        if status.status_code != 200:
            report.add("error", "async", f"GET /jobs/{{id}} returned {status.status_code}: {_error_text(status)}")
            raise _Failed
        state = (
            status.json().get("status")
            if status.headers.get("content-type", "").startswith("application/json")
            else None
        )
        if state == "succeeded":
            try:
                result = _send(client, "GET", f"{report.base_url}/jobs/{job_id}/result")
            except httpx.HTTPError as exc:
                report.add("error", "async", f"GET /jobs/{{id}}/result failed: {exc}")
                raise _Failed from None
            raw = _read_ir_response(report, result, "GET /jobs/{id}/result")
            with contextlib.suppress(httpx.HTTPError):
                _send(client, "DELETE", f"{report.base_url}/jobs/{job_id}")  # optional; ignore the answer
            return raw
        if state == "failed":
            report.add("error", "async", f"the job failed: {status.json().get('error')}")
            raise _Failed
        if state != "running":
            report.add("error", "async", f"job status must be running, succeeded or failed, got {state!r}")
            raise _Failed
        if time.monotonic() > deadline:
            report.add("error", "async", f"the job did not finish within {timeout:g} s")
            raise _Failed
        interval = min(interval * 1.5, 30.0)


def _error_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:200]
    except ValueError:
        pass
    return resp.text[:200] or resp.reason_phrase


# --- the IR ------------------------------------------------------------------------------------

_FOOTER_TEXT = re.compile(r"^(page\s+)?\d+(\s*(/|of)\s*\d+)?$", re.IGNORECASE)


def _check_ir(report: CheckReport, ir: DocumentIR, *, builtin: bool) -> None:
    blocks = ir.blocks
    if not blocks:
        report.add("error", "ir", "the document has no blocks")
        return
    headings = [b for b in blocks if b.type == "heading"]
    if not headings:
        report.add(
            "warn",
            "headings",
            "no heading blocks",
            "heading paths in chunk context and the structure chunker (chunks will not know their section)",
        )
    elif len(headings) >= 3 and len({b.level for b in headings}) == 1:
        report.add(
            "warn",
            "headings",
            f"all {len(headings)} headings are level {headings[0].level}",
            "nested heading paths and the structure chunker's section boundaries (the document looks flat)",
        )
    if not any(b.page is not None for b in blocks):
        report.add(
            "warn",
            "pages",
            "no block has a page number",
            "the page chunker, page_start/page_end on chunks, and evaluation targets that name pages",
        )
    tables = [b for b in blocks if b.type == "table"]
    if tables and not any("|" in b.text for b in tables):
        report.add(
            "warn",
            "tables",
            "table blocks have no Markdown pipe table in 'text'",
            "table-aware splitting (the header row repeated in each chunk) and readable table text in answers",
        )
    _check_repeated_furniture(report, ir)
    _check_boxes(report, ir)
    if builtin:
        _check_sample(report, ir)


def _check_repeated_furniture(report: CheckReport, ir: DocumentIR) -> None:
    by_page: dict[int, list[str]] = {}
    for block in ir.blocks:
        if block.page is not None and block.type in ("paragraph", "heading"):
            by_page.setdefault(block.page, []).append(" ".join(block.text.split()))
    if len(by_page) < 2 or any(b.type in ("page_header", "page_footer") for b in ir.blocks):
        return
    counts = Counter(text for texts in by_page.values() for text in set(texts))
    repeated = [t for t, n in counts.items() if n == len(by_page) and len(t) > 3 and not _FOOTER_TEXT.match(t)]
    if repeated:
        report.add(
            "warn",
            "header_footer",
            f"the text {repeated[0]!r} repeats on every page but is not labelled page_header or page_footer",
            "header and footer removal (it is indexed on every page and crowds out real matches)",
        )


def _check_boxes(report: CheckReport, ir: DocumentIR) -> None:
    sizes = {p.page: (p.width, p.height) for p in ir.pages if p.width and p.height}
    for block in ir.blocks:
        if block.bbox is None or block.page not in sizes:
            continue
        width, height = sizes[block.page]
        x0, y0, x1, y1 = block.bbox
        if x0 > x1 or y0 > y1 or x1 > width * 1.05 or y1 > height * 1.05 or min(x0, y0) < -0.05 * max(width, height):
            report.add(
                "warn",
                "bbox",
                f"block {block.id} has a box {block.bbox} outside its {width:g} x {height:g} page "
                "(boxes are [x0, y0, x1, y1] from the top-left, in the page's units)",
                "box-based features (none ship today; previews would be misplaced)",
            )
            return


def _is_furniture(text: str) -> bool:
    return HEADER in text or bool(_FOOTER_TEXT.match(text.strip()))


def _check_sample(report: CheckReport, ir: DocumentIR) -> None:
    body = [b for b in ir.blocks if not _is_furniture(b.text) and b.type not in ("page_header", "page_footer")]
    position = 0
    for needle in EXPECTED_ORDER:
        found = next((i for i in range(position, len(body)) if needle.lower() in body[i].text.lower()), None)
        if found is None:
            anywhere = any(needle.lower() in b.text.lower() for b in body)
            report.add(
                "warn",
                "reading_order" if anywhere else "content",
                f"{needle!r} is {'out of reading order' if anywhere else 'missing'} in the sample's output",
                "chunks that follow the document's reading order" if anywhere else "the text itself",
            )
            return
        position = found  # a table block holds several of the expected texts
    headings = {b.text.strip() for b in ir.blocks if b.type == "heading"}
    missing = [h for h in EXPECTED_HEADINGS if not any(h in text for text in headings)]
    if missing:
        report.add("warn", "headings", f"the sample's headings were not recognised as headings: {', '.join(missing)}",
                   "heading paths and the structure chunker")  # fmt: skip
    pages = {b.page for b in ir.blocks if b.page is not None}
    if pages and pages != {1, 2}:
        report.add("warn", "pages", f"the sample has pages 1 and 2; the blocks report pages {sorted(pages)}",
                   "page numbers on chunks")  # fmt: skip
    if not any(b.type == "table" for b in ir.blocks):
        report.add("warn", "tables", "the sample's ruled table was not returned as a table block",
                   "table-aware splitting")  # fmt: skip
    if not any(b.type == "list_item" for b in ir.blocks):
        report.add("info", "lists", "no list_item blocks: the sample's bullets came back as another type")


# --- errors ------------------------------------------------------------------------------------


def _check_error_handling(client: httpx.Client, report: CheckReport) -> None:
    """A file the service cannot read must be a 4xx (permanent) with ``{"error": {...}}``; a 5xx is
    retried by RAGCentre."""
    form: dict[str, Any] = {"files": {"file": ("unreadable.xyz", b"\x00\x01 not a document", "application/x-unknown")},
            "data": {"options": "{}"}}  # fmt: skip
    try:
        resp = _send(client, "POST", f"{report.base_url}/parse", **form)
    except httpx.TimeoutException:
        report.add("warn", "errors", "an unreadable file made the service hang instead of answering with an error")
        return
    except httpx.HTTPError as exc:
        report.add("warn", "errors", f"an unreadable file broke the connection: {exc}")
        return
    if 200 <= resp.status_code < 300 or resp.status_code == 202:
        report.add("info", "errors", f"the service accepts an unknown file type ({resp.status_code})")
        return
    if resp.status_code >= 500:
        report.add(
            "warn",
            "errors",
            f"an unreadable file gets {resp.status_code}, which RAGCentre retries with backoff",
            "fast failure of files the service can never parse (use 415 or 422 for those)",
        )
        return
    try:
        body = resp.json()
        well_formed = isinstance(body, dict) and isinstance(body.get("error"), dict)
    except ValueError:
        well_formed = False
    if not well_formed:
        report.add("warn", "errors", f"the {resp.status_code} for an unreadable file has no {{\"error\": {{\"code\", \"message\"}}}} body",
                   "a readable failure message in the document's run history")  # fmt: skip
    else:
        report.add("info", "errors", f"an unreadable file gets {resp.status_code} with an error body, as it should")
