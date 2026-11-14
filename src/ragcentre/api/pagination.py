"""Offset-based cursors: ``limit`` (default 50, max 500) and an opaque ``cursor``."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypeVar

from fastapi import Query

from ragcentre.errors import ValidationFailed

T = TypeVar("T")


@dataclass(frozen=True)
class PageParams:
    limit: int
    offset: int

    def page(self, items: Sequence[T]) -> tuple[list[T], str | None]:
        """Slice a full list."""
        part = list(items[self.offset : self.offset + self.limit])
        nxt = str(self.offset + self.limit) if self.offset + self.limit < len(items) else None
        return part, nxt

    def from_overfetch(self, rows: Sequence[T]) -> tuple[list[T], str | None]:
        """Rows fetched with ``LIMIT limit + 1 OFFSET offset``."""
        nxt = str(self.offset + self.limit) if len(rows) > self.limit else None
        return list(rows[: self.limit]), nxt


def page_params(limit: int = Query(default=50, ge=1, le=500), cursor: str | None = Query(default=None)) -> PageParams:
    offset = 0
    if cursor:
        try:
            offset = int(cursor)
        except ValueError:
            raise ValidationFailed("Invalid cursor.") from None
        if offset < 0:
            raise ValidationFailed("Invalid cursor.")
    return PageParams(limit=limit, offset=offset)
