"""Built-in, in-process parser for ``text/plain`` and ``text/markdown`` (section 8.1).

Markdown: ATX headings (``#`` to ``######``) become ``heading`` blocks, fenced code becomes
``code`` blocks, lines starting with ``- ``, ``* `` or ``1. `` become ``list_item`` blocks, and
other text is split into ``paragraph`` blocks on blank lines. Plain text gets paragraphs only.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ragcentre.providers.base import BuildEnv, ProviderConfig, ProviderTypeSpec

# Bump whenever a change alters this parser's output for some input: the value is part of the
# provider fingerprint, so a new value keeps old and new parses apart.
IMPL_VERSION = 1

MIME_TYPES = ("text/plain", "text/markdown")
MARKDOWN_MIMES = frozenset({"text/markdown", "text/x-markdown"})

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})(.*)$")
_LIST = re.compile(r"^[ \t]*(?:[-*]|\d+\.)[ \t]+(.*)$")


def decode(content: bytes) -> str:
    text = content.decode("utf-8-sig", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _paragraphs(text: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for part in re.split(r"\n[ \t]*\n", text):
        stripped = part.strip()
        if stripped:
            blocks.append({"type": "paragraph", "text": stripped})
    return blocks


def parse_markdown(text: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    para: list[str] = []
    item: list[str] | None = None

    def flush() -> None:
        nonlocal item
        if para:
            blocks.append({"type": "paragraph", "text": "\n".join(para).strip()})
            para.clear()
        if item is not None:
            blocks.append({"type": "list_item", "text": "\n".join(item).strip()})
            item = None

    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        fence = _FENCE.match(line)
        if fence:
            flush()
            marker = fence.group(1)
            lang = fence.group(2).strip()
            body: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].lstrip().startswith(marker[0] * len(marker)):
                body.append(lines[i])
                i += 1
            i += 1  # closing fence (or end of input)
            block: dict[str, Any] = {"type": "code", "text": "\n".join(body)}
            if lang:
                block["metadata"] = {"language": lang}
            if block["text"].strip():
                blocks.append(block)
            continue
        heading = _HEADING.match(line)
        if heading:
            flush()
            title = heading.group(2).strip()
            if title:
                blocks.append({"type": "heading", "level": len(heading.group(1)), "text": title})
            i += 1
            continue
        if not line.strip():
            flush()
            i += 1
            continue
        listed = _LIST.match(line)
        if listed:
            flush()
            item = [listed.group(1)]
        elif item is not None and line[:1] in (" ", "\t"):
            item.append(line.strip())
        else:
            if item is not None:
                flush()
            para.append(line)
        i += 1
    flush()
    return [b for b in blocks if b["type"] == "code" or b["text"]]


def parse_text(content: bytes, mime_type: str) -> dict[str, Any]:
    text = decode(content)
    blocks = parse_markdown(text) if mime_type in MARKDOWN_MIMES else _paragraphs(text)
    return {"schema_version": "1.0", "metadata": {}, "pages": [], "blocks": blocks}


class BuiltinTextConfig(ProviderConfig):
    pass


class BuiltinTextParser:
    def parse(self, content: bytes, filename: str, mime_type: str, options: Mapping[str, Any]) -> dict[str, Any]:
        return parse_text(content, mime_type)


def _factory(config: Any, secrets: dict[str, str], env: BuildEnv) -> BuiltinTextParser:
    return BuiltinTextParser()


SPEC = ProviderTypeSpec(
    kind="parser",
    type="builtin_text",
    label="Built-in text",
    description="Plain text and Markdown, parsed in-process.",
    config_model=BuiltinTextConfig,
    fingerprint_fields=("impl_version",),
    secret_keys=(),
    factory=_factory,
    fixed_config={"impl_version": IMPL_VERSION},
)
