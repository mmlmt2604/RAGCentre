"""The OCR probe: a page that is only pixels, whose text has to come back.

``GET /info`` cannot prove OCR (shims load their engine on the first parse), and a PDF with a text
layer cannot either (parsers read it without OCR). This page can.
"""

from __future__ import annotations

import logging
import struct
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from typing import Literal

from ragcentre.context import HttpClientFactory
from ragcentre.errors import ExecutionError
from ragcentre.logs import redact
from ragcentre.models.ir import IRError, normalise_ir
from ragcentre.providers.base import BuildEnv
from ragcentre.providers.parsers.http import HttpParser, HttpParserConfig

log = logging.getLogger("ragcentre.ocr")

CODE = "4827"
WORDS = ("ragcentre", "scanned", "pump", "bolts", "valve", "pressure")
MIN_WORDS = 3  # an engine may misread a word; it cannot guess three and the number

REQUEST_TIMEOUT = 300.0
POLL_SECONDS = 1.0
JOB_TIMEOUT = 900.0  # a first parse may download models

ProbeCode = Literal[
    "read", "no_text", "needs_key", "engine_unavailable", "unreachable", "busy", "rejected", "no_ocr", "error"
]


@dataclass(frozen=True)
class ProbeResult:
    code: ProbeCode
    seconds: float = 0.0
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.code == "read"


@cache
def page_png() -> bytes:
    return files("ragcentre.ocr").joinpath("page.png").read_bytes()


@cache
def page_pdf() -> bytes:
    """The PNG as a one-page PDF with no fonts and no text operators.

    PDF's FlateDecode with a PNG predictor reads a PNG's IDAT stream as it is, so this needs no
    imaging library."""
    png = page_png()
    width, height = struct.unpack(">II", png[16:24])
    data, at = b"", 8
    while at < len(png):
        length = int.from_bytes(png[at : at + 4], "big")
        if png[at + 4 : at + 8] == b"IDAT":
            data += png[at + 8 : at + 8 + length]
        at += length + 12
    page_w, page_h = width * 72 / 150, height * 72 / 150  # the page was drawn at 150 dpi
    draw = b"q %f 0 0 %f 0 0 cm /Im0 Do Q" % (page_w, page_h)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %f %f] /Resources << /XObject << /Im0 5 0 R >> >> "
        b"/Contents 4 0 R >>" % (page_w, page_h),
        b"<< /Length %d >>\nstream\n" % len(draw) + draw + b"\nendstream",
        b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceGray /BitsPerComponent 8 "
        b"/Filter /FlateDecode /DecodeParms << /Predictor 15 /Colors 1 /BitsPerComponent 8 /Columns %d >> "
        b"/Length %d >>\nstream\n" % (width, height, width, len(data)) + data + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def probe_formats(mime_types: Sequence[str]) -> list[str]:
    """The probe formats a shim accepts, PDF first. Empty when it reads neither PDFs nor images."""
    wanted = ("application/pdf", "image/png")
    if not mime_types or any(m in ("*", "*/*") for m in mime_types):
        return list(wanted)
    return [w for w in wanted if w in mime_types or f"{w.split('/')[0]}/*" in mime_types]


def text_was_read(text: str) -> bool:
    lowered = text.lower()
    return CODE in lowered and sum(word in lowered for word in WORDS) >= MIN_WORDS


def classify(exc: ExecutionError) -> ProbeCode:
    status, code = exc.upstream_status, exc.upstream_code
    if status in (401, 403):
        return "needs_key"
    if status in (408, 429):
        return "busy"
    if status is not None:
        return "engine_unavailable" if status >= 500 else "rejected"
    return "unreachable" if code == "unreachable" else "error"


def run_probe(
    config: HttpParserConfig,
    api_key: str | None,
    mime_types: Sequence[str],
    http: HttpClientFactory,
) -> ProbeResult:
    """Parse the page through the same client ingestion uses, with the shim's default options."""
    formats = probe_formats(mime_types)
    if not formats:
        return ProbeResult("no_ocr")
    limited = config.model_copy(
        update={
            "timeout_seconds": min(config.timeout_seconds, REQUEST_TIMEOUT),
            "job_timeout_seconds": min(config.job_timeout_seconds, JOB_TIMEOUT),
        }
    )
    # Its own semaphore, so that a probe does not take one of the provider's ingestion slots.
    env = BuildEnv(
        provider_id="ocr-check",
        provider_name="OCR check",
        http=http,
        data_dir=None,
        semaphore=threading.BoundedSemaphore(2),
    )
    parser = HttpParser(limited, {"api_key": api_key} if api_key else {}, env)
    parser.max_poll_seconds = POLL_SECONDS
    try:
        for mime in formats:
            content, name = (page_pdf(), "check.pdf") if mime == "application/pdf" else (page_png(), "check.png")
            try:
                ir = normalise_ir(parser.parse(content, name, mime, {}))
            except ExecutionError as exc:
                if exc.upstream_status == 415:
                    continue
                return ProbeResult(classify(exc), detail=redact(exc.message)[:500])
            except IRError as exc:
                return ProbeResult("error", detail=redact(str(exc))[:500])
            text = " ".join(block.text for block in ir.blocks)
            return ProbeResult("read" if text_was_read(text) else "no_text")
        return ProbeResult("no_ocr")
    except Exception as exc:
        log.exception("OCR probe failed")
        return ProbeResult("error", detail=redact(f"{type(exc).__name__}: {exc}")[:500])
    finally:
        parser.close()
