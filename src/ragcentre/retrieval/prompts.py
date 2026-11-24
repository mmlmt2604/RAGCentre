"""Built-in prompts for query transforms and their implementation versions.

Bump a mode's version whenever a change to its prompt or output handling alters what the same
query produces. The version enters the pipeline fingerprint, so evaluation results never mix
two behaviours.

Every system prompt starts with a ``Task: <mode>`` line. Models ignore it; the fake LLM in
``ragcentre.testing`` uses it to choose its answer.
"""

from __future__ import annotations

TRANSFORM_MODES: tuple[str, ...] = ("rewrite", "multi_query", "hyde", "decompose")

TRANSFORM_IMPL: dict[str, int] = {"rewrite": 1, "multi_query": 1, "hyde": 1, "decompose": 1}

# Room for the JSON envelope around the generated text.
_ENVELOPE_TOKENS = 64
_TOKENS_PER_QUERY = 60
_TOKENS_PER_PASSAGE = 200

_CONTRACT = (
    'Respond with one JSON object of the form {"queries": ["...", "..."]} and nothing else. '
    "Write every item in the same language as the user's message."
)

_TASKS: dict[str, str] = {
    "rewrite": (
        "Rewrite the user's search query as one clearer, self-contained query for finding passages "
        "in a document collection. Keep names, numbers, identifiers and technical terms exactly as "
        "written. Do not answer the question. Return exactly one item."
    ),
    "multi_query": (
        "Write up to {n} alternative phrasings of the user's search query, so that passages worded "
        "differently from the query can still be found. Vary vocabulary and perspective, keep the "
        "meaning, and keep names, numbers and identifiers exactly as written. Do not answer the "
        "question."
    ),
    "hyde": (
        "Write up to {n} short passages of 60 to 120 words that could plausibly appear in a document "
        "collection and would answer the user's query. Write in the style of the documents, not as a "
        "reply to a person, and use the terminology those documents would use. Details may be "
        "invented: the passages are used only to search, never shown as facts."
    ),
    "decompose": (
        "Break the user's question into up to {n} self-contained sub-questions, each answerable on its "
        "own from a document collection. Replace pronouns so that every sub-question stands alone. "
        "If the question is already simple, return it as the only item. Do not answer the question."
    ),
}


def system_prompt(mode: str, n: int, instructions: str) -> str:
    parts = [f"Task: {mode}", _TASKS[mode].format(n=n)]
    if instructions.strip():
        parts.append("Additional instructions from the owner of the collection:\n" + instructions.strip())
    parts.append(_CONTRACT)
    return "\n\n".join(parts)


def max_tokens(mode: str, n: int) -> int:
    per_item = _TOKENS_PER_PASSAGE if mode == "hyde" else _TOKENS_PER_QUERY
    return _ENVELOPE_TOKENS + n * per_item
