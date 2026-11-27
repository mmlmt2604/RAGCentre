"""Measurements and checks on a parse result, always against the fixture's ground truth.

Two layers. The measurement functions (``tokenize``, ``recall``, ``iou``, ``heading_levels``, ...) take a
Document IR dict and return numbers. The check builders (``recall_at_least``, ``recall_pair``, ...) wrap
them in a ``Check`` that a ``Probe`` carries. A check reads the truth, never the engine's own output, so it
cannot pass because the option did nothing.

Recall counts a truth token as found when a result token has a ``difflib`` ratio of at least 0.8 with it.
A result token satisfies one truth token only, so a single "the" cannot cover every "the" on the page.
Exact text equality is never a predicate.
"""

from __future__ import annotations

import base64
import binascii
import difflib
import math
import re
import struct
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from probe import Check, FixtureTruth, Observation, Verdict

RATIO = 0.8

_CJK = "㐀-䶿一-鿿豈-﫿"
_TOKEN = re.compile(rf"[{_CJK}]|[^\W_{_CJK}]+")


# --- tokens and recall --------------------------------------------------------------------------


def tokenize(text: str) -> list[str]:
    """Lower-cased word tokens. Punctuation is dropped. Each CJK character is one token."""
    return _TOKEN.findall(unicodedata.normalize("NFC", text).lower())


def recall(truth_tokens: Sequence[str], result_tokens: Iterable[str], threshold: float = RATIO) -> float:
    """Share of ``truth_tokens`` matched one to one by a result token with ``difflib`` ratio >= ``threshold``."""
    if not truth_tokens:
        return 1.0
    pool = Counter(result_tokens)
    matched = 0
    unmatched: list[str] = []
    for token in truth_tokens:  # exact matches first, so a fuzzy match never steals one
        if pool[token] > 0:
            pool[token] -= 1
            matched += 1
        else:
            unmatched.append(token)
    matcher = difflib.SequenceMatcher(None, autojunk=False)
    for token in unmatched:
        matcher.set_seq2(token)
        best_ratio, best = 0.0, None
        for candidate, count in pool.items():
            if count <= 0:
                continue
            matcher.set_seq1(candidate)
            if matcher.real_quick_ratio() < threshold or matcher.quick_ratio() < threshold:
                continue
            ratio = matcher.ratio()
            if ratio >= threshold and ratio > best_ratio:
                best_ratio, best = ratio, candidate
        if best is not None:
            pool[best] -= 1
            matched += 1
    return matched / len(truth_tokens)


def blocks_of(ir: Any) -> list[dict[str, Any]]:
    blocks = ir.get("blocks") if isinstance(ir, Mapping) else None
    return [b for b in blocks if isinstance(b, dict)] if isinstance(blocks, list) else []


def result_tokens(ir: Any, pages: set[int] | None = None, types: set[str] | None = None) -> list[str]:
    """Tokens of the blocks' text, optionally only blocks on ``pages`` or of ``types``."""
    out: list[str] = []
    for block in blocks_of(ir):
        if pages is not None and block.get("page") not in pages:
            continue
        if types is not None and block.get("type") not in types:
            continue
        out.extend(tokenize(str(block.get("text", ""))))
    return out


def recall_of(ir: Any, truth: FixtureTruth, pages: set[int] | None = None) -> float:
    """Recall of the truth tokens on ``pages`` (all pages when ``None``) in the result's blocks.

    With ``pages`` the result is read on the same pages, so a window that was ignored shows as extra text."""
    return recall(truth.tokens(pages), result_tokens(ir, pages))


# --- boxes --------------------------------------------------------------------------------------

Box = Sequence[float]


def iou(a: Box, b: Box) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def page_dims(ir: Any) -> dict[int, tuple[float, float]]:
    """``{page: (width, height)}`` for pages that declare both."""
    dims: dict[int, tuple[float, float]] = {}
    pages = ir.get("pages") if isinstance(ir, Mapping) else None
    for page in pages if isinstance(pages, list) else []:
        if isinstance(page, dict) and _number(page.get("width")) and _number(page.get("height")):
            dims[int(page.get("page", 0))] = (float(page["width"]), float(page["height"]))
    return dims


def line_box_iou(ir: Any, truth: FixtureTruth, min_iou: float = 0.5) -> tuple[float, float]:
    """``(share of truth lines whose best result block has IoU >= min_iou, mean best IoU)``.

    Truth boxes are scaled to the result's page size when the result declares one, so a PDF parsed
    at another resolution is compared fairly."""
    dims = page_dims(ir)
    best: list[float] = []
    for page in truth.pages:
        sx = sy = 1.0
        if page.page in dims and page.width > 0 and page.height > 0:
            sx, sy = dims[page.page][0] / page.width, dims[page.page][1] / page.height
        boxes = [
            b["bbox"]
            for b in blocks_of(ir)
            if isinstance(b.get("bbox"), list) and len(b["bbox"]) == 4 and (b.get("page") or 1) == page.page
        ]
        for line in page.lines:
            scaled = (line.bbox[0] * sx, line.bbox[1] * sy, line.bbox[2] * sx, line.bbox[3] * sy)
            best.append(max((iou(scaled, b) for b in boxes), default=0.0))
    if not best:
        return 0.0, 0.0
    return sum(v >= min_iou for v in best) / len(best), sum(best) / len(best)


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def bbox_problems(ir: Any, tolerance: float = 0.01) -> tuple[int, list[str]]:
    """``(blocks checked, problems)``. A box must be ``x0 <= x1``, ``y0 <= y1`` and inside its page, give or
    take ``tolerance`` of the page size. A block whose page declares no size is not checked."""
    dims = page_dims(ir)
    checked = 0
    problems: list[str] = []
    for index, block in enumerate(blocks_of(ir), start=1):
        box = block.get("bbox")
        if box is None:
            continue
        label = f"block {block.get('id', index)}"
        if not (isinstance(box, list) and len(box) == 4 and all(_number(v) for v in box)):
            problems.append(f"{label}: bbox is not four finite numbers")
            continue
        if box[0] > box[2] or box[1] > box[3]:
            problems.append(f"{label}: bbox {box} is not [x0, y0, x1, y1] with x0 <= x1 and y0 <= y1")
            continue
        size = dims.get(block.get("page") or 1)
        if size is None:
            continue
        checked += 1
        slack_x, slack_y = size[0] * tolerance, size[1] * tolerance
        if box[0] < -slack_x or box[1] < -slack_y or box[2] > size[0] + slack_x or box[3] > size[1] + slack_y:
            problems.append(f"{label}: bbox {box} is outside the {size[0]:g} x {size[1]:g} page")
    return checked, problems


# --- headings, confidence, pages ----------------------------------------------------------------


def heading_levels(ir: Any) -> list[int]:
    return [b["level"] for b in blocks_of(ir) if b.get("type") == "heading" and isinstance(b.get("level"), int)]


def confidences(ir: Any) -> list[Any]:
    """Every ``metadata.confidence`` present, in block order (any type: validity is checked elsewhere)."""
    out: list[Any] = []
    for block in blocks_of(ir):
        meta = block.get("metadata")
        if isinstance(meta, dict) and "confidence" in meta:
            out.append(meta["confidence"])
    return out


def confidence_problems(ir: Any) -> list[str]:
    return [
        f"confidence {c!r} is not a number from 0 to 1" for c in confidences(ir) if not (_number(c) and 0 <= c <= 1)
    ]


def block_pages(ir: Any) -> set[int]:
    return {b["page"] for b in blocks_of(ir) if isinstance(b.get("page"), int)}


def declared_pages(ir: Any) -> set[int]:
    pages = ir.get("pages") if isinstance(ir, Mapping) else None
    return (
        {p["page"] for p in pages if isinstance(p, dict) and isinstance(p.get("page"), int)}
        if isinstance(pages, list)
        else set()
    )


def png_size(data: bytes) -> tuple[int, int]:
    """Width and height from a PNG's header. Raises ``ValueError`` when ``data`` is not a PNG."""
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ValueError("not a PNG")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def page_images(ir: Any) -> dict[int, tuple[int, int]]:
    """``{page: (image width, image height)}`` for every ``pages[].image``. Raises ``ValueError`` on a bad image."""
    out: dict[int, tuple[int, int]] = {}
    pages = ir.get("pages") if isinstance(ir, Mapping) else None
    for page in pages if isinstance(pages, list) else []:
        image = page.get("image") if isinstance(page, dict) else None
        if not image:
            continue
        try:
            out[page["page"]] = png_size(base64.b64decode(image["data_b64"], validate=True))
        except (KeyError, TypeError, ValueError, binascii.Error) as exc:
            raise ValueError(f"page {page.get('page')}: image is not a PNG in data_b64 ({exc})") from None
    return out


def page_image_problems(ir: Any, exact: bool = False, tolerance: float = 0.02) -> list[str]:
    """Problems with the decoded page images against the page ``width`` and ``height``.

    The aspect ratio must match within ``tolerance``. With ``exact`` the width must equal ``width``, which
    is how a shim whose ``bbox`` is in image pixels says so."""
    try:
        images = page_images(ir)
    except ValueError as exc:
        return [str(exc)]
    dims = page_dims(ir)
    problems = []
    for page, (iw, ih) in images.items():
        if page not in dims:
            problems.append(f"page {page}: has an image but no width and height")
            continue
        pw, ph = dims[page]
        if iw <= 0 or ih <= 0 or abs(iw / ih - pw / ph) > tolerance * pw / ph:
            problems.append(f"page {page}: image {iw}x{ih} does not match the {pw:g}x{ph:g} page")
        elif exact and (abs(iw - pw) > 1 or abs(ih - ph) > 1):
            problems.append(f"page {page}: image {iw}x{ih} is not the page size {pw:g}x{ph:g}")
    return problems


# --- IR validity --------------------------------------------------------------------------------


def ir_problems(ir: Any) -> list[str]:
    """Why ``ir`` is not a usable Document IR, as the host sees it. Empty when it is.

    Runs ``ragcentre``'s ``normalise_ir``, then checks what it leaves open: unique block ids, ``page``
    inside the declared pages, ``bbox`` inside its page, and ``confidence`` between 0 and 1."""
    from ragcentre.models.ir import IRError, normalise_ir

    try:
        doc = normalise_ir(ir)
    except IRError as exc:
        return [f"normalise_ir: {exc}"]
    problems: list[str] = []
    ids = [b.id for b in doc.blocks]
    if len(set(ids)) != len(ids):
        problems.append("block ids are not unique")
    declared = {p.page for p in doc.pages}
    count = doc.metadata.get("page_count")
    if declared and count is not None and count != len(declared):
        problems.append(f"metadata.page_count {count} but {len(declared)} pages")
    for block in doc.blocks:
        if block.page is not None and declared and block.page not in declared:
            problems.append(f"block {block.id}: page {block.page} is not one of the pages {sorted(declared)}")
        if block.page is not None and not declared and isinstance(count, int) and block.page > count:
            problems.append(f"block {block.id}: page {block.page} is past page_count {count}")
    problems.extend(bbox_problems(ir)[1])
    problems.extend(confidence_problems(ir))
    return problems


# --- checks (what a Probe carries) --------------------------------------------------------------


def _blocks_count(ir: Any) -> int:
    return len(blocks_of(ir))


def ir_valid() -> Check:
    def fn(obs: Observation) -> Verdict:
        problems = ir_problems(obs.run.body)
        return Verdict(not problems, "; ".join(problems[:3]), {"blocks": _blocks_count(obs.run.body)})

    return Check("ir_valid", "structure", fn)


def recall_at_least(minimum: float, pages: set[int] | None = None) -> Check:
    def fn(obs: Observation) -> Verdict:
        value = recall_of(obs.run.body, obs.truth, pages)
        return Verdict(value >= minimum, f"recall {value:.2f}, needs {minimum:.2f}", {"recall": round(value, 4)})

    return Check(f"recall>={minimum}", "recall", fn)


def recall_at_most(maximum: float, pages: set[int] | None = None) -> Check:
    def fn(obs: Observation) -> Verdict:
        value = recall_of(obs.run.body, obs.truth, pages)
        return Verdict(value <= maximum, f"recall {value:.2f}, allowed {maximum:.2f}", {"recall": round(value, 4)})

    return Check(f"recall<={maximum}", "compare", fn)


def recall_pair(treatment_min: float, control_max: float, pages: set[int] | None = None) -> Check:
    """Option on reads the text, option off does not: the run recalls at least ``treatment_min`` and the
    control at most ``control_max``."""

    def fn(obs: Observation) -> Verdict:
        assert obs.control is not None, "recall_pair needs a control run"
        on = recall_of(obs.run.body, obs.truth, pages)
        off = recall_of(obs.control.body, obs.truth, pages)
        ok = on >= treatment_min and off <= control_max
        return Verdict(
            ok,
            f"recall {on:.2f} with the option (needs {treatment_min:.2f}), {off:.2f} without (allowed {control_max:.2f})",
            {"recall": round(on, 4), "control_recall": round(off, 4)},
        )

    return Check("recall_pair", "recall", fn)


def recall_above_control(margin: float = 0.05, treatment_min: float = 0.0, pages: set[int] | None = None) -> Check:
    """The run recalls more than the control by ``margin``, and at least ``treatment_min``."""

    def fn(obs: Observation) -> Verdict:
        assert obs.control is not None, "recall_above_control needs a control run"
        on = recall_of(obs.run.body, obs.truth, pages)
        off = recall_of(obs.control.body, obs.truth, pages)
        return Verdict(
            on >= off + margin and on >= treatment_min,
            f"recall {on:.2f} against {off:.2f} for the control (needs +{margin:.2f} and {treatment_min:.2f})",
            {"recall": round(on, 4), "control_recall": round(off, 4)},
        )

    return Check("recall_above_control", "recall", fn)


def line_boxes(min_iou: float = 0.5, min_fraction: float = 0.8) -> Check:
    def fn(obs: Observation) -> Verdict:
        fraction, mean = line_box_iou(obs.run.body, obs.truth, min_iou)
        return Verdict(
            fraction >= min_fraction,
            f"{fraction:.0%} of lines have IoU >= {min_iou} (needs {min_fraction:.0%}), mean best IoU {mean:.2f}",
            {"line_iou_share": round(fraction, 4), "line_iou_mean": round(mean, 4)},
        )

    return Check("line_boxes", "geometry", fn)


def boxes_inside_pages(require_boxes: bool = True) -> Check:
    """Every block has a ``bbox`` (with ``require_boxes``) and it lies inside its page."""

    def fn(obs: Observation) -> Verdict:
        checked, problems = bbox_problems(obs.run.body)
        missing = [b for b in blocks_of(obs.run.body) if b.get("bbox") is None] if require_boxes else []
        if missing:
            problems.append(f"{len(missing)} blocks have no bbox")
        if require_boxes and checked == 0 and not problems:
            problems.append("no block could be checked against a page size")
        return Verdict(not problems, "; ".join(problems[:3]), {"boxes_checked": checked})

    return Check("boxes_inside_pages", "geometry", fn)


def confidence_in_range(require_all: bool = False) -> Check:
    """At least one block has ``metadata.confidence`` (every block with ``require_all``), all within 0 to 1."""

    def fn(obs: Observation) -> Verdict:
        values = confidences(obs.run.body)
        problems = confidence_problems(obs.run.body)
        n = _blocks_count(obs.run.body)
        if not values:
            problems.append("no block has a confidence")
        elif require_all and len(values) != n:
            problems.append(f"{n - len(values)} of {n} blocks have no confidence")
        numbers = {"confidences": len(values)}
        if values and all(_number(v) for v in values):
            numbers.update(confidence_min=round(min(values), 4), confidence_max=round(max(values), 4))
        return Verdict(not problems, "; ".join(problems[:3]), numbers)

    return Check("confidence_in_range", "structure", fn)


def no_confidence() -> Check:
    def fn(obs: Observation) -> Verdict:
        values = confidences(obs.run.body)
        return Verdict(not values, f"{len(values)} blocks carry a confidence", {"confidences": len(values)})

    return Check("no_confidence", "structure", fn)


def confidences_at_least(minimum: float) -> Check:
    """At least one block is kept, and every kept block's confidence is ``minimum`` or more."""

    def fn(obs: Observation) -> Verdict:
        values = confidences(obs.run.body)
        low = [v for v in values if not _number(v) or v < minimum]
        ok = bool(values) and not low
        return Verdict(
            ok, f"{len(low)} of {len(values)} confidences below {minimum}", {"blocks": _blocks_count(obs.run.body)}
        )

    return Check(f"confidences>={minimum}", "structure", fn)


def distinct_heading_levels(at_least: int) -> Check:
    def fn(obs: Observation) -> Verdict:
        levels = heading_levels(obs.run.body)
        return Verdict(
            len(set(levels)) >= at_least,
            f"heading levels {sorted(set(levels))}",
            {"heading_levels": sorted(set(levels))},
        )

    return Check(f"heading_levels>={at_least}", "structure", fn)


def headings_share_one_level() -> Check:
    """There are headings and they all have the same level."""

    def fn(obs: Observation) -> Verdict:
        levels = heading_levels(obs.run.body)
        return Verdict(
            bool(levels) and len(set(levels)) == 1,
            f"heading levels {sorted(set(levels))}",
            {"heading_levels": sorted(set(levels))},
        )

    return Check("headings_share_one_level", "structure", fn)


def first_block_heading(level: int) -> Check:
    def fn(obs: Observation) -> Verdict:
        blocks = blocks_of(obs.run.body)
        first = blocks[0] if blocks else {}
        ok = first.get("type") == "heading" and first.get("level") == level
        return Verdict(ok, f"first block is {first.get('type')!r} level {first.get('level')!r}", {})

    return Check(f"first_block_heading_{level}", "structure", fn)


def no_headings() -> Check:
    def fn(obs: Observation) -> Verdict:
        n = len(heading_levels(obs.run.body))
        return Verdict(n == 0, f"{n} heading blocks", {"headings": n})

    return Check("no_headings", "structure", fn)


def fewer_blocks_than_control() -> Check:
    def fn(obs: Observation) -> Verdict:
        assert obs.control is not None, "fewer_blocks_than_control needs a control run"
        a, b = _blocks_count(obs.run.body), _blocks_count(obs.control.body)
        return Verdict(a < b, f"{a} blocks with the option, {b} without", {"blocks": a, "control_blocks": b})

    return Check("fewer_blocks_than_control", "compare", fn)


def page_set_equals(expected: set[int]) -> Check:
    """The pages that blocks sit on, and the pages declared (when any are), are exactly ``expected``."""

    def fn(obs: Observation) -> Verdict:
        found, declared = block_pages(obs.run.body), declared_pages(obs.run.body)
        ok = found == expected and (not declared or declared == expected)
        return Verdict(
            ok,
            f"blocks on pages {sorted(found)}, declared {sorted(declared)}, expected {sorted(expected)}",
            {"pages": sorted(found)},
        )

    return Check("page_set_equals", "structure", fn)


def page_images_match(exact: bool = False) -> Check:
    """Every declared page has an image that decodes to a PNG of the page's size. With a control run, the
    control must have none (the option is what asked for them)."""

    def fn(obs: Observation) -> Verdict:
        problems = page_image_problems(obs.run.body, exact=exact)
        try:
            sizes = page_images(obs.run.body)
        except ValueError:
            sizes = {}
        declared = declared_pages(obs.run.body)
        if not sizes or set(sizes) != declared:
            problems.append(f"images on pages {sorted(sizes)}, pages declared {sorted(declared)}")
        if obs.control is not None and page_images_present(obs.control.body):
            problems.append("the control run also returned page images")
        return Verdict(not problems, "; ".join(problems[:3]), {"images": len(sizes)})

    return Check("page_images_match", "structure", fn)


def page_images_present(ir: Any) -> bool:
    pages = ir.get("pages") if isinstance(ir, Mapping) else None
    return any(isinstance(p, dict) and p.get("image") for p in pages) if isinstance(pages, list) else False


def all_of(name: str, *checks: Check) -> Check:
    """Every check must pass. Takes the ``recall`` kind when any member has it."""

    def fn(obs: Observation) -> Verdict:
        verdicts = [c(obs) for c in checks]
        numbers: dict[str, Any] = {}
        for v in verdicts:
            numbers.update(v.numbers)
        failed = [f"{c.name}: {v.detail}" for c, v in zip(checks, verdicts, strict=True) if not v.ok]
        return Verdict(not failed, "; ".join(failed), numbers)

    kinds = [c.kind for c in checks]
    return Check(name, "recall" if "recall" in kinds else kinds[0], fn)
