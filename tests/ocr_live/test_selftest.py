"""Tier A self-tests of the live OCR runner. They need no engine, no network and no fonts.

1. Predicate unit tests on hand-built good and bad parse results.
2. A null shim, which ignores options and returns the truth, must FAIL every paired (option on/off) probe.
3. A garbage shim, which returns unrelated text, must FAIL every recall probe.
Plus: the committed fixtures match ``truth.json``, and regenerating them gives identical bytes.

``uv run pytest tests/ocr_live -q`` runs only this file. Live work happens only through ``runner.py``.
"""

from __future__ import annotations

import base64
import struct
import time
import zlib
from pathlib import Path
from typing import Any

import predicates as P
import pytest
from client import EngineClient, Reply
from fastapi import FastAPI, File, UploadFile
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from probe import FixtureTruth, LineTruth, Observation, PageTruth, Probe, runs_at, unmet_requirements

# --- builders for hand-made truth and results ---------------------------------------------------


def make_truth(
    pages: dict[int, list[str]] | None = None,
    *,
    lines: dict[int, list[tuple[str, tuple[float, float, float, float]]]] | None = None,
    size: tuple[float, float] = (1000.0, 500.0),
) -> FixtureTruth:
    pages = pages or {1: ["alpha", "bravo", "charlie", "delta", "echo"]}
    lines = lines or {}
    return FixtureTruth(
        name="hand_built.png",
        kind="image",
        units="px",
        language="en",
        sha256="",
        pages=tuple(
            PageTruth(
                page=n,
                width=size[0],
                height=size[1],
                tokens=tuple(tokens),
                lines=tuple(LineTruth(text, tuple(P.tokenize(text)), box) for text, box in lines.get(n, [])),
                blocks=(),
            )
            for n, tokens in pages.items()
        ),
        extras={},
    )


def ir(blocks: list[dict[str, Any]], pages: list[dict[str, Any]] | None = None, **meta: Any) -> dict[str, Any]:
    return {"schema_version": "1.0", "metadata": meta, "pages": pages or [], "blocks": blocks}


def para(text: str, **kw: Any) -> dict[str, Any]:
    return {"type": "paragraph", "text": text, **kw}


def png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    raw = b"".join(b"\x00" + b"\xff" * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def page_with_image(page: int, width: int, height: int, image_size: tuple[int, int]) -> dict[str, Any]:
    data = base64.b64encode(png(*image_size)).decode()
    return {"page": page, "width": width, "height": height, "image": {"mime": "image/png", "data_b64": data}}


def observe(body: Any, control: Any = None, truth: FixtureTruth | None = None) -> Observation:
    return Observation(
        run=Reply(200, body, 0.1),
        control=None if control is None else Reply(200, control, 0.1),
        truth=truth or make_truth(),
        info={},
    )


# --- tokens and recall --------------------------------------------------------------------------


def test_tokenize_lowercases_drops_punctuation_and_keeps_german_letters():
    assert P.tokenize("Die Straße, größer als 3.5!") == ["die", "straße", "größer", "als", "3", "5"]


def test_tokenize_makes_one_token_per_chinese_character():
    assert P.tokenize("你好, world 世界") == ["你", "好", "world", "世", "界"]


def test_recall_counts_a_close_misreading_but_not_a_short_word_that_differs():
    # "readinq" shares 6 of 7 letters with "reading" (ratio 0.86); "car" against "cat" is 0.67.
    assert P.recall(["reading", "cat"], ["readinq", "car"]) == 0.5


def test_recall_lets_one_result_token_satisfy_one_truth_token_only():
    assert P.recall(["the", "the", "the"], ["the"]) == pytest.approx(1 / 3)


def test_recall_of_unrelated_text_is_zero_and_of_an_empty_truth_is_one():
    assert P.recall(["alpha", "bravo"], P.tokenize("lorem ipsum dolor sit amet")) == 0.0
    assert P.recall([], ["anything"]) == 1.0


def test_recall_of_a_page_window_reads_only_those_pages():
    truth = make_truth({1: ["one", "uno"], 2: ["two", "dos"]})
    body = ir([para("one uno", page=1), para("two dos", page=2)])
    assert P.recall_of(body, truth, {2}) == 1.0
    assert P.recall_of(ir([para("one uno", page=1)]), truth, {2}) == 0.0


def test_recall_checks_pass_on_the_truth_and_fail_on_garbage():
    truth = make_truth()
    good = ir([para("alpha bravo charlie delta echo")])
    bad = ir([para("lorem ipsum dolor sit amet")])
    check = P.recall_at_least(0.8)
    assert check(observe(good, truth=truth)).ok
    verdict = check(observe(bad, truth=truth))
    assert not verdict.ok and verdict.numbers["recall"] == 0.0
    assert P.recall_at_most(0.1)(observe(bad, truth=truth)).ok
    assert not P.recall_at_most(0.1)(observe(good, truth=truth)).ok


def test_recall_pair_needs_the_option_on_to_read_and_off_to_not_read():
    good, empty = ir([para("alpha bravo charlie delta echo")]), ir([])
    assert P.recall_pair(0.8, 0.1)(observe(good, empty)).ok
    assert not P.recall_pair(0.8, 0.1)(observe(good, good)).ok  # the option changed nothing
    assert not P.recall_pair(0.8, 0.1)(observe(empty, empty)).ok
    assert not P.recall_pair(0.8, 0.1)(observe(empty, good)).ok  # the wrong way round


def test_recall_above_control_needs_a_margin():
    full, half = ir([para("alpha bravo charlie delta echo")]), ir([para("alpha bravo")])
    assert P.recall_above_control(0.2)(observe(full, half)).ok
    assert not P.recall_above_control(0.2)(observe(full, full)).ok
    assert not P.recall_above_control(0.2)(observe(half, full)).ok


# --- boxes --------------------------------------------------------------------------------------


def test_iou_of_identical_overlapping_and_disjoint_boxes():
    assert P.iou([0, 0, 10, 10], [0, 0, 10, 10]) == 1.0
    assert P.iou([0, 0, 10, 10], [5, 0, 15, 10]) == pytest.approx(1 / 3)
    assert P.iou([0, 0, 10, 10], [20, 20, 30, 30]) == 0.0


def test_line_boxes_scales_truth_to_the_page_size_the_result_declares():
    truth = make_truth(lines={1: [("alpha bravo", (100, 100, 300, 140)), ("charlie delta", (100, 200, 300, 240))]})
    same = ir([para("a", page=1, bbox=[100, 100, 300, 140]), para("b", page=1, bbox=[100, 200, 300, 240])])
    doubled = ir(
        [para("a", page=1, bbox=[200, 200, 600, 280]), para("b", page=1, bbox=[200, 400, 600, 480])],
        pages=[{"page": 1, "width": 2000, "height": 1000}],
    )
    shifted = ir([para("a", page=1, bbox=[100, 300, 300, 340])])
    assert P.line_boxes()(observe(same, truth=truth)).ok
    assert P.line_boxes()(observe(doubled, truth=truth)).ok  # 2x page, 2x boxes
    verdict = P.line_boxes()(observe(shifted, truth=truth))
    assert not verdict.ok and verdict.numbers["line_iou_share"] == 0.0


# --- headings, confidence, pages ----------------------------------------------------------------


def heading(level: int, text: str = "h") -> dict[str, Any]:
    return {"type": "heading", "level": level, "text": text}


def test_heading_checks():
    varied = ir([heading(1), para("x"), heading(2), heading(3)])
    flat = ir([heading(2), heading(2)])
    none = ir([para("x")])
    assert P.distinct_heading_levels(2)(observe(varied)).ok
    assert not P.distinct_heading_levels(2)(observe(flat)).ok
    assert P.headings_share_one_level()(observe(flat)).ok
    assert not P.headings_share_one_level()(observe(varied)).ok
    assert not P.headings_share_one_level()(observe(none)).ok  # no headings is not "one level"
    assert P.first_block_heading(1)(observe(varied)).ok
    assert not P.first_block_heading(1)(observe(ir([para("x"), heading(1)]))).ok
    assert P.no_headings()(observe(none)).ok
    assert not P.no_headings()(observe(varied)).ok


def test_boxes_inside_pages_flags_each_kind_of_bad_box():
    pages = [{"page": 1, "width": 100, "height": 50}]

    def check(box: Any) -> list[str]:
        return P.bbox_problems(ir([para("x", page=1, bbox=box)], pages))[1]

    assert check([0, 0, 100, 50]) == []
    assert check([-0.5, 0, 100.5, 50]) == []  # inside the 1% slack
    assert "outside" in check([0, 0, 140, 50])[0]
    assert "x0 <= x1" in check([60, 0, 10, 50])[0]
    assert "four finite numbers" in check([0, 0, float("nan"), 50])[0]
    assert "four finite numbers" in check([0, 0, 10])[0]
    # A block on a page that declares no size cannot be checked: the check that requires boxes says so.
    no_size = ir([para("x", page=1, bbox=[0, 0, 5, 5])])
    assert not P.boxes_inside_pages()(observe(no_size)).ok
    assert P.boxes_inside_pages()(observe(ir([para("x", page=1, bbox=[0, 0, 5, 5])], pages))).ok
    assert not P.boxes_inside_pages()(observe(ir([para("x", page=1)], pages))).ok  # a block with no bbox


def test_confidence_checks():
    def with_conf(*values: Any) -> dict[str, Any]:
        return ir([para("x", metadata={"confidence": v}) for v in values] + [para("no confidence")])

    assert P.confidence_in_range()(observe(with_conf(0.0, 0.5, 1.0))).ok
    assert not P.confidence_in_range()(observe(with_conf(1.2))).ok
    assert not P.confidence_in_range()(observe(with_conf(True))).ok  # a bool is not a number
    assert not P.confidence_in_range()(observe(with_conf("0.9"))).ok
    assert not P.confidence_in_range()(observe(ir([para("x")]))).ok  # none present
    assert not P.confidence_in_range(require_all=True)(observe(with_conf(0.5))).ok  # one block lacks it
    assert P.no_confidence()(observe(ir([para("x")]))).ok
    assert not P.no_confidence()(observe(with_conf(0.5))).ok
    assert P.confidences_at_least(0.9)(observe(ir([para("x", metadata={"confidence": 0.93})]))).ok
    assert not P.confidences_at_least(0.9)(observe(with_conf(0.95, 0.4))).ok
    assert not P.confidences_at_least(0.9)(observe(ir([]))).ok  # a filter that dropped everything proves nothing


def test_page_set_equals():
    on_2_3 = ir([para("a", page=2), para("b", page=3)], pages=[{"page": 2}, {"page": 3}])
    all_pages = ir([para("a", page=1), para("b", page=2), para("c", page=3)])
    assert P.page_set_equals({2, 3})(observe(on_2_3)).ok
    assert not P.page_set_equals({2, 3})(observe(all_pages)).ok
    declared_too_many = ir([para("a", page=2)], pages=[{"page": 1}, {"page": 2}])
    assert not P.page_set_equals({2})(observe(declared_too_many)).ok


def test_page_images_match_decodes_the_png_and_compares_with_the_page():
    good = ir([], pages=[page_with_image(1, 612, 792, (612, 792))])
    scaled = ir([], pages=[page_with_image(1, 612, 792, (1224, 1584))])
    wrong_shape = ir([], pages=[page_with_image(1, 612, 792, (800, 800))])
    broken = ir(
        [], pages=[{"page": 1, "width": 612, "height": 792, "image": {"mime": "image/png", "data_b64": "AAAA"}}]
    )
    no_image = ir([], pages=[{"page": 1, "width": 612, "height": 792}])
    assert P.page_images_match()(observe(good)).ok
    assert P.page_images_match()(observe(scaled)).ok  # same aspect, rendered at 2x
    assert not P.page_images_match(exact=True)(observe(scaled)).ok  # a shim that says bbox is in image pixels
    assert not P.page_images_match()(observe(wrong_shape)).ok
    assert not P.page_images_match()(observe(broken)).ok
    assert not P.page_images_match()(observe(no_image)).ok
    assert P.page_images_match()(observe(good, control=no_image)).ok
    assert not P.page_images_match()(observe(good, control=good)).ok  # the control also had images


def test_fewer_blocks_than_control():
    assert P.fewer_blocks_than_control()(observe(ir([para("a")]), ir([para("a"), para("b")]))).ok
    assert not P.fewer_blocks_than_control()(observe(ir([para("a")]), ir([para("a")]))).ok


# --- IR validity --------------------------------------------------------------------------------


def test_ir_problems_empty_for_a_valid_ir_and_named_for_each_defect():
    pages = [{"page": 1, "width": 100, "height": 50}]
    valid = ir(
        [heading(1, "T"), para("x", page=1, bbox=[1, 1, 50, 20], metadata={"confidence": 0.5})], pages, page_count=1
    )
    assert P.ir_problems(valid) == []
    assert P.ir_valid()(observe(valid)).ok

    def problems(body: Any) -> str:
        return "; ".join(P.ir_problems(body))

    assert "schema_version" in problems({"blocks": []})
    assert "level" in problems(ir([{"type": "heading", "text": "no level"}]))
    assert "duplicate block id" in problems(ir([para("a", id="x"), para("b", id="x")]))
    assert "not one of the pages" in problems(ir([para("a", page=2)], pages))
    assert "page_count" in problems(ir([], pages, page_count=3))
    assert "outside" in problems(ir([para("a", page=1, bbox=[0, 0, 500, 20])], pages))
    assert "confidence" in problems(ir([para("a", metadata={"confidence": 7})]))
    assert not P.ir_valid()(observe(ir([para("a", page=2)], pages))).ok
    assert P.ir_problems("not a document")  # not even an object


# --- probe shape --------------------------------------------------------------------------------


def test_probe_needs_a_predicate_or_an_expected_status_and_only_one():
    check = P.ir_valid()
    Probe(id="a", engine="*", fixture="f", predicate=check)
    Probe(id="b", engine="*", fixture="f", expect_status=415, expect_code="unsupported_media_type")
    for bad in (
        {},  # neither
        {"predicate": check, "expect_status": 400},  # both
        {"expect_status": 400, "control": {"x": 1}},  # a paired probe has a predicate
        {"predicate": check, "expect_code": "x"},
        {"predicate": check, "gate": "maybe"},
        {"predicate": check, "tier": "D"},
    ):
        with pytest.raises(ValueError):
            Probe(id="bad", engine="*", fixture="f", **bad)


def test_tiers_are_cumulative():
    probe_a, probe_b, probe_c = (Probe(id=t, engine="*", fixture="f", predicate=P.ir_valid(), tier=t) for t in "ABC")
    assert [runs_at(p, "A") for p in (probe_a, probe_b, probe_c)] == [True, False, False]
    assert [runs_at(p, "B") for p in (probe_a, probe_b, probe_c)] == [True, True, False]
    assert [runs_at(p, "C") for p in (probe_a, probe_b, probe_c)] == [True, True, True]


def test_requirements_check_the_machine_the_capability_and_what_was_assumed():
    info = {"capabilities": {"async": True, "page_images": False}}
    assert unmet_requirements(("capability:async",), info) == []
    assert unmet_requirements(("capability:page_images",), info) == ["capability:page_images"]
    assert unmet_requirements(("bin:definitely-not-a-program",), info) == ["bin:definitely-not-a-program"]
    assert (
        unmet_requirements(("bin:definitely-not-a-program",), info, frozenset({"bin:definitely-not-a-program"})) == []
    )
    assert unmet_requirements(("env:OCR_LIVE_SURELY_UNSET",), info) == ["env:OCR_LIVE_SURELY_UNSET"]
    assert unmet_requirements(("platform:plan9",), info) == ["platform:plan9"]
    with pytest.raises(ValueError):
        unmet_requirements(("gpu:yes",), info)


# --- client -------------------------------------------------------------------------------------


def async_only_app(result: dict[str, Any], *, running_polls: int = 0) -> FastAPI:
    """A shim that answers every parse with ``202`` and a 2 second ``poll_after_seconds`` hint."""
    app = FastAPI()
    polls = {"n": 0}

    @app.get("/info")
    def info() -> dict[str, Any]:
        return {"capabilities": {"async": True}}

    @app.post("/parse", response_model=None)
    def parse(file: UploadFile = File(...)) -> JSONResponse:
        return JSONResponse({"job_id": "j1", "poll_after_seconds": 2}, status_code=202)

    @app.get("/jobs/{job_id}")
    def status(job_id: str) -> dict[str, str]:
        polls["n"] += 1
        return {"status": "running" if polls["n"] <= running_polls else "succeeded"}

    @app.get("/jobs/{job_id}/result")
    def job_result(job_id: str) -> dict[str, Any]:
        return result

    @app.delete("/jobs/{job_id}", status_code=204)
    def delete(job_id: str) -> None:
        return None

    return app


def test_client_times_a_fast_async_job_by_its_work_not_by_the_two_second_hint(tmp_path: Path):
    path = tmp_path / "a.png"
    path.write_bytes(b"not read by this shim")
    client = EngineClient("http://shim", http=TestClient(async_only_app(ir([para("done")]), running_polls=2)))
    start = time.perf_counter()
    reply = client.parse(path)
    elapsed = time.perf_counter() - start
    assert (reply.status, reply.mode, reply.body["blocks"][0]["text"]) == (200, "async", "done")
    assert elapsed < 1.5  # it waited for 2 running polls (0.2 s + 0.3 s), not for the 2 s hint
    assert reply.seconds == pytest.approx(elapsed, abs=0.05)
