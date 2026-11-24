"""Chunk enrichment (section 19): prompts, request keys and how enrich nodes stack.

An enrich node asks an ``llm`` provider to write a short text for every chunk it reads. That text
is stored beside the chunk (``chunk_enrichments``), never as a new chunk, so chunk ids stay those
of the chunk node. Enrich nodes may follow each other; each node's ``enrichment`` is the upstream
enrichment followed by what it generated, and ``embedded_text`` puts it before the chunk's own
context and text.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ragcentre.chunking.base import BLOCK_SEPARATOR
from ragcentre.hashing import canonical, sha256_bytes

# Bump whenever a code change alters the requests sent or the text stored for the same inputs.
IMPL_VERSION = 1

# The document goes in the system message so servers with prefix caching (llama-server, vLLM,
# hosted APIs) reuse it across all chunks of a document.
DOCUMENT_TEMPLATE = (
    "You help index documents for search. Here is the whole document, titled {title}:\n"
    "<document>\n{document}\n</document>"
)

DEFAULT_PROMPTS: Mapping[str, str] = {
    # The situating prompt from Anthropic's "contextual retrieval" write-up.
    "situate": (
        "Here is the chunk we want to situate within the whole document\n"
        "<chunk>\n{chunk}\n</chunk>\n"
        "Please give a short succinct context to situate this chunk within the overall document for the "
        "purposes of improving search retrieval of the chunk. Answer only with the succinct context and "
        "nothing else."
    ),
    "summary": (
        "<chunk>\n{chunk}\n</chunk>\n"
        "Summarise this chunk in one or two sentences for a search index. Answer only with the summary."
    ),
    "keywords": (
        "<chunk>\n{chunk}\n</chunk>\n"
        "List up to ten search keywords or key phrases for this chunk, separated by commas, including "
        "synonyms a searcher might use. Answer only with the list."
    ),
    "questions": (
        "<chunk>\n{chunk}\n</chunk>\n"
        "Write up to three short questions that this chunk answers, one per line. Answer only with the "
        "questions."
    ),
}

PLACEHOLDERS = ("{chunk}", "{context}", "{heading_path}", "{title}")
# A rough characters-per-token ratio, used only to estimate spending before a call.
CHARS_PER_TOKEN = 4


def effective_prompt(mode: str, prompt: str | None) -> str:
    """The instruction actually sent: the custom prompt, or the mode's default."""
    return prompt if prompt else DEFAULT_PROMPTS[mode]


def prompt_problems(prompt: str | None) -> list[str]:
    if prompt is not None and "{chunk}" not in prompt:
        return ["A custom prompt must contain {chunk}, where the chunk's text goes."]
    return []


def _fill(template: str, values: Mapping[str, str]) -> str:
    """Replace ``{name}`` placeholders in one pass, so a value that itself contains a placeholder
    (a chunk quoting "{title}") is never expanded again."""
    out: list[str] = []
    i = 0
    while i < len(template):
        if template[i] == "{":
            end = template.find("}", i)
            if end != -1 and template[i + 1 : end] in values:
                out.append(values[template[i + 1 : end]])
                i = end + 1
                continue
        out.append(template[i])
        i += 1
    return "".join(out)


@dataclass(frozen=True)
class Request:
    system: str | None
    prompt: str
    max_tokens: int
    temperature: float

    def key(self) -> str:
        """Cache key within one LLM provider fingerprint."""
        return sha256_bytes(
            canonical(
                {
                    "impl": IMPL_VERSION,
                    "system": self.system,
                    "prompt": self.prompt,
                    "max_tokens": self.max_tokens,
                    "temperature": self.temperature,
                }
            )
        )

    def estimated_tokens(self) -> int:
        chars = len(self.system or "") + len(self.prompt)
        return chars // CHARS_PER_TOKEN + 1 + self.max_tokens


def document_text(blocks: Sequence[Any], max_chars: int) -> str:
    """The document as the prompt shows it: content blocks in reading order, cut at ``max_chars``."""
    text = BLOCK_SEPARATOR.join(b.text for b in blocks if b.text)
    return text[:max_chars]


def build_requests(
    runtime: Mapping[str, Any],
    title: str,
    document: str,
    chunks: Sequence[Mapping[str, Any]],
) -> list[Request]:
    """One request per chunk. ``chunks`` carry ``text``, ``context``, ``heading_path`` (a list)."""
    template = effective_prompt(runtime["mode"], runtime.get("prompt"))
    system = _fill(DOCUMENT_TEMPLATE, {"title": title, "document": document}) if document else None
    out: list[Request] = []
    for chunk in chunks:
        values = {
            "chunk": str(chunk["text"]),
            "context": str(chunk.get("context") or ""),
            "heading_path": " > ".join(chunk.get("heading_path") or []),
            "title": title,
        }
        out.append(
            Request(
                system=system,
                prompt=_fill(template, values),
                max_tokens=int(runtime["max_tokens"]),
                temperature=float(runtime["temperature"]),
            )
        )
    return out


def stack(upstream: str | None, generated: str) -> str | None:
    """The enrichment after this node: upstream enrichment first, then this node's text."""
    joined = BLOCK_SEPARATOR.join(part for part in (upstream, generated.strip()) if part)
    return joined or None
