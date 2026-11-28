"""Tier A self-tests of the live OCR runner. They need no engine and no network.

1. Predicate unit tests on hand-built good and bad parse results.
2. Every probe in ``probes/*.py`` is run against in-process shims. An oracle (the truth, options honoured) passes
   the ``baseline`` and ``shim`` probes. A null shim, which ignores options and returns the truth, must FAIL every
   paired (option on/off) probe. A garbage shim, which returns unrelated text, must FAIL every recall probe.
3. The runner: outcomes, exit status, tiers, the shared replies, async engines, the report, calibrate.
4. The committed fixtures match ``truth.json``, and regenerating them twice gives identical bytes.

``uv run pytest tests/ocr_live -q`` runs only this file. Live work happens only through ``runner.py``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import statistics
import struct
import subprocess
import sys
import time
import zlib
from dataclasses import replace
from pathlib import Path
from typing import Any

import predicates as P
import pytest
import runner
import shims
from client import EngineClient, Reply
from fastapi import FastAPI, File, UploadFile
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from probe import (
    FIXTURES_DIR,
    HERE,
    TRUTH_JSON,
    Check,
    FixtureTruth,
    LineTruth,
    Observation,
    PageTruth,
    Probe,
    load_truth,
    runs_at,
    unmet_requirements,
)

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


def test_extra_recall_reads_the_named_token_list_and_not_the_page_tokens():
    truth = replace(make_truth(), extras={"image_tokens": ["loading", "dock", "closed"]})
    reads = ir([para("loading dock closed")])
    page_text_only = ir([para("alpha bravo charlie delta echo")])  # the page tokens, none of the picture's
    assert P.extra_recall_at_least("image_tokens", 0.8)(observe(reads, truth=truth)).ok
    assert not P.extra_recall_at_least("image_tokens", 0.8)(observe(page_text_only, truth=truth)).ok
    pair = P.extra_recall_pair("image_tokens", 0.8, 0.1)
    assert pair(observe(reads, page_text_only, truth)).ok
    assert not pair(observe(reads, reads, truth)).ok  # the option changed nothing
    assert not pair(observe(page_text_only, reads, truth)).ok  # the wrong way round
    assert pair(observe(reads, page_text_only, truth)).numbers == {
        "image_tokens_recall": 1.0,
        "image_tokens_control_recall": 0.0,
    }


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


# --- the probe tables against the shims ---------------------------------------------------------

TRUTH = load_truth()
ENGINE_FREE = "t"


def table(*stems: str) -> list[Probe]:
    """The probes of ``probes/<stem>.py`` files, all of them when no stem is given."""
    files = [p for p in runner.probe_files() if not stems or p.stem in stems]
    return [probe for path in files for probe in runner.load_probe_file(path, TRUTH)]


def engine_client(app: FastAPI) -> EngineClient:
    return EngineClient("http://shim", http=TestClient(app))


def run_table(probes: list[Probe], app: FastAPI | None = None, tier: str = "C", **kw: Any) -> dict[str, Any]:
    """Run ``probes`` through the runner against an in-process shim. Requirements count as met unless ``assumed``
    is given, so a failure here is the predicate's and not a missing program."""
    client = engine_client(app or shims.oracle_app())
    info = client.info().body
    kw.setdefault("assumed", frozenset(r for p in probes for r in p.requires))
    return runner.run_engine(ENGINE_FREE, client, probes, tier, info=info, truth=TRUTH, **kw)


def met_ids(app: FastAPI, probes: list[Probe]) -> list[str]:
    return [e["id"] for e in run_table(probes, app)["probes"] if e["met"]]


def answered_ok(entry: dict[str, Any]) -> bool:
    """Every reply was a 200: the probe failed on what its predicate saw and not on an error answer."""
    return bool(entry["replies"]) and all(r["status"] == 200 for r in entry["replies"])


def test_every_probe_module_loads_with_unique_ids_and_real_fixtures():
    probes = table()
    assert len({p.id for p in probes}) == len(probes)
    assert {p.stem for p in runner.probe_files()} >= {"baseline", "shim"}
    assert {p.engine for p in table("baseline")} == {"*"}


def test_the_oracle_shim_passes_every_baseline_and_shim_probe():
    probes = table("baseline", "shim")
    report = run_table(probes)
    assert [e["id"] for e in report["probes"] if not e["met"]] == []
    assert report["summary"]["ok"]


def test_the_null_shim_fails_every_paired_probe_on_what_its_predicate_sees():
    paired = [p for p in table() if p.paired]
    assert len(paired) >= 3  # an empty set would make this test pass for nothing
    entries = run_table(paired, shims.null_app())["probes"]
    assert [e["id"] for e in entries if e["met"]] == []
    # a gate probe fails, a record probe is recorded: either way its predicate said no on a real 200 answer
    assert all(e["outcome"] == ("record" if e["gate"] == "record" else "fail") and answered_ok(e) for e in entries)


def test_the_garbage_shim_fails_every_recall_probe_on_what_its_predicate_sees():
    recall = [p for p in table() if p.predicate is not None and p.predicate.kind == "recall"]
    assert len(recall) >= 5
    entries = run_table(recall, shims.garbage_app())["probes"]
    assert [e["id"] for e in entries if e["met"]] == []
    assert all(answered_ok(e) for e in entries)


def test_the_null_shim_check_has_teeth_a_paired_probe_that_ignores_its_control_slips_through():
    ignores_control = Probe(
        id="weak",
        engine="shim",
        fixture="scan_clean.png",
        options={"ocr": True},
        control={"ocr": False},
        predicate=P.recall_at_least(0.8),  # looks only at the run, so a shim that ignores options passes it
    )
    strict = Probe(
        id="strict",
        engine="shim",
        fixture="scan_clean.png",
        options={"ocr": True},
        control={"ocr": False},
        predicate=P.recall_pair(0.8, 0.1),
    )
    assert met_ids(shims.null_app(), [ignores_control, strict]) == ["weak"]
    assert met_ids(shims.oracle_app(), [ignores_control, strict]) == ["weak", "strict"]


# --- the runner ---------------------------------------------------------------------------------


def probe(id: str, predicate: Check | None = None, **kw: Any) -> Probe:
    if predicate is None and "expect_status" not in kw:
        predicate = P.recall_at_least(0.8)
    return Probe(id=id, engine=ENGINE_FREE, fixture=kw.pop("fixture", "scan_clean.png"), predicate=predicate, **kw)


def test_a_failing_gate_probe_fails_the_run_and_a_failing_record_probe_does_not():
    impossible = P.recall_at_least(1.1)
    report = run_table([probe("gate", impossible), probe("kept", impossible, gate="record")])
    outcomes = {e["id"]: (e["outcome"], e["met"]) for e in report["probes"]}
    assert outcomes == {"gate": ("fail", False), "kept": ("record", False)}
    assert report["summary"]["failed_gates"] == ["gate"] and not report["summary"]["ok"]
    only_record = run_table([probe("kept", impossible, gate="record")])
    assert only_record["summary"]["ok"]
    assert only_record["probes"][0]["numbers"] == {"recall": 1.0}  # the numbers are stored all the same


def test_an_error_probe_passes_on_the_expected_status_and_code_only():
    refuses = probe("refuses", fixture="scan_clean.gif", expect_status=415, expect_code="unsupported_media_type")
    wrong_code = probe("wrong", fixture="scan_clean.gif", expect_status=415, expect_code="something_else")
    wrong_status = probe("accepted", expect_status=415)  # a png the oracle reads: 200
    entries = {e["id"]: e for e in run_table([refuses, wrong_code, wrong_status])["probes"]}
    assert [entries[i]["met"] for i in ("refuses", "wrong", "accepted")] == [True, False, False]
    assert "answered 200, expected 415" in entries["accepted"]["detail"]


def test_a_reply_other_than_200_fails_a_predicate_probe_with_the_engines_reason():
    entry = run_table([probe("typo", options={"no_such_option": 1})])["probes"][0]
    assert entry["outcome"] == "fail" and entry["numbers"] == {}
    assert "run answered 400 invalid_options" in entry["detail"]
    paired = probe("paired", options={"ocr": True}, control={"typo": 1}, predicate=P.recall_above_control())
    assert "control answered 400 invalid_options" in run_table([paired])["probes"][0]["detail"]


def test_a_predicate_that_raises_is_an_error_and_an_error_in_a_gate_probe_fails_the_run():
    boom = Check("boom", "structure", lambda obs: 1 / 0)
    report = run_table([probe("boom", boom), probe("boom_recorded", boom, gate="record")])
    assert [(e["outcome"], "ZeroDivisionError" in e["detail"]) for e in report["probes"]] == [
        ("error", True),
        ("error", True),
    ]
    assert report["summary"]["failed_gates"] == ["boom"]  # a record probe that errors does not fail the run


def test_a_probe_with_an_unmet_requirement_is_skipped_unless_the_requirement_is_assumed():
    needs = probe("needs", requires=("bin:ocr-live-no-such-program",))
    report = run_table([needs], assumed=frozenset())
    entry = report["probes"][0]
    assert (entry["outcome"], entry["met"], entry["detail"]) == ("skipped", None, "needs bin:ocr-live-no-such-program")
    assert report["summary"]["ok"] and report["summary"]["skipped"] == 1
    assert run_table([needs], assumed=frozenset({"bin:ocr-live-no-such-program"}))["probes"][0]["outcome"] == "pass"


def test_tier_and_id_globs_choose_the_probes_that_run():
    a, b, c = probe("a.x", tier="A"), probe("b.x", tier="B"), probe("b.y", tier="C")
    report = run_table([a, b, c], tier="B")
    assert [e["id"] for e in report["probes"]] == ["a.x", "b.x"]
    assert report["summary"]["not_run_by_tier_or_filter"] == 1
    assert [e["id"] for e in run_table([a, b, c], tier="C", only=["b.*"])["probes"]] == ["b.x", "b.y"]


def test_replies_for_the_same_fixture_and_options_are_shared_between_probes():
    app = shims.oracle_app()
    posts: list[str] = []

    @app.middleware("http")
    async def count(request: Any, call_next: Any) -> Any:
        if request.method == "POST":
            posts.append(request.url.path)
        return await call_next(request)

    probes = [
        probe("one", P.ir_valid()),
        probe("two"),
        probe("three", options={"ocr": True}, control={"ocr": False}, predicate=P.recall_pair(0.8, 0.1)),
    ]
    one, two, three = run_table(probes, app)["probes"]
    assert len(posts) == 3  # {}, {"ocr": true} and {"ocr": false}: four replies were read, three parses ran
    assert [r["cached"] for r in one["replies"] + two["replies"] + three["replies"]] == [False, True, False, False]
    assert two["seconds"] == 0.0 and one["seconds"] > 0.0


def test_the_runner_follows_an_engine_that_answers_with_jobs():
    report = run_table(
        [probe("one", P.ir_valid()), probe("two", fixture="scan_german.png")], shims.oracle_app(jobs=True)
    )
    assert report["info"]["capabilities"]["async"] is True
    assert [e["met"] for e in report["probes"]] == [True, True]
    assert {r["mode"] for e in report["probes"] for r in e["replies"]} == {"async"}
    assert all(e["seconds"] > 0 for e in report["probes"])  # each waited for its own job


def test_engine_arguments_are_checked():
    assert runner.parse_engines(["a=http://x:1", "b=https://y"], None) == [("a", "http://x:1"), ("b", "https://y")]
    assert runner.parse_engines(["a=http://x", "b=http://y"], "b") == [("b", "http://y")]
    for bad in ("a", "a=ftp://x", "=http://x", "../a=http://x", "baseline=http://x", "a b=http://x"):
        with pytest.raises(runner.UsageError):
            runner.parse_engines([bad], None)
    for specs, engine_filter in ((["a=http://x", "a=http://y"], None), (["a=http://x"], "zzz"), ([], None)):
        with pytest.raises(runner.UsageError):
            runner.parse_engines(specs, engine_filter)
    assert runner.key_variable("my-engine.v2") == "OCR_LIVE_API_KEY_MY_ENGINE_V2"


@pytest.fixture(scope="module")
def served():
    with (
        shims.serving(shims.oracle_app()) as oracle,
        shims.serving(shims.null_app()) as null,
        shims.serving(shims.garbage_app()) as garbage,
    ):
        yield {"oracle": oracle, "null": null, "garbage": garbage}


def cli(*args: str) -> int:
    return runner.main(list(args))


def test_the_command_line_exits_0_on_the_oracle_and_writes_a_complete_report(served, tmp_path, capsys):
    path = tmp_path / "report.json"
    assert cli("--engine", f"shim={served['oracle']}", "--tier", "C", "--report", str(path)) == 0
    report = json.loads(path.read_text())
    engine = report["engines"]["shim"]
    assert report["ok"] and engine["engine_version"] == "0.0.0+oracle-shim"
    assert engine["info"]["capabilities"] == {"page_images": False}
    assert {"system", "arch", "cpu_count", "memory_gb", "python"} <= set(report["machine"])
    assert "node" not in report["machine"]  # no host name in a report that gets shared
    assert (engine["summary"]["fail"], engine["summary"]["error"], engine["summary"]["skipped"]) == (0, 0, 1)
    first = engine["probes"][0]
    assert {"id", "outcome", "met", "seconds", "numbers", "replies"} <= set(first)
    assert "shim.ocr_off_reads_nothing" in capsys.readouterr().out


def test_the_command_line_exits_1_when_a_gate_probe_fails(served, tmp_path):
    for shim, failed in (
        ("null", {"shim.ocr_off_reads_nothing", "shim.fast_reads_less", "shim.merge_makes_fewer_blocks"}),
        ("garbage", {"baseline.clean_scan_recall", "shim.recall_chinese", "shim.recall_multipage_pdf"}),
    ):
        path = tmp_path / f"{shim}.json"
        assert cli("--engine", f"shim={served[shim]}", "--tier", "C", "--report", str(path)) == 1
        report = json.loads(path.read_text())
        assert not report["ok"] and failed <= set(report["engines"]["shim"]["summary"]["failed_gates"])


def test_the_command_line_exits_2_for_usage_errors_and_an_engine_that_is_not_there(served, capsys):
    assert cli("--engine", "shim=http://127.0.0.1:1") == 2  # nothing listens on port 1
    assert "cannot reach" in capsys.readouterr().err
    assert cli("--engine", "shim") == 2
    assert cli("--engine", f"shim={served['oracle']}", "--probe", "no.such.probe") == 2
    assert cli() == 2  # no engine given
    with pytest.raises(SystemExit) as bad_flag:
        cli("--engine", f"shim={served['oracle']}", "--tier", "Z")
    assert bad_flag.value.code == 2


def test_the_engine_filter_and_probe_globs_pick_what_runs(served, tmp_path):
    path = tmp_path / "r.json"
    code = cli(
        "--engine",
        f"shim={served['oracle']}",
        "--engine",
        "other=http://127.0.0.1:1",  # not reachable, but filtered out before it is contacted
        "--engine-filter",
        "shim",
        "--probe",
        "baseline.*",
        "--tier",
        "A",
        "--report",
        str(path),
    )
    report = json.loads(path.read_text())
    assert code == 0 and list(report["engines"]) == ["shim"]
    assert [e["id"] for e in report["engines"]["shim"]["probes"]] == [
        "baseline.unknown_option",
        "baseline.gif_unsupported",
    ]


def test_list_shows_the_probe_table_without_contacting_the_engine(capsys):
    assert cli("--engine", "shim=http://127.0.0.1:1", "--list") == 0
    listed = capsys.readouterr().out
    assert "baseline.py + shim.py" in listed and "paired  shim.ocr_off_reads_nothing" in listed


def test_the_runner_sends_the_key_found_in_the_environment(monkeypatch):
    monkeypatch.setenv("OCR_LIVE_SHIM_API_KEY", "s3cret")  # the shim's own key: it refuses parse calls without it
    with shims.serving(shims.oracle_app()) as url:
        monkeypatch.delenv("OCR_LIVE_API_KEY_SHIM", raising=False)
        assert cli("--engine", f"shim={url}", "--tier", "A") == 1  # 401 where 400 and 415 were expected
        monkeypatch.setenv("OCR_LIVE_API_KEY_SHIM", "s3cret")
        assert cli("--engine", f"shim={url}", "--tier", "A") == 0


def test_calibrate_times_the_clean_scan_cold_then_warm(served, tmp_path, capsys):
    path = tmp_path / "cal.json"
    assert cli("calibrate", "--engine", f"shim={served['null']}", "--runs", "3", "--report", str(path)) == 0
    calibration = json.loads(path.read_text())["engines"]["shim"]["calibration"]
    assert (calibration["fixture"], calibration["pages"], len(calibration["seconds"])) == ("scan_clean.png", 1, 3)
    assert calibration["cold_seconds"] == calibration["seconds"][0]
    assert calibration["warm_seconds"] == pytest.approx(statistics.median(calibration["seconds"][1:]), abs=1e-3)
    assert calibration["warm_seconds_per_page"] == calibration["warm_seconds"]  # one page
    assert "cold" in capsys.readouterr().out
    assert cli("calibrate", "--engine", f"shim={served['null']}", "--runs", "1") == 2  # no warm run to time


def test_calibrate_fails_when_the_engine_cannot_read_the_scan(tmp_path):
    only_gif = shims.oracle_app(extensions=frozenset({"gif"}))
    with shims.serving(only_gif) as url:
        assert cli("calibrate", "--engine", f"shim={url}", "--report", str(tmp_path / "c.json")) == 1
    result = json.loads((tmp_path / "c.json").read_text())["engines"]["shim"]["calibration"]
    assert result["ok"] is False and "415" in result["detail"]


def test_the_runner_runs_as_a_script():
    done = subprocess.run(
        [sys.executable, str(HERE / "runner.py"), "--engine", "shim=http://127.0.0.1:1", "--list"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0 and "baseline.ir_valid" in done.stdout, done.stderr


# --- the fixtures -------------------------------------------------------------------------------

MAX_FIXTURE_BYTES = 200 * 1024
NO_FONTS = 3  # make_fixtures.EXIT_NO_FONTS


def test_the_committed_fixtures_are_the_files_truth_json_describes():
    described = json.loads(TRUTH_JSON.read_text(encoding="utf-8"))["fixtures"]
    assert {p.name for p in FIXTURES_DIR.iterdir()} == set(described)
    for name, entry in described.items():
        data = (FIXTURES_DIR / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["sha256"], name
        assert len(data) == entry["bytes"] < MAX_FIXTURE_BYTES, name


def test_every_fixture_has_tokens_to_find_and_line_boxes_inside_its_page():
    for name, truth in TRUTH.items():
        assert truth.tokens(), name
        for page in truth.pages:
            assert page.tokens, (name, page.page)
            if not page.width:  # a text or office file has no page geometry
                continue
            for line in page.lines:
                x0, y0, x1, y1 = line.bbox
                assert 0 <= x0 < x1 <= page.width + 1 and 0 <= y0 < y1 <= page.height + 1, (name, page.page, line.text)


def generate(out: Path) -> subprocess.CompletedProcess[str]:
    command = ["uv", "run", "--offline", "--with", "pillow", "--with", "reportlab", "python"]
    return subprocess.run(
        [*command, str(HERE / "make_fixtures.py"), "--out", str(out)], capture_output=True, text=True, timeout=300
    )


def test_regenerating_the_fixtures_twice_gives_identical_bytes(tmp_path):
    if shutil.which("uv") is None:
        pytest.skip("uv is not on PATH, and the generator needs pillow and reportlab (not project dependencies)")
    available = subprocess.run(
        ["uv", "run", "--offline", "--with", "pillow", "--with", "reportlab", "python", "-c", "import PIL, reportlab"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if available.returncode != 0:
        pytest.skip("pillow and reportlab cannot be resolved offline: " + available.stderr.strip()[-200:])
    first, second = generate(tmp_path / "a"), generate(tmp_path / "b")
    if NO_FONTS in (first.returncode, second.returncode):
        pytest.skip(first.stdout.strip())
    assert (first.returncode, second.returncode) == (0, 0), first.stderr[-500:] + second.stderr[-500:]
    files_a = sorted(p.relative_to(tmp_path / "a") for p in (tmp_path / "a").rglob("*") if p.is_file())
    files_b = sorted(p.relative_to(tmp_path / "b") for p in (tmp_path / "b").rglob("*") if p.is_file())
    assert files_a == files_b and len(files_a) == len(TRUTH) + 1  # every fixture and truth.json
    differing = [str(f) for f in files_a if (tmp_path / "a" / f).read_bytes() != (tmp_path / "b" / f).read_bytes()]
    assert differing == []
