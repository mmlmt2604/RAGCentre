from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Literal

import pytest
from fastapi.testclient import TestClient
from pydantic import Field

from shimkit.service import (
    PROTOCOL,
    ShimError,
    ShimOptions,
    create_app,
    file_extension,
    temp_upload,
)

IR = {"schema_version": "1.0", "metadata": {}, "pages": [], "blocks": [{"type": "paragraph", "text": "hi", "id": "b1"}]}


class Demo(ShimOptions):
    tables: bool = Field(True, description="Table recognition.")
    ocr: bool | Literal["auto"] = Field("auto", description="OCR mode.")
    tier: Literal["flash", "standard"] = Field("standard", description="Tier.")
    dpi: int = Field(192, ge=72, le=400, description="Resolution.")
    scale: float = Field(1.0, gt=0, description="Scale.")
    limit: int | None = Field(None, ge=1, description="At most this many.")


def make_client(parse: Any = None, *, serialise: bool = False, version: Any = None) -> TestClient:
    return TestClient(
        create_app(
            name="demo-shim",
            version=version or (lambda: "0.1.0+demo.1"),
            mime_types=["application/pdf"],
            capabilities={"page_images": False},
            api_key_env="DEMO_SHIM_API_KEY",
            parse=parse or (lambda content, filename, options: IR),
            options_model=Demo,
            serialise=serialise,
        )
    )


@pytest.fixture(autouse=True)
def no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEMO_SHIM_API_KEY", raising=False)


def post(client: TestClient, name: str = "m.pdf", options: str | None = None) -> Any:
    return client.post(
        "/parse",
        files={"file": (name, b"%PDF", "application/pdf")},
        data={} if options is None else {"options": options},
    )


def test_info() -> None:
    assert make_client().get("/info").json() == {
        "protocol": PROTOCOL,
        "name": "demo-shim",
        "version": "0.1.0+demo.1",
        "mime_types": ["application/pdf"],
        "capabilities": {"page_images": False},
        "options_schema": Demo.model_json_schema(),
    }


def test_info_reports_the_options_schema() -> None:
    schema = make_client().get("/info").json()["options_schema"]
    assert set(schema["properties"]) == {"tables", "ocr", "tier", "dpi", "scale", "limit"}
    assert schema["additionalProperties"] is False
    assert schema["properties"]["dpi"]["minimum"] == 72 and schema["properties"]["dpi"]["maximum"] == 400
    assert all(spec["description"] for spec in schema["properties"].values())


def test_info_reads_the_version_per_request() -> None:
    current = ["0.1.0+demo.1"]
    client = make_client(version=lambda: current[0])
    assert client.get("/info").json()["version"] == "0.1.0+demo.1"
    current[0] = "0.1.0+demo.2"
    assert client.get("/info").json()["version"] == "0.1.0+demo.2"


def test_parse_passes_content_filename_and_options() -> None:
    calls: list[tuple[bytes, str, Demo]] = []

    def parse(content: bytes, filename: str, options: Demo) -> dict[str, Any]:
        calls.append((content, filename, options))
        return IR

    client = make_client(parse)
    resp = post(client, options=json.dumps({"ocr": False}))
    assert resp.status_code == 200 and resp.json() == IR
    assert post(client).status_code == 200  # no options field
    assert post(client, options="").status_code == 200  # empty options string
    assert [(c, f, o.model_dump()["ocr"]) for c, f, o in calls] == [
        (b"%PDF", "m.pdf", False),
        (b"%PDF", "m.pdf", "auto"),
        (b"%PDF", "m.pdf", "auto"),
    ]
    assert all(isinstance(o, Demo) for _, _, o in calls)


@pytest.mark.parametrize("options", ["[1]", "3", '"x"', "null", "{not json"])
def test_options_must_be_a_json_object(options: str) -> None:
    resp = post(make_client(), options=options)
    assert resp.status_code == 400
    assert resp.json() == {"error": {"code": "invalid_options", "message": "options must be a JSON object"}}


def test_shim_error_becomes_the_protocol_error_body() -> None:
    def parse(content: bytes, filename: str, options: Demo) -> dict[str, Any]:
        raise ShimError(415, "unsupported_media_type", f"cannot read {filename}")

    resp = post(make_client(parse), name="m.xyz")
    assert resp.status_code == 415
    assert resp.json() == {"error": {"code": "unsupported_media_type", "message": "cannot read m.xyz"}}


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"ocrr": True}, "option 'ocrr' is not recognised"),
        ({"tables": "false"}, "option 'tables' must be true or false"),
        ({"tables": 1}, "option 'tables' must be true or false"),
        ({"tables": None}, "option 'tables' must be true or false"),
        ({"dpi": True}, "option 'dpi' must be an integer from 72 to 400"),
        ({"dpi": "150"}, "option 'dpi' must be an integer from 72 to 400"),
        ({"dpi": 150.5}, "option 'dpi' must be an integer from 72 to 400"),
        ({"dpi": 71}, "option 'dpi' must be an integer from 72 to 400"),
        ({"dpi": 401}, "option 'dpi' must be an integer from 72 to 400"),
        ({"tier": "fast"}, "option 'tier' must be flash or standard"),
        ({"tier": 1}, "option 'tier' must be flash or standard"),
        ({"ocr": "yes"}, "option 'ocr' must be true or false or auto"),
        ({"scale": "1"}, "option 'scale' must be a number that is greater than 0"),
        ({"limit": 0}, "option 'limit' must be an integer that is at least 1"),
        ({"tables": True, "dpi": 9, "zzz": 1}, None),  # the first failure is named
    ],
)
def test_invalid_options_are_400_and_name_the_key(options: dict[str, Any], message: str | None) -> None:
    called: list[Demo] = []

    def parse(content: bytes, filename: str, opts: Demo) -> dict[str, Any]:
        called.append(opts)
        return IR

    resp = post(make_client(parse), options=json.dumps(options))
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "invalid_options"
    got = resp.json()["error"]["message"]
    assert got == message if message else ("'dpi'" in got or "'zzz'" in got)
    assert called == []  # the parser never sees invalid options


def test_valid_options_are_accepted() -> None:
    seen: list[Demo] = []

    def parse(content: bytes, filename: str, opts: Demo) -> dict[str, Any]:
        seen.append(opts)
        return IR

    body = {"tables": False, "ocr": "auto", "tier": "flash", "dpi": 72, "scale": 2, "limit": 3}
    assert post(make_client(parse), options=json.dumps(body)).status_code == 200
    assert seen[0].model_dump() == {**body, "scale": 2.0}


def test_other_exceptions_are_conversion_failed() -> None:
    def parse(content: bytes, filename: str, options: Demo) -> dict[str, Any]:
        raise RuntimeError("broken file")

    resp = post(make_client(parse), name="m.pdf")
    assert resp.status_code == 422
    assert resp.json() == {"error": {"code": "conversion_failed", "message": "m.pdf: RuntimeError: broken file"}}


def test_503_is_passed_through() -> None:
    def parse(content: bytes, filename: str, options: Demo) -> dict[str, Any]:
        raise ShimError(503, "engine_unavailable", "model not loaded")

    assert post(make_client(parse)).status_code == 503


def test_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_SHIM_API_KEY", "s3cret")
    client = make_client()
    unauthorised = client.post("/parse", files={"file": ("m.pdf", b"x", "application/pdf")})
    assert unauthorised.status_code == 401 and unauthorised.json()["error"]["code"] == "unauthorized"
    for header in ("Bearer wrong", "s3cret", "bearer s3cret", "Bearer s3cret "):
        resp = client.post(
            "/parse", files={"file": ("m.pdf", b"x", "application/pdf")}, headers={"Authorization": header}
        )
        assert resp.status_code == 401, header
    ok = client.post(
        "/parse", files={"file": ("m.pdf", b"x", "application/pdf")}, headers={"Authorization": "Bearer s3cret"}
    )
    assert ok.status_code == 200


def test_info_is_readable_without_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """RAGCentre calls /info without credentials when it creates the provider."""
    monkeypatch.setenv("DEMO_SHIM_API_KEY", "s3cret")
    client = make_client()
    assert client.get("/info").status_code == 200
    assert client.get("/info", headers={"Authorization": "Bearer wrong"}).status_code == 200
    assert client.post("/info").status_code in (401, 405)  # only GET is exempt
    assert client.get("/info/").status_code == 401  # and only that exact path


def test_empty_api_key_means_no_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_SHIM_API_KEY", "")
    assert make_client().get("/info").status_code == 200


def _overlap(serialise: bool) -> int:
    running = 0
    peak = 0
    guard = threading.Lock()

    def parse(content: bytes, filename: str, options: Demo) -> dict[str, Any]:
        nonlocal running, peak
        with guard:
            running += 1
            peak = max(peak, running)
        time.sleep(0.05)
        with guard:
            running -= 1
        return IR

    client = make_client(parse, serialise=serialise)
    threads = [threading.Thread(target=post, args=(client,)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return peak


def test_serialise_runs_one_parse_at_a_time() -> None:
    assert _overlap(serialise=True) == 1


def test_without_serialise_parses_overlap() -> None:
    assert _overlap(serialise=False) > 1


def test_serialise_releases_the_lock_after_an_error() -> None:
    fail = [True]

    def parse(content: bytes, filename: str, options: Demo) -> dict[str, Any]:
        if fail[0]:
            raise RuntimeError("boom")
        return IR

    client = make_client(parse, serialise=True)
    assert post(client).status_code == 422
    fail[0] = False
    assert post(client).status_code == 200


# --- helpers ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "extension"),
    [
        ("report.PDF", "pdf"),
        ("a.b.tar.GZ", "gz"),
        ("noext", ""),
        ("trailingdot.", ""),
        (".hidden", "hidden"),
        ("../../etc/passwd", ""),
        ("..\\..\\x\\scan.TIFF", "tiff"),
        ("dir.d/file", ""),
        ("x.p/d\\f", ""),
        ("x.p$d f!", "pdf"),
        ("x." + "a" * 40, "a" * 16),
        ("x.\u00e9\u0131", ""),
    ],
)
def test_file_extension(filename: str, extension: str) -> None:
    assert file_extension(filename) == extension


def test_temp_upload_writes_and_removes() -> None:
    with temp_upload(b"data", "Scan.PNG") as path:
        assert path.suffix == ".png" and path.read_bytes() == b"data"
        directory = path.parent
    assert not path.exists() and not directory.exists()


def test_temp_upload_removes_on_error() -> None:
    with pytest.raises(RuntimeError), temp_upload(b"x", "a.pdf") as path:
        raise RuntimeError
    assert not path.exists()


def test_temp_upload_never_uses_the_clients_path() -> None:
    with temp_upload(b"x", "../../../etc/cron.d/evil.PDF") as path:
        assert path.name == "upload.pdf"
        assert "evil" not in str(path) and ".." not in path.parts
        assert path.resolve().is_relative_to(Path(path.parent).resolve())
    with temp_upload(b"x", "no-extension") as path:
        assert path.name == "upload"


def test_shutdown_hook_runs_when_the_server_stops() -> None:
    calls: list[str] = []
    app = create_app(
        name="demo-shim",
        version=lambda: "1",
        mime_types=[],
        capabilities={},
        api_key_env="DEMO_SHIM_API_KEY",
        parse=lambda content, filename, options: IR,
        options_model=Demo,
        shutdown=lambda: calls.append("stopped"),
    )
    with TestClient(app) as client:
        assert client.get("/info").status_code == 200
        assert calls == []
    assert calls == ["stopped"]


def test_no_shutdown_hook_is_fine() -> None:
    with TestClient(make_client().app):
        pass
