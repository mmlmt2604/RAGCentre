"""A ``ragcentre.parser/v1`` service that parses documents with the Surya OCR 2.5 model.

    GET  /info   service name, version and supported MIME types
    POST /parse  multipart ``file`` and ``options`` (JSON) -> Document IR

Options (from the pipeline's parse route):

    include_page_images  return each rendered page as a base64 PNG (default false)
    ocr_figures          also read text inside pictures, figures and diagrams (default false)
    dpi                  render resolution for PDF pages, 72 to 400 (default 192)
    page_range           [first, last], 1-based and inclusive (default: the whole file)

There is no ``full_page`` option: the shim never gives Surya layout results, so Surya always reads
whole pages (``full_page=False`` would raise).

Run it with::

    uvicorn surya_shim.app:app --port 8704

Surya runs its model in an inference server that it starts on first use (``llama-server`` from
llama.cpp on CPU and Apple Silicon, ``vllm`` in Docker on NVIDIA GPUs), or attaches to one named by
``SURYA_INFERENCE_BACKEND`` and ``SURYA_INFERENCE_URL``.

Environment: ``SURYA_SHIM_API_KEY`` requires ``Authorization: Bearer <key>`` (except on
``GET /info``).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from fastapi import FastAPI
from pydantic import Field
from shimkit import PAGE_RANGE_DESCRIPTION, PageRange, ShimError, ShimOptions, file_extension
from shimkit import create_app as create_shim_app

from surya_shim.convert import to_ir
from surya_shim.engine import DEFAULT_DPI, SUPPORTED_EXTENSIONS, SuryaEngine

SHIM_VERSION = "0.3.1"
# Defaults of surya.settings (0.24.0) for the settings that choose which weights a backend loads.
DEFAULT_GGUF_REPO = "datalab-to/surya-ocr-2.5-gguf"  # SURYA_GGUF_REPO
DEFAULT_GGUF_FILE = "surya-2.5.gguf"  # SURYA_GGUF_MODEL_FILE
DEFAULT_MMPROJ_FILE = "surya-2.5-mmproj.gguf"  # SURYA_GGUF_MMPROJ_FILE
NAME = "surya-shim"
MIME_TYPES = [
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/tiff",
    "image/bmp",
    "image/webp",
]


class Options(ShimOptions):
    include_page_images: bool = Field(False, description="Return each rendered page as a base64 PNG.")
    ocr_figures: bool = Field(False, description="Also read text inside pictures, figures and diagrams.")
    dpi: int = Field(DEFAULT_DPI, ge=72, le=400, description="Render resolution for PDF pages.")
    page_range: PageRange | None = Field(
        None,
        description=PAGE_RANGE_DESCRIPTION + " Pages are numbered as in the file. Default: the whole file.",
    )


# (content, filename, options) -> reduced pages (see convert.py)
Engine = Callable[[bytes, str, dict[str, Any]], list[dict[str, Any]]]


def _surya_version() -> str:
    try:
        return version("surya-ocr")
    except PackageNotFoundError:
        return "unknown"


def _model_name() -> str | None:
    """The last path segment of ``SURYA_MODEL_CHECKPOINT`` (the model decides the output as much as
    the package does). ``surya.settings`` is light: the ``surya`` package imports lazily."""
    try:
        from surya.settings import settings

        return _leaf(str(settings.SURYA_MODEL_CHECKPOINT)) or None
    except Exception:
        return None


def _suffix(value: str) -> str:
    """``value`` with every character outside ``[A-Za-z0-9_-]`` replaced by ``-``, except that a ``/``
    becomes ``--`` so a repo id or a file in a folder (``a/b``) does not read the same as ``a-b``."""
    return re.sub(r"[^A-Za-z0-9_-]", "-", value.replace("/", "--"))


def _backend() -> str | None:
    """The inference backend Surya will use: ``SURYA_INFERENCE_BACKEND`` if set, else what Surya's
    own auto-detection picks (vLLM on an NVIDIA GPU, otherwise llama.cpp). ``None`` when Surya is not
    importable. ``surya.inference._autodetect_backend`` is private; if it goes missing the backend
    is reported as ``auto``."""
    try:
        from surya.settings import settings

        if settings.SURYA_INFERENCE_BACKEND:
            return str(settings.SURYA_INFERENCE_BACKEND).lower()
    except Exception:
        return None
    try:
        from surya.inference import _autodetect_backend

        return str(_autodetect_backend()).lower()
    except Exception:
        return "auto"


def _leaf(path: str) -> str:
    """The last segment of a path or repo id, with either kind of separator."""
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _parent(path: str) -> str:
    """The name of the folder that holds ``path`` (empty for a bare file name)."""
    parts = path.replace("\\", "/").rstrip("/").split("/")
    return parts[-2] if len(parts) > 1 else ""


def _setting(settings: Any, name: str) -> str | None:
    """One setting as text, or ``None`` if it is unset or this Surya release no longer has it. Each
    setting is read on its own, so one that goes missing drops only its own suffix."""
    value = getattr(settings, name, None)
    return str(value) if value else None


def _llamacpp_weights() -> list[str]:
    """Suffixes for the llama.cpp weights when they are not the default ones, in the order repo,
    model file, projector file. Read from ``surya/inference/backends/llamacpp.py``: Surya uses
    ``SURYA_GGUF_LOCAL_MODEL_PATH`` and ``SURYA_GGUF_LOCAL_MMPROJ_PATH`` only together, and then
    fetches nothing, so the repo counts only when the pair is not set.

    Without the pair, a file is named by its setting (the whole value: it is a file name inside the
    Hugging Face repo and may include a folder). With the pair, it is named by the base name of its
    path; a local file that has the default name is still not the default weights (it can be a
    fine-tune saved under that name), so it gets ``gguflocal`` or ``mmprojlocal`` and the name of
    its folder."""
    try:
        from surya.settings import settings
    except Exception:
        return []
    local_model = _setting(settings, "SURYA_GGUF_LOCAL_MODEL_PATH")
    local_mmproj = _setting(settings, "SURYA_GGUF_LOCAL_MMPROJ_PATH")
    local = bool(local_model and local_mmproj)
    out: list[str] = []
    if not local and (repo := _setting(settings, "SURYA_GGUF_REPO")) and repo != DEFAULT_GGUF_REPO:
        out.append(f"ggufrepo-{_suffix(repo)}")
    for tag, local_path, setting, default in (
        ("gguf", local_model, "SURYA_GGUF_MODEL_FILE", DEFAULT_GGUF_FILE),
        ("mmproj", local_mmproj, "SURYA_GGUF_MMPROJ_FILE", DEFAULT_MMPROJ_FILE),
    ):
        if local and local_path:
            name = _leaf(local_path)
            if name != default:
                out.append(f"{tag}-{_suffix(name)}")
            else:
                folder = _parent(local_path)
                out.append(f"{tag}local-{_suffix(folder)}" if folder else f"{tag}local")
        elif (name := _setting(settings, setting)) and name != default:
            out.append(f"{tag}-{_suffix(name)}")
    return out


def _mlx_model() -> str | None:
    """The MLX checkpoint (``SURYA_MLX_MODEL``: a directory or a Hugging Face repo), by its last
    path segment. Only the MLX backend reads it (``surya/inference/backends/mlx.py``)."""
    try:
        from surya.settings import settings

        return (_leaf(str(settings.SURYA_MLX_MODEL)) or None) if settings.SURYA_MLX_MODEL else None
    except Exception:
        return None


def service_version() -> str:
    # RAGCentre pins this when the provider is created. A Surya upgrade, a different model, the
    # inference backend and the weights that backend loads all change the output, so all are part
    # of the version. Settings that only tune the runtime are left out on purpose (README).
    version_ = f"{SHIM_VERSION}+surya.{_surya_version()}"
    if model := _model_name():
        version_ += f".{model}"
    if backend := _backend():
        version_ += f".{_suffix(backend)}"
        if backend == "llamacpp":
            for part in _llamacpp_weights():
                version_ += f".{part}"
        elif backend == "mlx" and (mlx := _mlx_model()):
            version_ += f".mlx-{_suffix(mlx)}"
    return version_


def create_app(engine: Engine | None = None) -> FastAPI:
    run = engine or SuryaEngine()

    def parse(content: bytes, filename: str, options: Options) -> dict[str, Any]:
        opts = options.model_dump()
        if file_extension(filename) not in SUPPORTED_EXTENSIONS:
            raise ShimError(415, "unsupported_media_type", f"{filename}: Surya reads PDF and image files only")
        pages = run(content, filename, opts)
        return to_ir(pages, source_name=filename, include_page_images=opts["include_page_images"])

    return create_shim_app(
        name=NAME,
        version=service_version,
        mime_types=MIME_TYPES,
        capabilities={"page_images": True},
        api_key_env="SURYA_SHIM_API_KEY",
        parse=parse,
        options_model=Options,
        serialise=False,  # a client of the inference server, which does its own batching
        shutdown=getattr(run, "close", None),
    )


def __getattr__(name: str) -> Any:
    # ``uvicorn surya_shim.app:app`` builds the real app lazily, so importing this module (in
    # tests) does not import Surya.
    if name == "app":
        return create_app()
    raise AttributeError(name)
