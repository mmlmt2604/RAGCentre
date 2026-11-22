"""Use an existing docling-serve instead of running Docling in this process.

    DOCLING_SERVE_URL        e.g. http://docling-serve:5001 (setting it turns remote mode on)
    DOCLING_SERVE_API_KEY    sent as ``X-Api-Key`` when the server requires one
    DOCLING_SERVE_TIMEOUT    seconds to wait for one conversion (default 3600)

The shim posts the file to ``/v1/convert/file`` asking for the ``json`` format, which carries the
full ``DoclingDocument``, and maps that with the same ``to_ir`` as the in-process engine. Only
``docling-core`` (the document model) is needed here, not Docling and its models.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from docling_core.types.doc import DoclingDocument
from shimkit import ShimError

# shim option -> docling-serve form field (names from docling.datamodel.service.options). Booleans
# are sent as "true" / "false"; a list becomes a repeated field.
FORM_FIELDS = {
    "ocr": "do_ocr",
    "include_page_images": "include_page_images",
    "heading_hierarchy": "do_pdf_heading_hierarchy",
    "ocr_engine": "ocr_engine",
    "ocr_lang": "ocr_lang",
    "force_full_page_ocr": "force_ocr",
    "table_mode": "table_mode",
    "code_enrichment": "do_code_enrichment",
    "formula_enrichment": "do_formula_enrichment",
    "picture_classification": "do_picture_classification",
    "picture_description": "do_picture_description",
}


def form_fields(options: dict[str, Any]) -> dict[str, Any]:
    """The docling-serve form fields for the shim's validated ``options``. Unset options are left
    to the server's defaults. ``max_pages`` becomes the end of a ``page_range`` (docling-serve has
    no page limit of its own)."""
    fields: dict[str, Any] = {"to_formats": "json", "image_export_mode": "placeholder"}
    for name, field in FORM_FIELDS.items():
        value = options.get(name)
        if value is None or value == []:
            continue
        fields[field] = str(value).lower() if isinstance(value, bool) else value
    first, last = options.get("page_range") or (None, None)
    if options.get("max_pages") is not None:
        first = first or 1
        last = min(last, first + options["max_pages"] - 1) if last else first + options["max_pages"] - 1
    if first is not None and last is not None:
        fields["page_range"] = [str(first), str(last)]
    return fields


class DoclingServeConverter:
    """A converter (see ``app.Converter``) that calls docling-serve."""

    def __init__(
        self,
        url: str,
        api_key: str | None = None,
        timeout: float = 3600.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        headers = {"X-Api-Key": api_key} if api_key else {}
        self.reachable = True
        self._last_version: str | None = None
        self.client = client or httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0), headers=headers)

    @classmethod
    def from_env(cls) -> DoclingServeConverter | None:
        url = os.environ.get("DOCLING_SERVE_URL")
        if not url:
            return None
        return cls(
            url, os.environ.get("DOCLING_SERVE_API_KEY") or None, float(os.environ.get("DOCLING_SERVE_TIMEOUT", 3600))
        )

    def server_version(self) -> str:
        """The server's Docling version, or ``remote`` when it does not say.

        While the server is down this is the last version it reported, with ``reachable`` false:
        an outage is not a version change. The timeouts are shorter than RAGCentre's for ``/info``."""
        try:
            resp = self.client.get(f"{self.url}/version", timeout=httpx.Timeout(1.5, connect=1.0))
            self.reachable = resp.status_code < 500
            if not self.reachable:
                return self._last_version or "remote"
            body = resp.json()
            found = body.get("docling") or body.get("docling-serve") if isinstance(body, dict) else None
            if isinstance(found, dict):
                found = found.get("version")
            if found:
                self._last_version = str(found)
            return self._last_version or "remote"
        except (httpx.ConnectError, httpx.ConnectTimeout):
            self.reachable = False
            return self._last_version or "remote"
        except (httpx.HTTPError, ValueError):  # slow (a server at work) or not JSON: not an outage
            return self._last_version or "remote"

    def __call__(self, content: bytes, filename: str, options: dict[str, Any]) -> DoclingDocument:
        try:
            resp = self.client.post(
                f"{self.url}/v1/convert/file",
                files={"files": (filename, content, "application/octet-stream")},
                data=form_fields(options),
            )
        except httpx.TimeoutException as exc:
            raise ShimError(504, "upstream_timeout", f"docling-serve did not answer in time: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ShimError(502, "upstream_unreachable", f"cannot reach docling-serve at {self.url}: {exc}") from exc
        if resp.status_code in (401, 403):
            raise ShimError(502, "upstream_unauthorized", "docling-serve rejected DOCLING_SERVE_API_KEY")
        if resp.status_code == 415:
            raise ShimError(415, "unsupported_media_type", _detail(resp))
        if resp.status_code >= 500 or resp.status_code == 429:
            raise ShimError(503, "upstream_error", f"docling-serve returned {resp.status_code}: {_detail(resp)}")
        if resp.status_code >= 400:
            raise ShimError(
                422, "conversion_failed", f"{filename}: docling-serve returned {resp.status_code}: {_detail(resp)}"
            )
        body = resp.json()
        document = body.get("document") if isinstance(body, dict) else None
        status = str(body.get("status", "")) if isinstance(body, dict) else ""
        if not isinstance(document, dict) or not document.get("json_content"):
            errors = body.get("errors") if isinstance(body, dict) else None
            raise ShimError(
                422,
                "conversion_failed",
                f"{filename}: docling-serve returned {status or 'no document'}: {errors or ''}",
            )
        return DoclingDocument.model_validate(document["json_content"])


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300]
    detail = body.get("detail", body) if isinstance(body, dict) else body
    return str(detail)[:300]
