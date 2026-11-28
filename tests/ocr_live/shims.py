"""In-process ``ragcentre.parser/v1`` shims that stand in for an engine. They check the runner, not an engine.

``oracle_app``   returns the fixture's ground truth as a Document IR, validates its options against the schema it
                 is given (none by default, so any option is a ``400 invalid_options``), and refuses file types
                 it does not take with ``415``. Pass ``transform`` to model what an option does to the result.
``null_app``     the oracle that ignores options: it declares no schema and never reads ``options``. A probe that
                 sends an option on and off and expects different results must FAIL on it.
``garbage_app``  returns text unrelated to every fixture. Every recall probe must FAIL on it.

A fixture is recognised by the SHA-256 of the uploaded bytes, so a shim answers for the real fixture files only.
Built on ``examples/shimkit`` (a path dependency of the example shims, not installed): the folder is put on
``sys.path`` here.

    uv run python tests/ocr_live/shims.py oracle --port 8791        # serve one on a port for the runner
"""

from __future__ import annotations

import argparse
import hashlib
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

import uvicorn

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "examples" / "shimkit"))

from probe import FixtureTruth, load_truth  # noqa: E402
from shimkit import Option, ShimError, build_ir, create_app, file_extension  # noqa: E402

IMAGE_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff"})
MIME_BY_EXTENSION = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "bmp": "image/bmp",
    "tif": "image/tiff",
    "tiff": "image/tiff",
    "pdf": "application/pdf",
}
GARBAGE = (
    "Sourdough starters need flour, water, and patience. Whisk cinnamon, sugar, and butter until fluffy, then fold "
    "chopped walnuts into the batter before baking. Penguins waddle across glaciers while volcanoes rumble "
    "beneath violet skies."
)

# (the IR for the fixture, the request's options, the fixture's truth) -> the IR to return
Transform = Callable[[dict[str, Any], dict[str, Any], FixtureTruth], dict[str, Any]]


def table_html(text: str) -> str:
    rows = [line.split(" | ") for line in text.split("\n")]
    return "<table>" + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows) + "</table>"


def truth_ir(truth: FixtureTruth, *, lines: bool = False) -> dict[str, Any]:
    """The Document IR a perfect engine returns for ``truth``: its blocks, or with ``lines`` one paragraph per line."""
    blocks: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    for page in truth.pages:
        if page.width and page.height:
            pages.append({"page": page.page, "width": page.width, "height": page.height})
        else:
            pages.append({"page": page.page})
        items = (
            [{"type": "paragraph", "text": ln.text, "bbox": ln.bbox} for ln in page.lines]
            if lines
            else [
                {"type": b.type, "text": b.text, "bbox": b.bbox, "level": b.level, "html": b.html} for b in page.blocks
            ]
        )
        for item in items:
            block: dict[str, Any] = {"type": item["type"], "text": item["text"], "page": page.page}
            if any(item["bbox"]):
                block["bbox"] = list(item["bbox"])
            if item.get("level") is not None:
                block["level"] = item["level"]
            if item.get("html"):
                block["html"] = table_html(item["text"])
            blocks.append(block)
    return build_ir(blocks, pages, source_name=truth.name)


def _parse_function(
    truth: dict[str, FixtureTruth], extensions: frozenset[str], lines: bool, transform: Transform | None
) -> Callable[[bytes, str, dict[str, Any]], dict[str, Any]]:
    by_hash = {t.sha256: t for t in truth.values()}

    def parse(content: bytes, filename: str, options: dict[str, Any]) -> dict[str, Any]:
        if file_extension(filename) not in extensions:
            raise ShimError(415, "unsupported_media_type", f"{filename}: this shim does not read that type")
        known = by_hash.get(hashlib.sha256(content).hexdigest())
        if known is None:
            raise ShimError(422, "conversion_failed", f"{filename}: not one of the fixtures")
        ir = truth_ir(known, lines=lines)
        return transform(ir, options, known) if transform else ir

    return parse


def oracle_app(
    *,
    options: Sequence[Option] | None = (),
    transform: Transform | None = None,
    lines: bool = False,
    extensions: frozenset[str] = IMAGE_EXTENSIONS | {"pdf"},
    jobs: bool = False,
    name: str = "oracle-shim",
) -> Any:
    """``options`` is the declared schema: ``()`` accepts none, ``None`` declares no schema and ignores options."""
    return create_app(
        name=name,
        version=lambda: f"0.0.0+{name}",
        mime_types=sorted({MIME_BY_EXTENSION[e] for e in extensions if e in MIME_BY_EXTENSION}),
        capabilities={"page_images": False},
        api_key_env="OCR_LIVE_SHIM_API_KEY",
        parse=_parse_function(load_truth(), extensions, lines, transform),
        options=options,
        jobs=jobs,
    )


def null_app(**kwargs: Any) -> Any:
    """The oracle that ignores options: no schema, and ``transform`` is dropped."""
    kwargs.pop("transform", None)
    return oracle_app(options=None, name="null-shim", **kwargs)


def garbage_app(*, jobs: bool = False) -> Any:
    def parse(content: bytes, filename: str, options: dict[str, Any]) -> dict[str, Any]:
        if file_extension(filename) not in IMAGE_EXTENSIONS | {"pdf"}:
            raise ShimError(415, "unsupported_media_type", f"{filename}: this shim does not read that type")
        sentences = [s.strip() + "." for s in GARBAGE.split(".") if s.strip()]
        return build_ir(
            [{"type": "paragraph", "text": s, "page": 1} for s in sentences], [{"page": 1}], source_name=filename
        )

    return create_app(
        name="garbage-shim",
        version=lambda: "0.0.0+garbage-shim",
        mime_types=sorted(set(MIME_BY_EXTENSION.values())),
        capabilities={"page_images": False},
        api_key_env="OCR_LIVE_SHIM_API_KEY",
        parse=parse,
        options=None,
        jobs=jobs,
    )


SHIMS: dict[str, Callable[[], Any]] = {"oracle": oracle_app, "null": null_app, "garbage": garbage_app}


@contextmanager
def serving(app: Any, host: str = "127.0.0.1", port: int = 0) -> Iterator[str]:
    """Serve ``app`` on a loopback port in a thread and yield its URL. ``port`` 0 picks a free one."""
    if port == 0:
        with socket.socket() as probe_socket:
            probe_socket.bind((host, 0))
            port = probe_socket.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError(f"the shim did not start on {host}:{port}")
        time.sleep(0.02)
    try:
        yield f"http://{host}:{port}"
    finally:
        server.should_exit = True
        thread.join(10)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve a runner self-test shim.")
    parser.add_argument("shim", choices=sorted(SHIMS))
    parser.add_argument("--port", type=int, default=8791)
    args = parser.parse_args(argv)
    with serving(SHIMS[args.shim](), port=args.port) as url:
        print(f"{args.shim} shim at {url}  (Ctrl-C to stop)", flush=True)
        with suppress(KeyboardInterrupt):
            threading.Event().wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
