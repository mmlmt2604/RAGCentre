"""The connector interface (section 1.1 of the data-connectors plan).

A connector is a provider of kind ``connector``. It sits upstream of documents: it lists remote
items and fetches their bytes, and the sync engine writes those through ``ingest_document``.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class RemoteItem:
    remote_id: str
    name: str
    mime_type: str | None
    size: int | None
    version: str | None  # the remote_version used for skip-if-unchanged
    modified_at: str | None  # ISO-8601
    deleted: bool
    path: str | None


@dataclass(frozen=True)
class ScanResult:
    items: Iterator[RemoteItem]
    next_cursor: str | None
    full: bool  # True = this listing is the complete state (deletion diff applies)


@dataclass(frozen=True)
class FetchedFile:
    content: bytes
    filename: str
    mime_type: str
    version: str | None
    metadata: dict[str, Any]  # merged into document metadata (after the source's template)
    attributes: dict[str, Any]  # merged into document attributes


@runtime_checkable
class Connector(Protocol):
    def check(self) -> None: ...  # credentials + endpoint reachable (provider level)

    def scan(self, scope: Mapping[str, Any], cursor: str | None) -> ScanResult: ...  # items since cursor

    def fetch(self, scope: Mapping[str, Any], item: RemoteItem) -> FetchedFile: ...  # bytes + name + mime

    def close(self) -> None: ...  # optional; the engine calls it if present
