from __future__ import annotations

import pytest

from ragcentre.connectors.scopes import scope_schema, validate_scope
from ragcentre.errors import ValidationFailed


@pytest.mark.parametrize(
    ("connector", "scope"),
    [
        ("s3", {"bucket": "docs", "prefix": "manuals/"}),
        ("s3", {"bucket": "docs", "prefix": None}),
        ("sharepoint", {"drive_id": "library", "folder": "Policies"}),
        ("confluence", {"space_key": "ENG"}),
        ("gdrive", {"shared_drive_id": "shared", "folder_id": "folder"}),
        ("gdrive", {}),
        ("notion", {"database_id": "database", "parent_page_id": "page"}),
        ("notion", {}),
        ("plugin", {"arbitrary": [1, {"query": True}]}),
    ],
)
def test_supported_scopes_preserve_extension_fields(connector: str, scope: dict) -> None:
    scope["plugin_options"] = {"future": [1, None, "two"]}
    before = dict(scope)
    validate_scope(connector, scope)
    assert scope == before


@pytest.mark.parametrize(
    ("connector", "scope", "field"),
    [
        ("s3", {}, "bucket"),
        ("s3", {"bucket": " "}, "bucket"),
        ("s3", {"bucket": "docs", "prefix": 7}, "prefix"),
        ("sharepoint", {"drive_id": False}, "drive_id"),
        ("sharepoint", {"drive_id": "library", "folder": []}, "folder"),
        ("confluence", {"space_key": None}, "space_key"),
        ("gdrive", {"shared_drive_id": []}, "shared_drive_id"),
        ("gdrive", {"folder_id": 42}, "folder_id"),
        ("notion", {"database_id": {}}, "database_id"),
        ("notion", {"parent_page_id": True}, "parent_page_id"),
    ],
)
def test_invalid_known_scope_fields(connector: str, scope: dict, field: str) -> None:
    with pytest.raises(ValidationFailed) as error:
        validate_scope(connector, scope)
    assert error.value.details is not None
    assert any(problem["path"] == f"scope.{field}" for problem in error.value.details["problems"])
    assert "input" not in error.value.details


def test_schema_is_additive_and_independent() -> None:
    first = scope_schema("s3")
    assert first is not None
    assert first["additionalProperties"] is True
    assert first["required"] == ["bucket"]
    first["properties"].clear()
    assert scope_schema("s3")["properties"]["bucket"]["minLength"] == 1  # type: ignore[index]
    assert scope_schema("plugin") is None
