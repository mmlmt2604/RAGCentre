"""Document IR: the parser output contract (section 7).

``normalise_ir`` validates raw parser output and returns a ``DocumentIR`` in which every block
has an id, a known type and its ``heading_path``. Invalid output raises ``IRError``, which the
parse executor turns into a permanent node-run failure.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

BLOCK_TYPES = frozenset(
    {
        "heading",
        "paragraph",
        "list_item",
        "table",
        "figure",
        "code",
        "formula",
        "caption",
        "footnote",
        "page_header",
        "page_footer",
    }
)


class IRError(ValueError):
    """The parser returned something that is not a valid Document IR."""


class PageImage(BaseModel):
    model_config = ConfigDict(extra="allow")
    mime: str
    data_b64: str


class Page(BaseModel):
    model_config = ConfigDict(extra="allow")
    page: int = Field(ge=1)
    width: float | None = None
    height: float | None = None
    image: PageImage | None = None


class Block(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str | None = None
    type: str
    text: str
    level: int | None = None
    page: int | None = Field(default=None, ge=1)
    bbox: list[float] | None = None
    html: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    heading_path: list[str] = Field(default_factory=list)

    @field_validator("bbox")
    @classmethod
    def _bbox(cls, v: list[float] | None) -> list[float] | None:
        if v is not None and len(v) != 4:
            raise ValueError("bbox must be [x0, y0, x1, y1]")
        return v


class DocumentIR(BaseModel):
    model_config = ConfigDict(extra="allow")
    schema_version: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    pages: list[Page] = Field(default_factory=list)
    blocks: list[Block]

    def without_page_images(self) -> dict[str, Any]:
        data = self.model_dump(mode="json", exclude_none=False)
        for page in data.get("pages", []):
            page.pop("image", None)
        return data

    @property
    def has_pages(self) -> bool:
        return any(b.page is not None for b in self.blocks)


def _check_version(raw: Any) -> None:
    if not isinstance(raw, dict):
        raise IRError("parser output must be a JSON object")
    version = raw.get("schema_version")
    if not isinstance(version, str):
        raise IRError("schema_version is required and must be a string such as '1.0'")
    major = version.split(".", 1)[0]
    if major != "1":
        raise IRError(f"unsupported Document IR schema_version {version!r}; this RAGCentre accepts 1.x")


def compute_heading_paths(blocks: list[Block]) -> None:
    """Set each block's ``heading_path``.

    A heading of level N closes every open heading of level >= N. A heading block's own path
    includes itself, so a heading and the content under it share one path.
    """
    stack: list[tuple[int, str]] = []
    for block in blocks:
        if block.type == "heading":
            level = block.level or 1
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, block.text.strip()))
        block.heading_path = [text for _, text in stack]


def normalise_ir(raw: Any) -> DocumentIR:
    _check_version(raw)
    try:
        doc = DocumentIR.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise IRError(f"invalid Document IR at {where}: {first.get('msg')}") from None

    explicit: set[str] = set()
    for block in doc.blocks:
        if block.id is not None:
            if block.id in explicit:
                raise IRError(f"duplicate block id {block.id!r}")
            explicit.add(block.id)

    used = set(explicit)
    for index, block in enumerate(doc.blocks, start=1):
        if block.type not in BLOCK_TYPES:
            block.metadata = {**block.metadata, "original_type": block.type}
            block.type = "paragraph"
        if block.type == "heading":
            if block.level is None or not 1 <= block.level <= 6:
                raise IRError(f"block {block.id or index}: heading blocks need a level between 1 and 6")
        else:
            block.level = None
        if block.id is None:
            candidate = f"b{index}"
            suffix = 1
            while candidate in used:
                suffix += 1
                candidate = f"b{index}_{suffix}"
            block.id = candidate
            used.add(candidate)

    compute_heading_paths(doc.blocks)
    return doc
