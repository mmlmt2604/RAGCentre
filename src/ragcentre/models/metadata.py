"""Corpus metadata validation shared by uploads, schema updates and filter defaults."""

from __future__ import annotations

from typing import Any

from ragcentre.errors import ValidationFailed
from ragcentre.models.filters import type_ok
from ragcentre.stores.filter_sql import FIELD_RE


def validate_schema(schema: dict[str, Any]) -> None:
    for name, spec in schema.get("fields", {}).items():
        if not FIELD_RE.fullmatch(name) or name == "document_id":
            raise ValidationFailed(f"Invalid or reserved metadata field {name!r}.")
        if not type_ok(spec["default"], spec["type"]):
            raise ValidationFailed(f"Default for {name} must have type {spec['type']}.")


def validate_metadata(metadata: dict[str, Any], schema: dict[str, Any]) -> None:
    fields = schema.get("fields", {})
    for name, value in metadata.items():
        if name not in fields:
            raise ValidationFailed(f"Unknown metadata field {name!r}.")
        if not type_ok(value, fields[name]["type"]):
            raise ValidationFailed(f"Metadata {name} must have type {fields[name]['type']}.")
