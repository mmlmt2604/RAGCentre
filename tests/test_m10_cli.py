"""M10: the CLI against a real HTTP server, including every command of examples/quickstart.md."""

from __future__ import annotations

import glob
import json
import re
import shlex
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from typer.testing import CliRunner

from ragcentre.api.app import create_app
from ragcentre.cli import app
from ragcentre.context import open_context
from ragcentre.settings import Settings

ROOT = Path(__file__).resolve().parent.parent
QUICKSTART = ROOT / "examples" / "quickstart.md"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def server(tmp_path: Path) -> Iterator[str]:
    """A real uvicorn server with workers, on a free port, with a fresh data directory."""
    settings = Settings(data_dir=tmp_path / "data", workers=2, idle_sleep_seconds=0.05, max_idle_sleep_seconds=0.1)
    ctx = open_context(settings)
    ctx.start_workers(settings.workers)
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(create_app(ctx), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not srv.started:
        if time.monotonic() > deadline:  # pragma: no cover
            raise RuntimeError("server did not start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.should_exit = True
        thread.join(10)
        ctx.close()


def run(url: str, *args: str, input: str | None = None) -> tuple[int, str]:
    result = CliRunner().invoke(app, ["--url", url, *args], input=input)
    if result.exception and not isinstance(result.exception, SystemExit):
        raise result.exception
    return result.exit_code, result.output


def ok(url: str, *args: str) -> str:
    code, out = run(url, *args)
    assert code == 0, f"ragcentre {' '.join(args)} exited {code}:\n{out}"
    return out


def quickstart_commands() -> list[str]:
    text = QUICKSTART.read_text(encoding="utf-8")
    blocks = re.findall(r"```sh\n(.*?)```", text, flags=re.S)
    lines = [line.strip() for block in blocks for line in block.splitlines()]
    return [line for line in lines if line.startswith("uv run ragcentre ")]


def test_quickstart_verbatim(server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every `uv run ragcentre ...` line of the quickstart, in order (the server is the fixture's)."""
    monkeypatch.chdir(ROOT)  # the quickstart's paths are relative to the repository root
    commands = quickstart_commands()
    assert commands[0] == "uv run ragcentre serve"
    outputs: dict[str, str] = {}
    for line in commands[1:]:
        argv = shlex.split(line)[3:]
        expanded: list[str] = []
        for arg in argv:  # what the shell does with docs/customer/*.md
            expanded += sorted(glob.glob(arg)) if "*" in arg else [arg]
        outputs[line] = ok(server, *expanded)

    def out(prefix: str) -> str:
        return next(v for k, v in outputs.items() if k.startswith(f"uv run ragcentre {prefix}"))

    assert "hash-256" in out("provider list") and "local-sqlite" in out("provider list")
    assert out("docs add kestrel examples/quickstart/docs/customer").count(": added") == 4
    structure_plan = out("pipeline create kestrel -f examples/pipelines/structure.yaml --dry-run")
    assert re.search(r"^parse\s+parse\s+\w+\s+reused \(shared with fixed\)\s+0$", structure_plan, flags=re.M)
    assert "ready, 100% of 5 documents indexed" in out("pipeline status structure --watch")
    assert "warranty.md" in out('query kestrel "How long is the warranty on a carbon frame?" --top-k 3')
    staff = out('query kestrel "torque for the stem bolts"')
    assert "workshop-procedures.md" in staff and "warranty.md" not in staff
    report = out("eval run questions fixed structure --wait")
    assert "finished (48/48)" in report
    for row in ("hit_rate@3", "recall@10", "mrr", "latency p50 ms", "mean chunk chars"):
        assert row in report
    assert re.search(r"^recall@3 .*\*$", report, flags=re.M), report  # structure is significantly better


def test_json_output_and_idempotent_upload(server: str, tmp_path: Path) -> None:
    ok(server, "corpus", "create", "notes")
    doc = tmp_path / "note.md"
    doc.write_text("# Note\n\nHello world.\n", encoding="utf-8")
    first = json.loads(ok(server, "--json", "docs", "add", "notes", str(doc)))
    again = json.loads(ok(server, "--json", "docs", "add", "notes", str(doc)))
    assert first[0]["_created"] is True and again[0]["_created"] is False
    assert first[0]["id"] == again[0]["id"] and again[0]["revision"] == 1
    assert first[0]["external_id"] == "note.md"
    listed = json.loads(ok(server, "--json", "docs", "list", "notes"))
    assert [d["filename"] for d in listed] == ["note.md"]
    assert "exists, revision 1" in ok(server, "docs", "add", "notes", str(doc))


def test_metadata_is_typed_by_schema(server: str, tmp_path: Path) -> None:
    schema = tmp_path / "schema.yaml"
    schema.write_text("fields:\n  year: {type: int, default: 0}\n  public: {type: bool, default: false}\n")
    ok(server, "corpus", "create", "typed", "--schema", str(schema))
    doc = tmp_path / "a.txt"
    doc.write_text("text", encoding="utf-8")
    added = json.loads(ok(server, "--json", "docs", "add", "typed", str(doc), "-m", "year=2024", "-m", "public=yes"))
    assert added[0]["metadata"] == {"year": 2024, "public": True}
    code, out = run(server, "docs", "add", "typed", str(doc), "-m", "year=soon")
    assert code == 1 and "year must be a int" in out
    code, out = run(server, "docs", "add", "typed", str(doc), "-m", "colour=red")
    assert code == 1 and "Unknown metadata field 'colour'" in out


def test_errors_are_reported_with_problems(server: str, tmp_path: Path) -> None:
    ok(server, "corpus", "create", "c1")
    bad = tmp_path / "bad.yaml"
    good = (ROOT / "examples" / "pipelines" / "fixed.yaml").read_text(encoding="utf-8")
    bad.write_text(good.replace("provider: hash-256", "provider: nope"), encoding="utf-8")
    code, out = run(server, "pipeline", "create", "c1", "-f", str(bad), "--dry-run")
    assert code == 1 and "The pipeline is not valid" in out and "nope" in out
    code, out = run(server, "pipeline", "create", "c1", "-f", str(bad))
    assert code == 1 and "error: invalid_pipeline" in out and "  - embed: " in out
    code, out = run(server, "corpus", "activate", "missing", "x")
    assert code == 1 and "No corpus named or with id 'missing'" in out


def test_unreachable_server() -> None:
    code, out = run(f"http://127.0.0.1:{_free_port()}", "corpus", "list")
    assert code == 1 and "Could not reach RAGCentre" in out and "ragcentre serve" in out


def test_provider_secrets(server: str, tmp_path: Path) -> None:
    spec = tmp_path / "p.yaml"
    spec.write_text(
        "name: emb\nkind: embedder\ntype: openai_compatible\n"
        "config: {base_url: 'http://127.0.0.1:9', model: m}\nsecrets:\n  api_key: {env: RAGCENTRE_PROVIDER_SOME_KEY}\n"
    )
    ok(server, "provider", "add", "-f", str(spec))
    listed = json.loads(ok(server, "--json", "provider", "list"))
    emb = next(p for p in listed if p["name"] == "emb")
    assert emb["secrets"]["api_key"] == {"source": "env", "set": True, "env_var": "RAGCENTRE_PROVIDER_SOME_KEY"}
    inline = tmp_path / "inline.yaml"
    inline.write_text("name: e2\nkind: embedder\ntype: builtin_hash\nsecrets:\n  api_key: {value: sk-123}\n")
    code, out = run(server, "provider", "add", "-f", str(inline))
    assert code == 1 and "only environment references" in out
    code, out = run(server, "provider", "set-secret", "emb", "api_key")
    assert code == 1 and "exactly one of" in out
    # Stored secrets need RAGCENTRE_SECRET_KEY on the server; the API's refusal is shown.
    code, out = run(server, "provider", "set-secret", "emb", "api_key", "--value-stdin", input="sk-test\n")
    assert code == 1 and "secret_storage_disabled" in out
    assert "sk-test" not in out
    code, out = run(server, "provider", "test", "emb")
    assert code == 1 and "emb: FAILED" in out
