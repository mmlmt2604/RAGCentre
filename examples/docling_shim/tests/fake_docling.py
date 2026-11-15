"""A stand-in for the parts of ``docling`` the shim's engine module uses, so the tests need neither
Docling nor its models. The option classes are pydantic models with the field names and defaults of
Docling 2.150.1 (checked against the installed package: ``tests/test_live.py`` compares them with
the real ones when ``SHIM_LIVE=1``)."""

from __future__ import annotations

import sys
import types
from enum import StrEnum
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, ConfigDict


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OcrMode(StrEnum):
    FULL_PAGE = "full_page"
    LAYOUT_REGIONS = "layout_regions"
    PDF_AWARE_LAYOUT_REGIONS = "pdf_aware_layout_regions"
    DEFAULT = "default"


class TableFormerMode(StrEnum):
    FAST = "fast"
    ACCURATE = "accurate"


class OcrOptions(Base):
    kind: ClassVar[str] = "base"
    mode: OcrMode = OcrMode.DEFAULT
    lang: list[str] = []
    scale: float = 3.0
    replace_broken_text_layer: bool = False


class OcrAutoOptions(OcrOptions):
    kind: ClassVar[str] = "auto"


class EasyOcrOptions(OcrOptions):
    kind: ClassVar[str] = "easyocr"
    lang: list[str] = ["en", "es", "fr", "de"]
    confidence_threshold: float = 0.5


class RapidOcrOptions(OcrOptions):
    kind: ClassVar[str] = "rapidocr"
    lang: list[str] = ["ch"]
    backend: str = "onnxruntime"
    text_score: float = 0.5
    rapidocr_params: dict[str, Any] = {}


class TesseractOcrOptions(OcrOptions):
    kind: ClassVar[str] = "tesserocr"
    lang: list[str] = ["eng", "spa", "fra", "deu"]
    path: str | None = None
    psm: int | None = None


class TesseractCliOcrOptions(OcrOptions):
    kind: ClassVar[str] = "tesseract"
    lang: list[str] = ["eng", "spa", "fra", "deu"]
    tesseract_cmd: str = "tesseract"
    path: str | None = None
    psm: int | None = None


class TableStructureOptions(Base):
    do_cell_matching: bool = True
    mode: TableFormerMode = TableFormerMode.ACCURATE


class AcceleratorOptions(Base):
    num_threads: int = 4
    device: str = "auto"


class PictureDescriptionVlmOptions(Base):
    batch_size: int = 8
    scale: float = 2.0
    prompt: str = "Describe this image in a few sentences."


class PictureDescriptionApiOptions(Base):
    batch_size: int = 8
    scale: float = 2.0
    url: str = "http://localhost:8000/v1/chat/completions"
    headers: dict[str, str] = {}
    params: dict[str, Any] = {}
    prompt: str = "Describe this image in a few sentences."


class PdfPipelineOptions(Base):
    document_timeout: float | None = None
    accelerator_options: AcceleratorOptions = AcceleratorOptions()
    enable_remote_services: bool = False
    artifacts_path: str | None = None
    do_picture_classification: bool = False
    do_picture_description: bool = False
    picture_description_options: Any = PictureDescriptionVlmOptions()
    do_chart_extraction: bool = False
    images_scale: float = 1.0
    generate_page_images: bool = False
    do_table_structure: bool = True
    do_ocr: bool = True
    do_code_enrichment: bool = False
    do_formula_enrichment: bool = False
    table_structure_options: TableStructureOptions = TableStructureOptions()
    ocr_options: Any = OcrAutoOptions()
    ocr_batch_size: int = 4


class VlmConvertOptions(Base):
    preset: str = ""

    @classmethod
    def from_preset(cls, preset_id: str) -> VlmConvertOptions:
        return cls(preset=preset_id)


class VlmPipelineOptions(Base):
    document_timeout: float | None = None
    accelerator_options: AcceleratorOptions = AcceleratorOptions()
    enable_remote_services: bool = False
    artifacts_path: str | None = None
    do_picture_classification: bool = False
    picture_description_options: Any = PictureDescriptionVlmOptions()
    do_picture_description: bool = False
    do_chart_extraction: bool = False
    images_scale: float = 1.0
    generate_page_images: bool = True
    force_backend_text: bool = False
    vlm_options: VlmConvertOptions = VlmConvertOptions()


class FakeFormatOption:
    def __init__(self, pipeline_options: Any = None, pipeline_cls: Any = None) -> None:
        self.pipeline_options = pipeline_options
        self.pipeline_cls = pipeline_cls


class FakeDocumentConverter:
    instances: ClassVar[list[FakeDocumentConverter]] = []

    def __init__(self, format_options: dict[str, FakeFormatOption]) -> None:
        self.format_options = format_options
        self.calls: list[tuple[Any, dict[str, Any]]] = []
        FakeDocumentConverter.instances.append(self)

    def convert(self, stream: Any, **kwargs: Any) -> Any:
        self.calls.append((stream, kwargs))
        return types.SimpleNamespace(document="doc")


@pytest.fixture
def fake_docling(monkeypatch: pytest.MonkeyPatch) -> type[FakeDocumentConverter]:
    FakeDocumentConverter.instances = []
    base = types.ModuleType("docling.datamodel.base_models")
    base.InputFormat = types.SimpleNamespace(PDF="pdf", IMAGE="image")  # type: ignore[attr-defined]
    base.DocumentStream = lambda name, stream: (name, stream.read())  # type: ignore[attr-defined]
    pipeline = types.ModuleType("docling.datamodel.pipeline_options")
    for cls in (
        OcrMode, TableFormerMode, OcrAutoOptions, EasyOcrOptions, RapidOcrOptions, TesseractOcrOptions,
        TesseractCliOcrOptions, TableStructureOptions, AcceleratorOptions, PictureDescriptionApiOptions,
        PdfPipelineOptions, VlmConvertOptions, VlmPipelineOptions,
    ):  # fmt: skip
        setattr(pipeline, cls.__name__, cls)
    datamodel = types.ModuleType("docling.datamodel")
    datamodel.pipeline_options = pipeline  # type: ignore[attr-defined]
    converter = types.ModuleType("docling.document_converter")
    converter.DocumentConverter = FakeDocumentConverter  # type: ignore[attr-defined]
    converter.PdfFormatOption = FakeFormatOption  # type: ignore[attr-defined]
    converter.ImageFormatOption = FakeFormatOption  # type: ignore[attr-defined]
    vlm_module = types.ModuleType("docling.pipeline.vlm_pipeline")
    vlm_module.VlmPipeline = "VlmPipeline"  # type: ignore[attr-defined]
    for name, module in {
        "docling": types.ModuleType("docling"),
        "docling.pipeline": types.ModuleType("docling.pipeline"),
        "docling.pipeline.vlm_pipeline": vlm_module,
        "docling.datamodel": datamodel,
        "docling.datamodel.base_models": base,
        "docling.datamodel.pipeline_options": pipeline,
        "docling.document_converter": converter,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    return FakeDocumentConverter
