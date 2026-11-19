"""Declare a shim's route options once; get validation, defaults and ``/info.options_schema``.

    OPTIONS = [
        boolean("ocr", True, "Run OCR on scanned pages"),
        choice("table_mode", ["accurate", "fast"], "accurate", "Table-structure model"),
        integer("dpi", 192, minimum=72, maximum=400, description="Render resolution"),
        page_range("page_range", "Pages to read (1-based, inclusive)"),
    ]
    create_app(..., options=OPTIONS)       # /info gets "options_schema"; unknown keys are a 400
    values = parse_options(OPTIONS, raw)   # inside your parse function

The schema is plain JSON Schema (``type``, ``enum``, ``minimum``, ``maximum``, ``items``, ``default``,
``description``), so RAGCentre can validate a pipeline's route options and render a form for them
without knowing the engine. An option whose default is ``None`` is "not set": it is left out of the
schema's ``default`` and ``parse_options`` returns ``None`` for it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from shimkit.service import ShimError, bool_option, choice_option, int_option


def _invalid(message: str) -> ShimError:
    return ShimError(400, "invalid_options", message)


@dataclass(frozen=True)
class Option:
    key: str
    schema: dict[str, Any]
    default: Any

    def read(self, options: Mapping[str, Any]) -> Any:
        """The validated value for this option, or its default when the key is absent."""
        if self.key not in options:
            return self.default
        value = options[self.key]
        schema = self.schema
        if value is None and self.default is None:
            return None
        if schema.get("type") == "boolean":
            return bool_option(options, self.key, bool(self.default))
        if schema.get("type") == "integer":
            return int_option(
                options, self.key, int(self.default or 0), minimum=schema["minimum"], maximum=schema["maximum"]
            )
        if "enum" in schema and all(isinstance(c, str) for c in schema["enum"]):
            return choice_option(options, self.key, schema["enum"], str(self.default))
        problem = _check(schema, value)
        if problem:
            raise _invalid(f"option {self.key!r} {problem}")
        return value


def _schema(base: dict[str, Any], default: Any, description: str) -> dict[str, Any]:
    out = dict(base)
    if description:
        out["description"] = description
    if default is not None:
        out["default"] = default
    return out


def boolean(key: str, default: bool, description: str = "") -> Option:
    return Option(key, _schema({"type": "boolean"}, default, description), default)


def choice(key: str, choices: Sequence[str], default: str, description: str = "") -> Option:
    return Option(key, _schema({"type": "string", "enum": list(choices)}, default, description), default)


def integer(key: str, default: int, *, minimum: int, maximum: int, description: str = "") -> Option:
    base = {"type": "integer", "minimum": minimum, "maximum": maximum}
    return Option(key, _schema(base, default, description), default)


def string(key: str, default: str | None = None, description: str = "") -> Option:
    return Option(key, _schema({"type": "string"}, default, description), default)


def strings(key: str, description: str = "") -> Option:
    """A list of strings (for example OCR languages), unset by default."""
    return Option(key, _schema({"type": "array", "items": {"type": "string"}}, None, description), None)


def page_range(key: str = "page_range", description: str = "Only read these pages: [first, last], 1-based") -> Option:
    """``[first, last]``, 1-based and inclusive, unset (every page) by default."""
    base = {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 2, "maxItems": 2}
    return Option(key, _schema(base, None, description), None)


def any_of(key: str, allowed: Sequence[Any], default: Any, description: str = "") -> Option:
    """One of a few literal values of mixed types, for example ``true``, ``false`` or ``"auto"``."""
    return Option(key, _schema({"enum": list(allowed)}, default, description), default)


def options_schema(options: Sequence[Option]) -> dict[str, Any]:
    """The JSON Schema for the options object."""
    return {
        "type": "object",
        "properties": {o.key: o.schema for o in options},
        "additionalProperties": False,
    }


def parse_options(options: Sequence[Option], raw: Mapping[str, Any], *, strict: bool = True) -> dict[str, Any]:
    """Validate ``raw`` against the declarations and fill in defaults. With ``strict`` an option the
    shim does not declare is a ``400 invalid_options`` (a typo then fails loudly); without it, it is
    ignored."""
    if strict:
        unknown = sorted(set(raw) - {o.key for o in options})
        if unknown:
            known = ", ".join(o.key for o in options) or "none"
            raise _invalid(f"unknown option {', '.join(repr(k) for k in unknown)}; the options are: {known}")
    return {o.key: o.read(raw) for o in options}


def _check(schema: dict[str, Any], value: Any) -> str | None:
    """A problem with ``value`` as a phrase that follows the option name, or ``None``."""
    if "enum" in schema:
        allowed = schema["enum"]
        if not any(type(value) is type(a) and value == a for a in allowed):
            return "must be one of: " + ", ".join(repr(a) if not isinstance(a, str) else a for a in allowed)
        return None
    kind = schema.get("type")
    if kind == "array":
        if not isinstance(value, list):
            return "must be a list"
        if "minItems" in schema and len(value) < schema["minItems"]:
            return f"must have at least {schema['minItems']} items"
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return f"must have at most {schema['maxItems']} items"
        for item in value:
            problem = _check(schema.get("items", {}), item)
            if problem:
                return f"has an item that {problem}"
        if schema.get("items", {}).get("type") == "integer" and "minItems" in schema and value[0] > value[-1]:
            return "must not start after it ends"
        return None
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return "must be an integer"
        if "minimum" in schema and value < schema["minimum"]:
            return f"must be at least {schema['minimum']}"
        if "maximum" in schema and value > schema["maximum"]:
            return f"must be at most {schema['maximum']}"
        return None
    if kind == "string":
        return None if isinstance(value, str) else "must be a string"
    if kind == "boolean":
        return None if isinstance(value, bool) else "must be true or false"
    return None
