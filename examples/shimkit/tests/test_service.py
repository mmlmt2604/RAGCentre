from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from shimkit.service import (
    PROTOCOL,
    ShimError,
    bool_option,
    choice_option,
    create_app,
    file_extension,
    int_option,
    temp_upload,
)

IR = {"schema_version": "1.0", "metadata": {}, "pages": [], "blocks": [{"type": "paragraph", "text": "hi", "id": "b1"}]}


def make_client(parse: Any = None, *, serialise: bool = False, version: Any = None) -> TestClient:
    return TestClient(
        create_app(
            name="demo-shim",
            version=version or (lambda: "0.1.0+demo.1"),
            mime_types=["application/pdf"],
            capabilities={"page_images": False},
            api_key_env="DEMO_SHIM_API_KEY",
            parse=parse or (lambda content, filename, options: IR),
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
    }


def test_info_reads_the_version_per_request() -> None:
    current = ["0.1.0+demo.1"]
    client = make_client(version=lambda: current[0])
    assert client.get("/info").json()["version"] == "0.1.0+demo.1"
    current[0] = "0.1.0+demo.2"
    assert client.get("/info").json()["version"] == "0.1.0+demo.2"


def test_parse_passes_content_filename_and_options() -> None:
    calls: list[tuple[bytes, str, dict[str, Any]]] = []

    def parse(content: bytes, filename: str, options: dict[str, Any]) -> dict[str, Any]:
        calls.append((content, filename, options))
        return IR

    client = make_client(parse)
    resp = post(client, options=json.dumps({"ocr": False}))
    assert resp.status_code == 200 and resp.json() == IR
    assert post(client).status_code == 200  # no options field
    assert post(client, options="").status_code == 200  # empty options string
    assert calls == [(b"%PDF", "m.pdf", {"ocr": False}), (b"%PDF", "m.pdf", {}), (b"%PDF", "m.pdf", {})]


@pytest.mark.parametrize("options", ["[1]", "3", '"x"', "null", "{not json"])
def test_options_must_be_a_json_object(options: str) -> None:
    resp = post(make_client(), options=options)
    assert resp.status_code == 400
    assert resp.json() == {"error": {"code": "invalid_options", "message": "options must be a JSON object"}}


def test_shim_error_becomes_the_protocol_error_body() -> None:
    def parse(content: bytes, filename: str, options: dict[str, Any]) -> dict[str, Any]:
        raise ShimError(415, "unsupported_media_type", f"cannot read {filename}")

    resp = post(make_client(parse), name="m.xyz")
    assert resp.status_code == 415
    assert resp.json() == {"error": {"code": "unsupported_media_type", "message": "cannot read m.xyz"}}


def test_shim_error_from_options_helper() -> None:
    def parse(content: bytes, filename: str, options: dict[str, Any]) -> dict[str, Any]:
        bool_option(options, "tables", True)
        return IR

    resp = post(make_client(parse), options='{"tables": "yes"}')
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "invalid_options"


def test_other_exceptions_are_conversion_failed() -> None:
    def parse(content: bytes, filename: str, options: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("broken file")

    resp = post(make_client(parse), name="m.pdf")
    assert resp.status_code == 422
    assert resp.json() == {"error": {"code": "conversion_failed", "message": "m.pdf: RuntimeError: broken file"}}


def test_503_is_passed_through() -> None:
    def parse(content: bytes, filename: str, options: dict[str, Any]) -> dict[str, Any]:
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

    def parse(content: bytes, filename: str, options: dict[str, Any]) -> dict[str, Any]:
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

    def parse(content: bytes, filename: str, options: dict[str, Any]) -> dict[str, Any]:
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


def test_bool_option() -> None:
    assert bool_option({}, "k", True) is True
    assert bool_option({"k": False}, "k", True) is False
    for bad in ("true", 1, 0, None, [], "false"):
        with pytest.raises(ShimError) as err:
            bool_option({"k": bad}, "k", True)
        assert err.value.status == 400 and err.value.code == "invalid_options" and "'k'" in err.value.message


def test_choice_option() -> None:
    assert choice_option({}, "tier", ["flash", "standard"], "standard") == "standard"
    assert choice_option({"tier": "flash"}, "tier", ["flash", "standard"], "standard") == "flash"
    for bad in ("fast", "", 1, True, None, ["flash"]):
        with pytest.raises(ShimError) as err:
            choice_option({"tier": bad}, "tier", ["flash", "standard"], "standard")
        assert err.value.code == "invalid_options" and "flash, standard" in err.value.message


def test_int_option() -> None:
    assert int_option({}, "dpi", 192, minimum=72, maximum=400) == 192
    assert int_option({"dpi": 72}, "dpi", 192, minimum=72, maximum=400) == 72
    assert int_option({"dpi": 400}, "dpi", 192, minimum=72, maximum=400) == 400
    for bad in (71, 401, True, 150.5, "150", None):
        with pytest.raises(ShimError) as err:
            int_option({"dpi": bad}, "dpi", 192, minimum=72, maximum=400)
        assert err.value.code == "invalid_options" and "72 to 400" in err.value.message


def test_shutdown_hook_runs_when_the_server_stops() -> None:
    calls: list[str] = []
    app = create_app(
        name="demo-shim",
        version=lambda: "1",
        mime_types=[],
        capabilities={},
        api_key_env="DEMO_SHIM_API_KEY",
        parse=lambda content, filename, options: IR,
        shutdown=lambda: calls.append("stopped"),
    )
    with TestClient(app) as client:
        assert client.get("/info").status_code == 200
        assert calls == []
    assert calls == ["stopped"]


def test_no_shutdown_hook_is_fine() -> None:
    with TestClient(make_client().app):
        pass
