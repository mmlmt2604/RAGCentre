"""Host contract for OCR routing (PR 1 of the OCR verification plan, tier A).

No engine runs. Each test drives RAGCentre through its HTTP API and asserts what a user can see: which
engine read a document (the fake engines sign their output), what each shim was asked, node runs, the
parsed output and the pipeline's status. See ``ocr_contract_support`` for the fake shim farm.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ocr_contract_support import (
    ENGINE_A,
    ENGINE_B,
    HEADING_BBOX,
    LABEL_A,
    LABEL_B,
    Host,
    ShimFarm,
    engine_ir,
    open_host,
    pipeline_body,
    route,
)

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
JOBS = pytest.mark.parametrize("use_jobs", [False, True], ids=["sync", "async"])


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Host]:
    with open_host(tmp_path, monkeypatch) as opened:
        yield opened


@pytest.fixture
def fakes(host: Host) -> ShimFarm:
    return host.farm


def reader(host: Host, document: dict[str, Any], pipeline_id: str) -> str:
    """The engine that read a document in a pipeline, from the signature in its parsed text."""
    signed = {
        match.group(1)
        for text in host.texts(document["id"], pipeline_id)
        if (match := re.fullmatch(r"Read by (engine-\d+)\.", text))
    }
    assert len(signed) == 1, f"expected one engine's signature in {document['filename']}, found {signed}"
    return signed.pop()


def not_parsed(host: Host, document: dict[str, Any], pipeline_id: str) -> bool:
    response = host.client.get(f"/v1/documents/{document['id']}/parsed", params={"pipeline_id": pipeline_id})
    status: int = response.status_code
    return status == 404


# ---- first match wins -----------------------------------------------------------------------------


@JOBS
def test_the_first_route_that_matches_reads_the_document(host: Host, fakes: ShimFarm, use_jobs: bool) -> None:
    fakes.parser_async = use_jobs
    host.add_parser("docling", ENGINE_A)
    host.add_parser("scanner", ENGINE_B)
    doc = host.upload("manual.pdf")

    # The catch-all is listed first, so it takes the PDF even though a later route names PDF.
    catch_all_first = host.pipeline(pipeline_body("catch-all-first", [route("*/*", "docling"), route(PDF, "scanner")]))
    assert reader(host, doc, catch_all_first) == LABEL_A
    assert (len(fakes.parses(LABEL_A)), len(fakes.parses(LABEL_B))) == (1, 0)

    # The same two routes the other way round hand the same PDF to the other engine.
    specific_first = host.pipeline(pipeline_body("specific-first", [route(PDF, "scanner"), route("*/*", "docling")]))
    assert reader(host, doc, specific_first) == LABEL_B
    assert (len(fakes.parses(LABEL_A)), len(fakes.parses(LABEL_B))) == (1, 1)

    # Order is part of the pipeline: the two do not share a parse, and the first still reads as before.
    assert host.parse_node_fp(catch_all_first) != host.parse_node_fp(specific_first)
    assert reader(host, doc, catch_all_first) == LABEL_A


def test_routes_are_globs_over_the_mime_type(host: Host, fakes: ShimFarm) -> None:
    host.add_parser("docling", ENGINE_A)
    host.add_parser("scanner", ENGINE_B)
    files = {
        "scan.png": "image/png",
        "scan.jpg": "image/jpeg",
        "scan.tiff": "image/tiff",
        "manual.pdf": PDF,
        "sheet.docx": DOCX,
        "note.txt": "text/plain",
    }
    # Every file has its own bytes: a parse is cached by content, and a repeat would not reach an engine.
    docs = {name: host.upload(name, f"{name} bytes".encode(), mime) for name, mime in files.items()}
    pipeline_id = host.pipeline(
        pipeline_body(
            "by-glob",
            [
                route("image/[pj]*", "scanner"),  # png and jpeg, not tiff
                route("application/*", "docling"),  # pdf and docx
                route("text/*", "builtin-text"),  # read in process, no engine
            ],
        )
    )

    runs = {name: host.parse_run(doc["id"], pipeline_id) for name, doc in docs.items()}
    assert {name: run["status"] for name, run in runs.items()} == {
        "scan.png": "succeeded",
        "scan.jpg": "succeeded",
        "scan.tiff": "failed",
        "manual.pdf": "succeeded",
        "sheet.docx": "succeeded",
        "note.txt": "succeeded",
    }
    assert runs["scan.tiff"]["error_code"] == "unsupported_media_type"
    assert "image/tiff" in runs["scan.tiff"]["error_message"]

    assert {name: reader(host, docs[name], pipeline_id) for name in ("scan.png", "scan.jpg")} == {
        "scan.png": LABEL_B,
        "scan.jpg": LABEL_B,
    }
    assert {name: reader(host, docs[name], pipeline_id) for name in ("manual.pdf", "sheet.docx")} == {
        "manual.pdf": LABEL_A,
        "sheet.docx": LABEL_A,
    }
    assert host.texts(docs["note.txt"]["id"], pipeline_id) == ["note.txt bytes"]

    # Exactly the four files that matched an engine route reached an engine, each with its own MIME type.
    assert {(c.engine, c.filename, c.mime) for c in fakes.parses()} == {
        (LABEL_B, "scan.png", "image/png"),
        (LABEL_B, "scan.jpg", "image/jpeg"),
        (LABEL_A, "manual.pdf", PDF),
        (LABEL_A, "sheet.docx", DOCX),
    }
    assert len(fakes.parses()) == 4
    assert host.settle(pipeline_id)["status"] == "degraded"  # one file has no route


# ---- a file no route matches ----------------------------------------------------------------------


def test_a_file_no_route_matches_fails_while_the_rest_still_parse(host: Host, fakes: ShimFarm) -> None:
    host.add_parser("docling")
    pdf = host.upload("manual.pdf", b"pdf bytes")
    png = host.upload("scan.png", b"png bytes", "image/png")
    pdf_only = host.pipeline(pipeline_body("pdf-only", [route(PDF, "docling")]))

    run = host.parse_run(png["id"], pdf_only)
    assert run["status"] == "failed"
    assert run["error_code"] == "unsupported_media_type"
    assert run["attempt"] == 1  # nothing to retry: the route table is part of the pipeline
    assert "image/png" in run["error_message"]
    state = host.settle(pdf_only)
    assert state["status"] == "degraded"
    assert [r["id"] for r in state["recent_failures"]] == [run["id"]]
    assert not_parsed(host, png, pdf_only)
    assert [c.filename for c in fakes.parses()] == ["manual.pdf"]  # the image never reached an engine

    # The PDF in the same corpus is unaffected, and can be asked about.
    assert host.parse_run(pdf["id"], pdf_only)["status"] == "succeeded"
    assert reader(host, pdf, pdf_only) == LABEL_A

    # Trying again does not help, and does not call an engine either.
    again = host.retry_parse(png["id"], pdf_only)
    assert (again["status"], again["error_code"]) == ("failed", "unsupported_media_type")
    assert len(fakes.parses()) == 1

    # Another pipeline whose routes cover images reads the same file, so the file was not the problem.
    with_images = host.pipeline(pipeline_body("with-images", [route(PDF, "docling"), route("image/*", "docling")]))
    assert host.parse_run(png["id"], with_images)["status"] == "succeeded"
    assert host.settle(with_images)["status"] == "ready"
    assert not_parsed(host, png, pdf_only)


# ---- what the engine says about the page survives -------------------------------------------------


def layout(parsed: dict[str, Any]) -> dict[str, Any]:
    """Where things are on the page, as an OCR parse reports it, in a form that is easy to compare."""
    return {
        "page_count": parsed["metadata"].get("page_count"),
        "pages": [(p["page"], p.get("width"), p.get("height")) for p in parsed["pages"]],
        "blocks": [
            (
                b["type"],
                b["text"],
                b.get("level"),
                b["page"],
                b["bbox"],
                b["metadata"].get("confidence"),
                b.get("html"),
            )
            for b in parsed["blocks"]
        ],
    }


def test_page_position_and_confidence_survive_the_parse_cache(host: Host, fakes: ShimFarm) -> None:
    host.add_parser("docling")
    doc = host.upload("manual.pdf")
    options = {"tables": "accurate"}  # adds a table block, which carries html as well
    expected = layout(engine_ir(LABEL_A, options))
    # The expectation is anchored to values written out here, so it cannot go empty along with the output.
    heading, _, _, table = expected["blocks"]
    assert heading[3:6] == (1, HEADING_BBOX, 0.98)
    assert table[0] == "table" and table[3] == 2 and table[5] == 0.91 and "<td>" in table[6]
    assert expected["pages"] == [(1, 612.0, 792.0), (2, 612.0, 792.0)]

    first = host.pipeline(pipeline_body("layout", [route(PDF, "docling", **options)]))
    assert host.parse_run(doc["id"], first)["stats"]["cache_hit"] is False
    assert layout(host.parsed(doc["id"], first)) == expected

    # Read again out of the parse cache: the same parse comes back with nothing lost.
    assert host.retry_parse(doc["id"], first)["stats"]["cache_hit"] is True
    assert layout(host.parsed(doc["id"], first)) == expected

    # A second pipeline that differs only after the parse step shares that parse and shows the same page.
    second = host.pipeline(pipeline_body("layout-small-chunks", [route(PDF, "docling", **options)], chunk_chars=200))
    assert host.parse_node_fp(second) == host.parse_node_fp(first)
    assert layout(host.parsed(doc["id"], second)) == expected
    assert len(fakes.parses()) == 1


# ---- an engine that refuses the file --------------------------------------------------------------


@JOBS
def test_a_shim_that_refuses_the_media_type_fails_the_parse_for_good(
    host: Host, fakes: ShimFarm, use_jobs: bool
) -> None:
    fakes.parser_async = use_jobs
    fakes.refuse = {"image/gif"}
    host.add_parser("docling")
    gif = host.upload("anim.gif", b"gif bytes", "image/gif")
    png = host.upload("scan.png", b"png bytes", "image/png")
    pipeline_id = host.pipeline(pipeline_body("images", [route("image/*", "docling")]))

    run = host.parse_run(gif["id"], pipeline_id)
    assert run["status"] == "failed"
    assert run["error_code"] == "provider_error"
    assert run["attempt"] == 1  # a 415 is permanent, so it is not retried
    assert "docling" in run["error_message"] and "415" in run["error_message"]
    state = host.settle(pipeline_id)
    assert state["status"] == "degraded"
    assert [r["id"] for r in state["recent_failures"]] == [run["id"]]
    assert not_parsed(host, gif, pipeline_id)
    assert sorted(c.filename or "" for c in fakes.parses()) == ["anim.gif", "scan.png"]  # asked once each

    assert host.parse_run(png["id"], pipeline_id)["status"] == "succeeded"
    assert reader(host, png, pipeline_id) == LABEL_A
