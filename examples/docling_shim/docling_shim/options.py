"""The parse route's options: validation, the JSON Schema that ``/info`` reports, and the checks on
the ``engine`` passthrough. Needs pydantic only (no Docling), so it is cheap to import.

This shim is self-contained and does not use ``shimkit``; the helpers here are written the same way.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError
from pydantic_core import ErrorDetails


class OptionsError(Exception):
    """A ``400 invalid_options`` failure: the message names the option."""


def _check_page_range(value: list[int] | None) -> list[int] | None:
    if value is not None and value[0] > value[1]:
        raise ValueError("must be [first, last] with first not after last")
    return value


# ``[first, last]``: 1-based, inclusive (Docling's ``convert(page_range=...)``).
PageRange = Annotated[
    list[Annotated[int, Field(ge=1)]],
    Field(min_length=2, max_length=2),
    AfterValidator(_check_page_range),
]

OcrEngine = Literal["auto", "easyocr", "rapidocr", "tesseract", "tesseract_cli"]
# Docling's VLM presets (``VlmConvertOptions.list_preset_ids()``) are a closed set of 28 names, many of
# which need ``trust_remote_code``, a remote server or a specific runtime. Only the DocTags presets,
# whose output Docling turns into a ``DoclingDocument`` with boxes, are offered.
VlmModel = Literal["granite_docling", "smoldocling"]
# Options of the standard pipeline that mean nothing to the VLM pipeline: given with it they are refused.
STANDARD_ONLY = (
    "ocr", "tables", "table_mode", "ocr_engine", "languages", "force_full_page_ocr", "formulas", "code",
)  # fmt: skip


class Options(BaseModel):
    """Unknown keys are rejected and types are strict, so ``"false"`` is not a boolean."""

    model_config = ConfigDict(extra="forbid", strict=True)

    pipeline: Literal["standard", "vlm"] = Field(
        "standard",
        description="standard is Docling's layout, OCR and table pipeline; vlm has a vision-language model read "
        "each page (slow, needs a model download). The vlm pipeline takes none of: " + ", ".join(STANDARD_ONLY) + ".",
    )
    vlm_model: VlmModel = Field(
        "granite_docling", description="The model of the vlm pipeline (a Docling preset). Only with pipeline vlm."
    )
    ocr: bool = Field(True, description="Run OCR on scanned pages and images.")
    include_page_images: bool = Field(False, description="Return each rendered page as a base64 PNG.")
    page_range: PageRange | None = Field(
        None, description="Parse only these pages: [first, last], 1-based and inclusive. Default: the whole file."
    )
    languages: list[str] | None = Field(
        None,
        description="OCR languages, in the OCR engine's own codes (en for EasyOCR, eng for Tesseract). "
        "Default: the engine's own default.",
    )
    tables: bool = Field(True, description="Recognise table structure.")
    table_mode: Literal["fast", "accurate"] = Field(
        "accurate", description="Table recognition model: fast or accurate."
    )
    ocr_engine: OcrEngine = Field(
        "auto",
        description="OCR engine. auto picks the first installed of the engines Docling tries; tesseract is "
        "tesserocr (Python bindings) and tesseract_cli runs the tesseract program.",
    )
    force_full_page_ocr: bool = Field(
        False, description="OCR the whole page, even where there is a text layer (Docling OcrMode full_page)."
    )
    formulas: bool = Field(False, description="Recognise formulas and write them as LaTeX.")
    code: bool = Field(False, description="Recognise code blocks and their language.")
    picture_classification: bool = Field(
        False, description="Classify pictures (chart, photo, logo, ...); the class is kept in block metadata."
    )
    picture_description: bool = Field(
        False,
        description="Describe pictures with a vision model: Docling's local one, or the service's configured one.",
    )
    charts: bool = Field(False, description="Extract the data of charts.")
    images_scale: float = Field(1.0, gt=0, le=4, description="Scale of the page images Docling renders.")
    engine: dict[str, Any] = Field(
        default_factory=dict,
        description="Docling PdfPipelineOptions fields to set (merged over the named options), for example "
        '{"table_structure_options": {"do_cell_matching": false}}. Operational fields (paths, URLs, devices, batch sizes) are refused. '
        "See https://docling-project.github.io/docling/reference/pipeline_options/",
    )


# --- passthrough checks --------------------------------------------------------------------------

DENIED_KEYS = frozenset(
    {
        "artifacts_path", "enable_remote_services", "allow_external_plugins", "accelerator_options",
        "document_timeout", "url", "headers", "api_key", "params", "kwargs", "input",
        "queue_max_size", "batch_polling_interval_seconds", "stage_shutdown_timeout_seconds",
        "path", "tesseract_cmd", "batch_size", "concurrency", "timeout", "num_threads", "device",
        "use_gpu",
    }
)  # fmt: skip
DENIED_SUFFIXES = ("_path", "_dir", "_directory", "_batch_size")

# Every field of Docling's ``PdfPipelineOptions`` is in exactly one of these places (the live test
# ``test_engine_parameters_accounted_for`` compares them with the installed Docling, so an upgrade
# that adds a field fails it until someone decides where the field belongs).
NAMED_FIELDS = frozenset(
    {
        "do_ocr", "generate_page_images", "do_table_structure", "table_structure_options", "ocr_options",
        "do_formula_enrichment", "do_code_enrichment", "do_picture_classification", "do_picture_description",
        "do_chart_extraction", "images_scale",
    }
)  # fmt: skip
# Only in ``VlmPipelineOptions`` (the vlm pipeline): chosen by the option ``vlm_model``.
VLM_ONLY_FIELDS = frozenset({"vlm_options"})
# Settable through ``engine`` (``table_structure_options`` and ``ocr_options`` partly by named options).
ENGINE_FIELDS = frozenset(
    {
        "table_structure_options", "ocr_options", "picture_classification_options", "picture_description_options",
        "chart_extraction_options", "generate_picture_images", "extract_form_fields", "force_backend_text",
        "layout_options", "code_formula_options", "use_embedded_picture_images", "generate_table_images",
        "generate_parsed_pages", "use_reading_order_separators", "heading_hierarchy_options",
        "script_position_options",
    }
)  # fmt: skip
# Deliberately not exposed: all of them are refused by the denylist below.
NOT_EXPOSED = {
    "document_timeout": "operational: a deadline is a service setting",
    "accelerator_options": "operational: the device and thread count are DOCLING_SHIM_DEVICE and _NUM_THREADS",
    "enable_remote_services": "set by the service when a picture-description server is configured",
    "allow_external_plugins": "would let a pipeline author load code",
    "artifacts_path": "operational: where the models are",
    "ocr_batch_size": "operational: a batch size",
    "layout_batch_size": "operational: a batch size",
    "table_batch_size": "operational: a batch size",
    "batch_polling_interval_seconds": "operational: pipeline queue tuning",
    "queue_max_size": "operational: pipeline queue tuning",
    "stage_shutdown_timeout_seconds": "operational: pipeline queue tuning",
}

# named option -> the ``engine`` paths that set the same thing
OVERLAPS: dict[str, tuple[tuple[str, ...], ...]] = {
    "ocr": (("do_ocr",),),
    "include_page_images": (("generate_page_images",),),
    "tables": (("do_table_structure",),),
    "table_mode": (("table_structure_options", "mode"),),
    "languages": (("ocr_options", "lang"),),
    "force_full_page_ocr": (("ocr_options", "mode"),),
    "formulas": (("do_formula_enrichment",),),
    "code": (("do_code_enrichment",),),
    "picture_classification": (("do_picture_classification",),),
    "picture_description": (("do_picture_description",),),
    "charts": (("do_chart_extraction",),),
    "images_scale": (("images_scale",),),
}


def denied_key(value: Any, prefix: str = "") -> str | None:
    """The first denied key in ``value``, as a dotted path, at any depth (objects and lists)."""
    if isinstance(value, Mapping):
        for key, inner in value.items():
            name = str(key)
            if name in DENIED_KEYS or name.endswith(DENIED_SUFFIXES):
                return f"{prefix}{name}"
            if found := denied_key(inner, f"{prefix}{name}."):
                return found
    elif isinstance(value, list | tuple):
        for item in value:
            if found := denied_key(item, prefix):
                return found
    return None


def _has_path(value: Mapping[str, Any], path: tuple[str, ...]) -> bool:
    for key in path:
        if not isinstance(value, Mapping) or key not in value:
            return False
        value = value[key]
    return True


def check_engine(options: Options) -> None:
    """Refuse a denied key anywhere in ``engine`` and any setting that a named option also sets."""
    engine = options.engine
    if options.pipeline == "vlm":
        for name in STANDARD_ONLY:
            if name in options.model_fields_set:
                raise OptionsError(f"option {name!r} does not apply to the vlm pipeline")
    elif "vlm_model" in options.model_fields_set:
        raise OptionsError("option 'vlm_model' needs the option pipeline set to 'vlm'")
    if not engine:
        return
    if "vlm_options" in engine:
        raise OptionsError("option 'engine' sets 'vlm_options'; use the option 'vlm_model' instead")
    if key := denied_key(engine):
        raise OptionsError(f"option 'engine' may not set {key!r}: it is a service setting, not a parse option")
    for name in options.model_fields_set:
        for path in OVERLAPS.get(name, ()):
            if _has_path(engine, path):
                raise OptionsError(
                    f"option 'engine' sets {'.'.join(path)!r}, which option {name!r} already sets; use one of them"
                )


# --- error messages ------------------------------------------------------------------------------


def _bounds(key: str) -> dict[str, Any]:
    field = Options.model_fields.get(key)
    found: dict[str, Any] = {}
    for item in field.metadata if field else []:
        for name in ("ge", "gt", "le", "lt"):
            if hasattr(item, name):
                found[name] = getattr(item, name)
    return found


def _range(noun: str, bounds: dict[str, Any]) -> str:
    low = bounds.get("ge", bounds.get("gt"))
    high = bounds.get("le", bounds.get("lt"))
    if "ge" in bounds and "le" in bounds:
        return f"{noun} from {low} to {high}"
    parts = []
    if "ge" in bounds:
        parts.append(f"at least {low}")
    if "gt" in bounds:
        parts.append(f"greater than {low}")
    if "le" in bounds:
        parts.append(f"at most {high}")
    if "lt" in bounds:
        parts.append(f"less than {high}")
    return f"{noun} that is {' and '.join(parts)}" if parts else noun


def _expectation(error: ErrorDetails) -> str:
    kind = error["type"]
    key = str(error["loc"][0]) if error["loc"] else ""
    if kind == "bool_type":
        return "true or false"
    if kind == "literal_error":
        return str((error.get("ctx") or {}).get("expected", "one of the allowed values")).replace("'", "")
    if kind in ("int_type", "int_parsing", "int_from_float"):
        return _range("an integer", _bounds(key))
    if kind in ("float_type", "float_parsing"):
        return _range("a number", _bounds(key))
    if kind in ("greater_than_equal", "greater_than", "less_than_equal", "less_than"):
        return _range("a number", _bounds(key))
    simple = {"string_type": "a string", "list_type": "a list", "tuple_type": "a list", "dict_type": "an object"}
    return simple.get(kind, str(error["msg"]))


def options_message(exc: ValidationError) -> str:
    """A short sentence about the first option that failed validation."""
    errors = exc.errors()
    first = errors[0]
    top = first["loc"][0] if first["loc"] else None
    key = str(top) if top is not None else "options"
    if first["type"] == "extra_forbidden":
        return f"option {key!r} is not recognised"
    nested = len(first["loc"]) > 1 and isinstance(first["loc"][1], int)
    if nested or first["type"] in ("too_short", "too_long", "value_error", "assertion_error"):
        detail = str(first["msg"]).removeprefix("Value error, ").removeprefix("Assertion failed, ")
        return f"option {key!r} is invalid: {detail}"
    same = [e for e in errors if e["loc"] and e["loc"][0] == top and e["type"] != "extra_forbidden"]
    return f"option {key!r} must be {' or '.join(dict.fromkeys(_expectation(e) for e in same))}"


def parse_options(raw: Mapping[str, Any]) -> Options:
    """Validate a request's options (``OptionsError`` if they are wrong), including ``engine``."""
    try:
        options = Options.model_validate(dict(raw))
    except ValidationError as exc:
        raise OptionsError(options_message(exc)) from None
    check_engine(options)
    return options
