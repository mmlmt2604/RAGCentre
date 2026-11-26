"""Source scope contracts for the bundled connectors, without discarding extension keys.

These schemas describe the filters actually used by scan(), not remote reachability.
Unknown connector types keep their plugin-owned scope contract.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ragcentre.errors import ValidationFailed


def _field(title: str, description: str, *, required: bool = False) -> dict[str, Any]:
    field: dict[str, Any] = {"type": "string", "title": title, "description": description}
    if required:
        field["minLength"] = 1
    else:
        field["type"] = ["string", "null"]
    return field


def _scope(
    properties: dict[str, Any],
    required: tuple[str, ...] = (),
    *,
    restrictions: tuple[str, ...] = (),
    broad_message: str = "",
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": True,
    }
    if broad_message:
        schema["x-broad-scope"] = {"unless_any": list(restrictions), "message": broad_message}
    return schema


_SCOPES = {
    "s3": _scope(
        {
            "bucket": _field("Bucket", "The bucket name, without s3://.", required=True),
            "prefix": _field("Prefix", "Only object keys starting with this exact prefix; for example manuals/."),
        },
        ("bucket",),
        restrictions=("prefix",),
        broad_message="Without a prefix, this source imports all supported objects in the selected bucket.",
    ),
    "sharepoint": _scope(
        {
            "drive_id": _field("Drive ID", "Microsoft Graph drive ID for the document library.", required=True),
            "folder": _field(
                "Folder", "Path within the drive; includes descendants. Leave empty for the entire drive."
            ),
        },
        ("drive_id",),
        restrictions=("folder",),
        broad_message="Without a folder, this source imports all supported files in the selected drive.",
    ),
    "confluence": _scope(
        {
            "space_key": _field(
                "Space key", "The Confluence space key. Imports current pages in this space.", required=True
            )
        },
        ("space_key",),
    ),
    "gdrive": _scope(
        {
            "shared_drive_id": _field(
                "Shared drive ID", "Optional shared drive ID; otherwise uses files visible to the service account."
            ),
            "folder_id": _field("Folder ID", "Only direct children of this folder; nested folders are not traversed."),
        },
        restrictions=("folder_id",),
        broad_message="Without a folder, this source imports all supported files visible in the selected shared drive, or all files visible to the service account when no drive is selected.",
    ),
    "notion": _scope(
        {
            "database_id": _field(
                "Database ID", "Optional database to query instead of searching all pages shared with the integration."
            ),
            "parent_page_id": _field(
                "Parent page ID",
                "Only pages whose immediate parent is this page; nested pages are not traversed. Both filters apply when both are set.",
            ),
        },
        restrictions=("database_id", "parent_page_id"),
        broad_message="Without a database or parent page, this source imports all pages shared with the Notion integration.",
    ),
}


def scope_schema(connector_type: str) -> dict[str, Any] | None:
    """Return an independent JSON Schema for a bundled connector, or no schema for plugins."""
    schema = _SCOPES.get(connector_type)
    return deepcopy(schema) if schema is not None else None


def validate_scope(connector_type: str, scope: dict[str, Any]) -> None:
    """Validate known fields only; never coerce, trim, or drop stored/plugin data."""
    schema = _SCOPES.get(connector_type)
    if schema is None:
        return
    problems = []
    for key, field in schema["properties"].items():
        value = scope.get(key)
        if key in schema["required"]:
            if not isinstance(value, str) or not value.strip():
                problems.append(
                    {"path": f"scope.{key}", "message": f"{field['title']} is required and must be a non-empty string."}
                )
        elif value is not None and not isinstance(value, str):
            problems.append({"path": f"scope.{key}", "message": f"{field['title']} must be a string or null."})
    if problems:
        raise ValidationFailed("Check the source scope fields.", details={"problems": problems})
