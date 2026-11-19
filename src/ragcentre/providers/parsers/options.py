"""Validate a parse route's ``options`` against the ``options_schema`` a parser service publishes.

The schema is the small subset of JSON Schema that ``shimkit`` generates and a hand-written service
can easily produce: an object with ``properties`` (each with ``type``, ``enum``, ``minimum``,
``maximum``, ``items``, ``minItems``/``maxItems``) and optionally ``additionalProperties: false``.
Anything the validator does not understand is accepted: the service has the last word, and this
check only exists to catch typos and wrong types when a pipeline is saved rather than at parse
time. It never raises.
"""

from __future__ import annotations

from typing import Any


def validate_route_options(schema: Any, options: Any) -> list[tuple[str, str]]:
    """``[(key, message), ...]``: ``key`` is the option's name (``""`` for the object itself)."""
    if not isinstance(schema, dict) or not isinstance(options, dict):
        return []
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return []
    problems: list[tuple[str, str]] = []
    if schema.get("additionalProperties") is False:
        known = ", ".join(sorted(properties)) or "none"
        for key in options:
            if key not in properties:
                problems.append((str(key), f'Unknown option "{key}". This parser\'s options: {known}.'))
    for key, value in options.items():
        spec = properties.get(key)
        if isinstance(spec, dict):
            message = _problem(spec, value)
            if message:
                problems.append((str(key), message))
    return problems


def _problem(spec: dict[str, Any], value: Any) -> str | None:
    """Why ``value`` does not fit ``spec``, or ``None``."""
    enum = spec.get("enum")
    if isinstance(enum, list):
        if not any(type(value) is type(allowed) and value == allowed for allowed in enum):
            return "Must be one of: " + ", ".join(_show(a) for a in enum) + "."
        return None
    kind = spec.get("type")
    if kind == "boolean":
        return None if isinstance(value, bool) else "Must be true or false."
    if kind == "string":
        return None if isinstance(value, str) else "Must be a string."
    if kind in ("integer", "number"):
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or (kind == "integer" and isinstance(value, float))
        ):
            return "Must be an integer." if kind == "integer" else "Must be a number."
        low, high = spec.get("minimum"), spec.get("maximum")
        if isinstance(low, int | float) and value < low:
            return f"Must be at least {low:g}."
        if isinstance(high, int | float) and value > high:
            return f"Must be at most {high:g}."
        return None
    if kind == "array":
        if not isinstance(value, list):
            return "Must be a list."
        low, high = spec.get("minItems"), spec.get("maxItems")
        if isinstance(low, int) and len(value) < low:
            return f"Needs at least {low} items."
        if isinstance(high, int) and len(value) > high:
            return f"Takes at most {high} items."
        items = spec.get("items")
        if isinstance(items, dict):
            for item in value:
                message = _problem(items, item)
                if message:
                    return f"Each item: {message[0].lower()}{message[1:]}"
    return None


def _show(value: Any) -> str:
    return value if isinstance(value, str) else str(value).lower() if isinstance(value, bool) else repr(value)
