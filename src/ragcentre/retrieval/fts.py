"""Safe FTS5 query construction (section 13)."""

from __future__ import annotations

import re

_WS = re.compile(r"\s+")


def fts_query(text: str) -> str | None:
    tokens = [t for t in _WS.split(text.strip()) if t]
    if not tokens:
        return None

    def quote(token: str) -> str:
        return '"' + token.replace('"', '""') + '"'

    return " OR ".join(quote(t) for t in tokens)
