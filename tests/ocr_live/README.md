# OCR verification

These tests distinguish service contracts from actual recognition. Offline tests use synthetic
services; live probes submit the committed fixtures to real engine URLs and measure the resulting
Document IR against `truth.json`. Engine URLs may be local, container-based, or remote.

## Offline checks

```sh
uv run pytest tests/ocr_live tests/test_ocr_options_contract.py tests/test_ocr_routing_contract.py tests/test_parser_image_sample.py
```

The self-tests include services that ignore options or return unrelated text. Paired predicates
must reject an unchanged control where an improvement is required. A `record` observation never
fails a run; inspect its `met` field before treating it as evidence for a claim.

## Real engine probes

Start the chosen shim as described in its README, then run from the repository root:

```sh
uv run python tests/ocr_live/runner.py --engine surya=http://127.0.0.1:8704 --tier C --report surya.json
uv run python tests/ocr_live/runner.py --engine paddleocr=http://127.0.0.1:8703 --tier C --report paddleocr.json
uv run python tests/ocr_live/runner.py --engine docling=http://127.0.0.1:8701 --tier C --report docling.json
```

Use engine names `docling`, `doctr`, `easyocr`, `mineru`, `paddleocr`, or `surya`. The teaching
Tesseract service uses `tesseract`. `--list` shows the selected matrix, and repeated `--probe GLOB`
flags narrow a run. Tiers are cumulative: A checks validation, B checks basic recognition, and C
adds specialty and strategy comparisons. Reports preserve the engine version, options, controls,
measurements, and skipped requirements. Exit zero means the executed gates passed; it does not
mean skipped models or recorded quality claims were verified.

Run heavy engines one at a time. Changing strategies can load different models; allow enough RAM,
swap, and disk space for the selected models. Do not use these smoke-run timings to rank engines.

MinerU's default standard-tier scan baseline needs models. To verify its model-free text-layer PDF
and native DOCX/HTML/CSV support separately:

```sh
uv run python tests/ocr_live/runner.py --engine mineru=http://127.0.0.1:8702 --tier C --probe 'mineru.*' --report mineru.json
```

Set `OCR_LIVE_MINERU_MODELS=1` only after installing its basic/standard/advanced models. PaddleOCR's
extra language/chart/seal models require `OCR_LIVE_PADDLE_EXTRA_MODELS=1`; its VL probe requires
`OCR_LIVE_PADDLE_VL=1`. A seal loading probe does not measure seal recognition quality. Other
requirements appear in `--list` and in skipped report entries.

## Real host strategy loop

With the teaching Tesseract shim running:

```sh
OCR_LIVE_WORKFLOW_URL=http://127.0.0.1:18710 uv run pytest tests/ocr_live/test_user_workflow_live.py -q
```

This uses real outbound HTTP, workers, ingestion, query, and the parse cache. It verifies that an
image reaches OCR while plain text uses the builtin parser, route options reach the shim, changing
options creates a new parse fingerprint, and returning to the original strategy reuses its parse.
For another engine, also set `OCR_LIVE_WORKFLOW_OPTIONS` and
`OCR_LIVE_WORKFLOW_CHANGED_OPTIONS` to two distinct, valid JSON option objects.

The browser variant specifically expects Tesseract's `lang` option and installed `eng`/`osd`
language packs. After `npm --prefix ui ci` and `npm --prefix ui run build`:

```sh
OCR_LIVE_WORKFLOW_URL=http://127.0.0.1:18710 npm --prefix ui run e2e -- ocr-engine.spec.ts
```

It uploads the real scan, queries the first pipeline, edits `lang` through the schema form, creates
a new pipeline, and searches its OCR output. The ordinary browser suite uses a synthetic parser
service and skips this opt-in test.
