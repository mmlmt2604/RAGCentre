"""Shared pieces for the example ``ragcentre.parser/v1`` shims (MinerU, PaddleOCR, Surya).

``docling_shim`` is deliberately self-contained and does not use this package.
"""

from shimkit.html import first_heading_level, html_to_text, split_list_items, table_to_markdown
from shimkit.ir import IR_VERSION, build_ir
from shimkit.service import (
    DENIED_ENGINE_KEYS,
    PAGE_RANGE_DESCRIPTION,
    PROTOCOL,
    PageRange,
    ShimError,
    ShimOptions,
    check_engine_options,
    create_app,
    file_extension,
    temp_upload,
)

__all__ = [
    "DENIED_ENGINE_KEYS",
    "IR_VERSION",
    "PAGE_RANGE_DESCRIPTION",
    "PROTOCOL",
    "PageRange",
    "ShimError",
    "ShimOptions",
    "build_ir",
    "check_engine_options",
    "create_app",
    "file_extension",
    "first_heading_level",
    "html_to_text",
    "split_list_items",
    "table_to_markdown",
    "temp_upload",
]
