"""Probes for the Tesseract example shim (``examples/shimkit/example/tesseract_shim.py``): ``--engine tesseract=URL``.

The shim reads images only, returns one ``paragraph`` block per Tesseract paragraph, and has one option, ``lang``.
The runner also loads ``baseline.py`` for it (the unknown option, ``gif`` and the clean scan).

A German pack is needed for the ``lang`` probes. Tesseract looks for packs in ``$TESSDATA_PREFIX``, so point the
shim and the runner at a directory with ``eng.traineddata`` and ``deu.traineddata`` (the shim reads the variable
when it starts). The probes ask for ``env:TESSDATA_PREFIX``. When the pack is in Tesseract's default directory, or
the shim runs on another machine, pass ``--has env:TESSDATA_PREFIX`` to run them.
"""

from __future__ import annotations

import predicates as P
from probe import Check, Observation, Probe, Verdict

DEU = ("env:TESSDATA_PREFIX",)  # a German pack outside Tesseract's default directory, see the module docstring


def only_paragraphs() -> Check:
    """There is at least one block and every block is a ``paragraph``: Tesseract has no layout analysis."""

    def fn(obs: Observation) -> Verdict:
        types = sorted({str(b.get("type")) for b in P.blocks_of(obs.run.body)})
        return Verdict(types == ["paragraph"], f"block types {types}", {"block_types": types})

    return Check("only_paragraphs", "structure", fn)


def umlaut_recall_pair(treatment_min: float, control_max: float, ratio: float = 0.9) -> Check:
    """The words that carry an umlaut or an eszett (``truth.extras["umlaut_words"]``) are read with the option and
    mis-read without it. The usual 0.8 token ratio forgives ``prufung`` for ``prüfung``, so this uses ``ratio``."""

    def fn(obs: Observation) -> Verdict:
        assert obs.control is not None, "umlaut_recall_pair needs a control run"
        words = obs.truth.extras["umlaut_words"]
        on = P.recall(words, P.result_tokens(obs.run.body), ratio)
        off = P.recall(words, P.result_tokens(obs.control.body), ratio)
        return Verdict(
            on >= treatment_min and off <= control_max,
            f"umlaut words at ratio {ratio}: {on:.2f} with the option (needs {treatment_min:.2f}), "
            f"{off:.2f} without (allowed {control_max:.2f})",
            {"umlaut_recall": round(on, 4), "umlaut_control_recall": round(off, 4)},
        )

    return Check("umlaut_recall_pair", "recall", fn)


def read_in_format(fixture: str, note: str) -> Probe:
    return Probe(
        id=f"tesseract.format_{fixture.rsplit('.', 1)[1]}",
        engine="tesseract",
        fixture=fixture,
        predicate=P.recall_at_least(0.8),
        tier="C",
        note=note,
    )


PROBES = [
    Probe(
        id="tesseract.clean_scan",
        engine="tesseract",
        fixture="scan_clean.png",
        predicate=P.all_of(
            "clean_scan_paragraphs_on_one_page",
            P.recall_at_least(0.8),
            only_paragraphs(),
            P.page_set_equals({1}),
        ),
        tier="B",
        note="Plain OCR of an image: the text is read, every block is a paragraph, and there is one page.",
    ),
    Probe(
        id="tesseract.lang_deu_reads_german",
        engine="tesseract",
        fixture="scan_german.png",
        options={"lang": "deu"},
        control={"lang": "eng"},
        predicate=P.all_of(
            "deu_beats_eng_on_german",
            P.recall_above_control(margin=0.01, treatment_min=0.9),
            umlaut_recall_pair(treatment_min=0.9, control_max=0.5),
        ),
        requires=DEU,
        tier="B",
        note="The German pack reads the umlauts and the eszett that the English pack garbles.",
    ),
    Probe(
        id="tesseract.pdf_unsupported",
        engine="tesseract",
        fixture="report_born_digital.pdf",
        expect_status=415,
        expect_code="unsupported_media_type",
        tier="A",
        note="The example reads images only, so a PDF is refused rather than rendered.",
    ),
    read_in_format("scan_clean.jpg", "jpeg is in the shim's list of images."),
    read_in_format("scan_clean.webp", "webp is in the shim's list of images."),
    read_in_format("scan_clean.bmp", "bmp is in the shim's list of images."),
    Probe(
        id="tesseract.multi_frame_tiff_reads_every_frame",
        engine="tesseract",
        fixture="multi_frame.tiff",
        predicate=P.all_of("every_frame_is_a_page", P.recall_at_least(0.8), P.page_set_equals({1, 2, 3})),
        gate="record",
        tier="C",
        note="Records how many TIFF frames are read: the numbers hold the pages that carry blocks and the recall.",
    ),
]
