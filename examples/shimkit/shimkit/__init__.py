"""Shared pieces for the example ``ragcentre.parser/v1`` shims (Docling, MinerU, PaddleOCR, Surya)."""

from shimkit.html import first_heading_level, html_to_text, split_list_items, table_to_markdown
from shimkit.ir import IR_VERSION, build_ir
from shimkit.service import (
    PROTOCOL,
    ShimError,
    bool_option,
    choice_option,
    create_app,
    file_extension,
    int_option,
    temp_upload,
)

__all__ = [
    "IR_VERSION",
    "PROTOCOL",
    "ShimError",
    "bool_option",
    "build_ir",
    "choice_option",
    "create_app",
    "file_extension",
    "first_heading_level",
    "html_to_text",
    "int_option",
    "split_list_items",
    "table_to_markdown",
    "temp_upload",
]
