"""Shared pieces for the example ``ragcentre.parser/v1`` shims (Docling, MinerU, PaddleOCR, Surya)."""

from shimkit.html import first_heading_level, html_to_text, split_list_items, table_to_markdown
from shimkit.ir import IR_VERSION, build_ir
from shimkit.options import (
    Option,
    any_of,
    boolean,
    choice,
    integer,
    options_schema,
    page_range,
    parse_options,
    string,
    strings,
)
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
    "Option",
    "ShimError",
    "any_of",
    "bool_option",
    "boolean",
    "build_ir",
    "choice",
    "choice_option",
    "create_app",
    "file_extension",
    "first_heading_level",
    "html_to_text",
    "int_option",
    "integer",
    "options_schema",
    "page_range",
    "parse_options",
    "split_list_items",
    "string",
    "strings",
    "table_to_markdown",
    "temp_upload",
]
