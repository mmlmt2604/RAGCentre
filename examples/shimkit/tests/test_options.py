from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from shimkit import (
    ShimError,
    any_of,
    boolean,
    choice,
    create_app,
    integer,
    options_schema,
    page_range,
    parse_options,
    string,
    strings,
)

SPECS = [
    boolean("ocr", True, "Run OCR"),
    choice("mode", ["accurate", "fast"], "accurate", "Table model"),
    integer("dpi", 192, minimum=72, maximum=400, description="Resolution"),
    string("prompt", None, "A prompt"),
    strings("lang", "Languages"),
    page_range(),
    any_of("ocr_mode", [True, False, "auto"], "auto", "Tri-state"),
]


def test_defaults() -> None:
    assert parse_options(SPECS, {}) == {
        "ocr": True,
        "mode": "accurate",
        "dpi": 192,
        "prompt": None,
        "lang": None,
        "page_range": None,
        "ocr_mode": "auto",
    }


def test_values_are_read() -> None:
    got = parse_options(
        SPECS, {"ocr": False, "mode": "fast", "dpi": 300, "lang": ["en"], "page_range": [2, 5], "ocr_mode": False}
    )
    assert got["ocr"] is False and got["mode"] == "fast" and got["dpi"] == 300
    assert got["lang"] == ["en"] and got["page_range"] == [2, 5] and got["ocr_mode"] is False


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"ocr": "false"}, "'ocr' must be true or false"),
        ({"mode": "slow"}, "'mode' must be one of: accurate, fast"),
        ({"dpi": 10}, "'dpi' must be an integer from 72 to 400"),
        ({"dpi": True}, "'dpi' must be an integer from 72 to 400"),
        ({"prompt": 3}, "'prompt' must be a string"),
        ({"lang": "en"}, "'lang' must be a list"),
        ({"lang": ["en", 1]}, "'lang' has an item that must be a string"),
        ({"page_range": [1]}, "'page_range' must have at least 2 items"),
        ({"page_range": [1, 2, 3]}, "'page_range' must have at most 3".replace("3", "2") + " items"),
        ({"page_range": [0, 2]}, "'page_range' has an item that must be at least 1"),
        ({"page_range": [5, 2]}, "'page_range' must not start after it ends"),
        ({"page_range": ["1", 2]}, "'page_range' has an item that must be an integer"),
        ({"ocr_mode": "yes"}, "'ocr_mode' must be one of: True, False, auto"),
        ({"ocr_mode": 1}, "'ocr_mode' must be one of"),
    ],
)
def test_invalid_values(raw: dict[str, Any], message: str) -> None:
    with pytest.raises(ShimError) as caught:
        parse_options(SPECS, raw)
    assert caught.value.status == 400 and caught.value.code == "invalid_options" and message in caught.value.message


def test_unknown_options_are_rejected_unless_lenient() -> None:
    with pytest.raises(ShimError) as caught:
        parse_options(SPECS, {"tabels": False})
    assert "unknown option 'tabels'" in caught.value.message and "ocr, mode" in caught.value.message
    assert parse_options(SPECS, {"tabels": False}, strict=False)["ocr"] is True


def test_null_resets_an_unset_by_default_option() -> None:
    assert parse_options(SPECS, {"page_range": None})["page_range"] is None


def test_schema_is_plain_json_schema() -> None:
    schema = options_schema(SPECS)
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    props = schema["properties"]
    assert props["ocr"] == {"type": "boolean", "description": "Run OCR", "default": True}
    assert props["mode"]["enum"] == ["accurate", "fast"] and props["dpi"]["maximum"] == 400
    assert props["page_range"]["minItems"] == 2 and "default" not in props["page_range"]
    assert props["ocr_mode"]["enum"] == [True, False, "auto"]
    json.dumps(schema)  # serialisable


def client(**kwargs: Any) -> TestClient:
    seen: list[dict[str, Any]] = []

    def parse(content: bytes, filename: str, options: dict[str, Any]) -> dict[str, Any]:
        seen.append(options)
        return {"schema_version": "1.0", "blocks": [], "metadata": {}, "pages": []}

    app = create_app(
        name="demo",
        version=lambda: "1",
        mime_types=["application/pdf"],
        capabilities={},
        api_key_env="DEMO_KEY",
        parse=parse,
        **kwargs,
    )
    http = TestClient(app)
    http.seen = seen  # type: ignore[attr-defined]
    return http


def post(http: TestClient, options: dict[str, Any]) -> Any:
    return http.post(
        "/parse", files={"file": ("a.pdf", b"x", "application/pdf")}, data={"options": json.dumps(options)}
    )


def test_service_publishes_the_schema_and_is_strict() -> None:
    http = client(options=SPECS)
    assert http.get("/info").json()["options_schema"] == options_schema(SPECS)
    assert post(http, {"ocr": False}).status_code == 200
    bad = post(http, {"tabels": False})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_options"
    assert post(http, {"dpi": 5}).status_code == 400
    assert http.seen == [{"ocr": False}]  # type: ignore[attr-defined]


def test_strict_options_can_be_turned_off() -> None:
    http = client(options=SPECS, strict_options=False)
    assert post(http, {"tabels": False}).status_code == 200


def test_a_service_without_declarations_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEMO_KEY", raising=False)
    http = client()
    assert "options_schema" not in http.get("/info").json()
    assert post(http, {"anything": 1}).status_code == 200


def test_a_boolean_can_be_unset_and_an_integer_can_default_to_none() -> None:
    specs = [boolean("maybe", None, "Engine default"), integer("limit", None, minimum=1, maximum=9)]
    assert parse_options(specs, {}) == {"maybe": None, "limit": None}
    assert parse_options(specs, {"maybe": True, "limit": 5}) == {"maybe": True, "limit": 5}
    assert parse_options(specs, {"maybe": None, "limit": None}) == {"maybe": None, "limit": None}
    with pytest.raises(ShimError):
        parse_options(specs, {"maybe": "yes"})
    with pytest.raises(ShimError):
        parse_options(specs, {"limit": 10})
    assert "default" not in options_schema(specs)["properties"]["maybe"]
