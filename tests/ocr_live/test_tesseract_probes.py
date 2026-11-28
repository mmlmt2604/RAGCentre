"""Tier A: the Tesseract probes (``probes/tesseract.py``) against in-process shims, no Tesseract needed.

An oracle that behaves like Tesseract (one option, ``lang``; the English pack garbles umlauts; an unknown pack is a
``400``) passes every probe. Shims that do not behave like it fail on what the predicates see: the null shim ignores
``lang`` and the garbage shim returns unrelated text. The live run is ``runner.py --engine tesseract=URL``.
"""

from __future__ import annotations

from typing import Any

import pytest
import runner
import shims
from client import EngineClient
from fastapi import FastAPI
from fastapi.testclient import TestClient
from probe import FixtureTruth, Probe, load_truth
from shimkit import ShimError, parse_options, string

TRUTH = load_truth()
OPTIONS = [string("lang", "eng", "Tesseract language codes, for example eng or eng+deu")]
PACKS = {"eng", "deu", "osd"}
ENGLISH_MISREADINGS = {"ü": "ii", "ä": "a", "ö": "o", "ß": "fi"}  # what the English pack makes of them


def tesseract_like(ir: dict[str, Any], options: dict[str, Any], truth: FixtureTruth) -> dict[str, Any]:
    languages = parse_options(OPTIONS, options)["lang"].split("+")
    missing = [code for code in languages if code not in PACKS]
    if missing:
        raise ShimError(400, "invalid_options", f"lang: no pack for {', '.join(missing)}")
    if "deu" not in languages:
        for block in ir["blocks"]:
            for letter, misreading in ENGLISH_MISREADINGS.items():
                block["text"] = block["text"].replace(letter, misreading)
    return ir


def first_frame_only(ir: dict[str, Any], options: dict[str, Any], truth: FixtureTruth) -> dict[str, Any]:
    """What the example shim does with a TIFF: it reads frame 1 and says nothing about the others."""
    ir = tesseract_like(ir, options, truth)
    return {**ir, "blocks": [b for b in ir["blocks"] if b["page"] == 1], "pages": ir["pages"][:1]}


def oracle(transform: Any = tesseract_like) -> FastAPI:
    return shims.oracle_app(options=OPTIONS, transform=transform, extensions=shims.IMAGE_EXTENSIONS, jobs=True)


def probes() -> list[Probe]:
    return runner.load_probes("tesseract", TRUTH)[0]


def run(app: FastAPI, selected: list[Probe] | None = None) -> dict[str, Any]:
    client = EngineClient("http://shim", http=TestClient(app))
    table = selected if selected is not None else probes()
    assumed = frozenset(r for p in table for r in p.requires)
    return runner.run_engine("tesseract", client, table, "C", info=client.info().body, truth=TRUTH, assumed=assumed)


def outcomes(report: dict[str, Any]) -> dict[str, str]:
    return {e["id"]: e["outcome"] for e in report["probes"]}


def test_every_probe_is_for_tesseract_and_names_a_fixture_that_exists():
    table = [p for p in probes() if p.engine == "tesseract"]
    assert len(table) >= 6  # an empty table would pass everything below for nothing
    assert all(p.id.startswith("tesseract.") for p in table)


def test_an_oracle_that_behaves_like_tesseract_passes_every_probe():
    report = run(oracle())
    assert [e["id"] for e in report["probes"] if not e["met"]] == []
    assert report["summary"]["ok"]
    assert set(outcomes(report).values()) <= {"pass", "record"}


def test_the_german_probe_needs_the_german_pack_to_read_the_umlauts_not_just_any_recall():
    german = [p for p in probes() if p.id == "tesseract.lang_deu_reads_german"]
    assert len(german) == 1
    assert outcomes(run(oracle(), german)) == {"tesseract.lang_deu_reads_german": "pass"}
    # An engine that reads German the same way with either pack, here the whole truth, has nothing to show for deu.
    assert outcomes(run(shims.null_app(), german)) == {"tesseract.lang_deu_reads_german": "fail"}
    assert outcomes(run(shims.garbage_app(), german)) == {"tesseract.lang_deu_reads_german": "fail"}


def test_the_german_probe_is_skipped_until_the_machine_has_the_pack_set_up(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TESSDATA_PREFIX", raising=False)
    german = [p for p in probes() if p.id == "tesseract.lang_deu_reads_german"]
    client = EngineClient("http://shim", http=TestClient(oracle()))
    report = runner.run_engine("tesseract", client, german, "C", info=client.info().body, truth=TRUTH)
    assert outcomes(report) == {"tesseract.lang_deu_reads_german": "skipped"}
    assert report["probes"][0]["detail"] == "needs env:TESSDATA_PREFIX"


def test_a_shim_that_reads_one_tiff_frame_is_recorded_with_its_pages_and_does_not_fail_the_run():
    report = run(oracle(first_frame_only))
    tiff = next(e for e in report["probes"] if e["id"] == "tesseract.multi_frame_tiff_reads_every_frame")
    assert (tiff["outcome"], tiff["met"], tiff["numbers"]["pages"]) == ("record", False, [1])
    assert 0.2 < tiff["numbers"]["recall"] < 0.4  # 10 of the 35 tokens are on frame 1
    assert report["summary"]["ok"]


def test_a_bad_lang_is_a_bad_option_for_the_oracle_and_a_failure_for_a_shim_that_accepts_it():
    bad_lang = [p for p in probes() if p.id.startswith("tesseract.lang_") and p.expect_status is not None]
    assert len(bad_lang) == 3  # an unknown pack, an empty lang and a path
    assert set(outcomes(run(oracle(), bad_lang)).values()) == {"pass"}
    assert set(outcomes(run(shims.null_app(), bad_lang)).values()) == {"fail"}


def test_the_pdf_is_refused_by_the_oracle_that_reads_images_only():
    report = run(oracle())
    assert outcomes(report)["tesseract.pdf_unsupported"] == "pass"
