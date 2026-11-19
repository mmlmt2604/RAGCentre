"""Use a running PaddleX basic-serving instance instead of loading PaddleOCR in this process.

    PADDLEX_SERVING_URL      e.g. http://paddlex:8080 (setting it turns remote mode on)
    PADDLEX_SERVING_API_KEY  optional; sent as ``Authorization: Bearer`` (PaddleX serving has no
                             authentication of its own, so this is for a reverse proxy in front)
    PADDLEX_SERVING_TIMEOUT  seconds to wait for one document (default 3600)

Start the server with a layout-parsing pipeline, for example::

    paddlex --serve --pipeline PP-StructureV3 --port 8080

The shim calls ``POST /layout-parsing`` and reduces ``result.layoutParsingResults[].prunedResult``
to the same plain pages ``convert.to_ir`` reads, so the IR is the one the in-process engine gives.

Differences from the in-process engine: the server's pipeline was built with its own feature set,
so a request can turn a feature off but not on; ``heading_levels`` has no effect, because the
server's JSON carries no ``title_level`` (every section title is level 2); and ``PADDLEOCR_SHIM_LANG``
is the server's business.
"""

from __future__ import annotations

import base64
import os
from typing import Any

import httpx
from shimkit import ShimError

from paddleocr_shim.engine import OPTION_ARGUMENTS

ENDPOINT = "/layout-parsing"

# shim option -> request field of PaddleX's ``/layout-parsing``
REQUEST_FIELDS = {
    "tables": "useTableRecognition",
    "formulas": "useFormulaRecognition",
    "charts": "useChartRecognition",
    "seals": "useSealRecognition",
    "orientation": "useDocOrientationClassify",
    "unwarp": "useDocUnwarping",
    "textline_orientation": "useTextlineOrientation",
    "region_detection": "useRegionDetection",
    "text_det_limit_side_len": "textDetLimitSideLen",
}
assert set(OPTION_ARGUMENTS) <= set(REQUEST_FIELDS)  # one source of truth for the option names


def reduce_pruned(pruned: dict[str, Any], position: int) -> dict[str, Any]:
    """One ``prunedResult`` -> ``{"page", "width", "height", "blocks"}`` (see ``engine.reduce_page``).

    ``position`` is the 1-based place in the result list, used when ``page_index`` is null."""
    page_index = pruned.get("page_index")
    blocks = [
        {
            "label": block.get("block_label"),
            "content": block.get("block_content"),
            "bbox": list(block["block_bbox"]) if block.get("block_bbox") is not None else None,
            "order": block.get("block_order"),
            "title_level": block.get("title_level"),
        }
        for block in pruned.get("parsing_res_list") or []
    ]
    return {
        "page": page_index + 1 if isinstance(page_index, int) else position,
        "width": pruned["width"],
        "height": pruned["height"],
        "blocks": blocks,
    }


class PaddleServingEngine:
    """``(content, filename, options) -> list of reduced pages``, like ``PaddleEngine``."""

    def __init__(
        self, url: str, api_key: str | None = None, timeout: float = 3600.0, client: httpx.Client | None = None
    ):
        self.url = url.rstrip("/")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.client = client or httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0), headers=headers)

    @classmethod
    def from_env(cls) -> PaddleServingEngine | None:
        url = os.environ.get("PADDLEX_SERVING_URL")
        if not url:
            return None
        key = os.environ.get("PADDLEX_SERVING_API_KEY") or None
        return cls(url, key, float(os.environ.get("PADDLEX_SERVING_TIMEOUT", 3600)))

    def __call__(self, content: bytes, filename: str, options: dict[str, bool]) -> list[dict[str, Any]]:
        is_pdf = filename.lower().endswith(".pdf")
        body: dict[str, Any] = {"file": base64.b64encode(content).decode("ascii"), "fileType": 0 if is_pdf else 1}
        body.update({field: options[name] for name, field in REQUEST_FIELDS.items() if options.get(name) is not None})
        try:
            resp = self.client.post(f"{self.url}{ENDPOINT}", json=body)
        except httpx.TimeoutException as exc:
            raise ShimError(504, "upstream_timeout", f"PaddleX serving did not answer in time: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ShimError(502, "upstream_unreachable", f"cannot reach PaddleX serving at {self.url}: {exc}") from exc
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        message = payload.get("errorMsg") if isinstance(payload, dict) else None
        message = str(message or resp.text[:300] or resp.reason_phrase)
        if resp.status_code in (401, 403):
            raise ShimError(502, "upstream_unauthorized", f"PaddleX serving rejected the request: {message}")
        if resp.status_code == 429 or resp.status_code >= 500:
            # PaddleX reports a file it cannot read as 422 and its own failures as 500.
            raise ShimError(503, "upstream_error", f"PaddleX serving returned {resp.status_code}: {message}")
        if resp.status_code >= 400:
            raise ShimError(
                422, "conversion_failed", f"{filename}: PaddleX serving returned {resp.status_code}: {message}"
            )
        results = (
            ((payload or {}).get("result") or {}).get("layoutParsingResults") if isinstance(payload, dict) else None
        )
        if not isinstance(results, list):
            raise ShimError(502, "upstream_invalid", "PaddleX serving did not return result.layoutParsingResults")
        return [reduce_pruned(item["prunedResult"], position) for position, item in enumerate(results, start=1)]
