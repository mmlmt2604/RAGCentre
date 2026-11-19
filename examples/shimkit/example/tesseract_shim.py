"""A complete ragcentre.parser/v1 service for Tesseract, in about forty lines.

    pip install ./examples/shimkit pytesseract pillow uvicorn      (and the tesseract program)
    uvicorn tesseract_shim:app --port 8710
"""

import io

import pytesseract
from PIL import Image
from shimkit import ShimError, build_ir, create_app, file_extension, parse_options, string

OPTIONS = [string("lang", "eng", "Tesseract language codes, for example eng or eng+deu")]
IMAGES = {"png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp"}


def parse(content: bytes, filename: str, options: dict) -> dict:
    opts = parse_options(OPTIONS, options)
    if file_extension(filename) not in IMAGES:
        raise ShimError(415, "unsupported_media_type", f"{filename}: this service reads images only")
    image = Image.open(io.BytesIO(content))
    words = pytesseract.image_to_data(image, lang=opts["lang"], output_type=pytesseract.Output.DICT)
    paragraphs: dict[tuple, list[int]] = {}  # (block, paragraph) -> indexes of its words, in reading order
    for i, text in enumerate(words["text"]):
        if text.strip():
            paragraphs.setdefault((words["block_num"][i], words["par_num"][i]), []).append(i)
    blocks = []
    for indexes in paragraphs.values():
        boxes = [(words["left"][i], words["top"][i], words["width"][i], words["height"][i]) for i in indexes]
        blocks.append(
            {
                "type": "paragraph",
                "text": " ".join(words["text"][i] for i in indexes),
                "page": 1,
                "bbox": [min(x for x, _, _, _ in boxes), min(y for _, y, _, _ in boxes),
                         max(x + w for x, _, w, _ in boxes), max(y + h for _, y, _, h in boxes)],
            }
        )  # fmt: skip
    pages = [{"page": 1, "width": image.width, "height": image.height}]
    return build_ir(blocks, pages, source_name=filename)


app = create_app(
    name="tesseract-shim",
    version=lambda: f"0.1.0+tesseract.{pytesseract.get_tesseract_version()}",  # RAGCentre pins this
    mime_types=["image/png", "image/jpeg", "image/tiff", "image/bmp", "image/webp"],
    capabilities={"page_images": False},
    api_key_env="TESSERACT_SHIM_API_KEY",
    parse=parse,
    options=OPTIONS,
    serialise=True,  # one document at a time
    jobs=True,  # parse in the background, so a slow scan is no problem
)
