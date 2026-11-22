"""Build check: Docling's automatic OCR engine reads text, with what is already installed.

The Dockerfile runs it without a network, so it also fails when the engine would have to download
models on the first scan. It tests the OCR engine alone; the layout models are not needed.
"""

from docling.datamodel.accelerator_options import AcceleratorOptions
from docling.datamodel.pipeline_options import OcrAutoOptions
from docling.models.stages.ocr.auto_ocr_model import OcrAutoModel
from PIL import Image, ImageDraw, ImageFont

EXPECTED = "4827"


def main() -> None:
    auto = OcrAutoModel(True, None, OcrAutoOptions(), AcceleratorOptions())
    if auto._engine is None:
        raise SystemExit("Docling found no OCR engine")
    page = Image.new("RGB", (700, 160), "white")
    ImageDraw.Draw(page).text((30, 40), f"PUMP {EXPECTED}", fill="black", font=ImageFont.load_default(size=64))
    reader = getattr(auto._engine, "reader", None)
    if reader is None:
        raise SystemExit(f"expected RapidOCR, got {type(auto._engine).__name__}")
    read = " ".join(reader(page).txts or ())
    if EXPECTED not in read:
        raise SystemExit(f"OCR did not read the test text, got {read!r}")
    print(f"OCR works: {type(auto._engine).__name__} read {read!r}")


if __name__ == "__main__":
    main()
