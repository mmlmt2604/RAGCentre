"""Command-line interface (section 17).

``serve`` and ``worker`` run in-process. Every other command is a thin client over the HTTP API
(``ragcentre.client``): it reads ``RAGCENTRE_URL`` (default ``http://127.0.0.1:8700``) and
``RAGCENTRE_API_TOKEN``. Corpora, providers, pipelines and datasets can be named by name or id.
``--json`` (before the command) prints the API's JSON instead of tables.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import mimetypes
import signal
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from ragcentre.client import ApiError, Client, Unreachable
from ragcentre.logs import configure_logging, redact, register_secret
from ragcentre.settings import Settings

app = typer.Typer(
    help="RAGCentre: build, compare and evaluate retrieval pipelines.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    add_completion=False,
)
provider_app = typer.Typer(
    help="Parsers, chunkers, embedders, rerankers, language models and vector stores.", no_args_is_help=True
)
corpus_app = typer.Typer(help="Corpora: document collections with a metadata schema.", no_args_is_help=True)
docs_app = typer.Typer(help="Documents in a corpus.", no_args_is_help=True)
pipeline_app = typer.Typer(
    help="Pipelines: how a corpus is parsed, chunked, embedded and indexed.", no_args_is_help=True
)
eval_app = typer.Typer(help="Evaluation datasets and runs.", no_args_is_help=True)
parser_app = typer.Typer(help="Check parser and OCR services.", no_args_is_help=True)
store_app = typer.Typer(help="Check vector stores against live data.", no_args_is_help=True)
cache_app = typer.Typer(help="Caches kept next to the database.", no_args_is_help=True)
app.add_typer(provider_app, name="provider")
app.add_typer(corpus_app, name="corpus")
app.add_typer(docs_app, name="docs")
app.add_typer(pipeline_app, name="pipeline")
app.add_typer(eval_app, name="eval")
app.add_typer(parser_app, name="parser")
app.add_typer(store_app, name="store")
app.add_typer(cache_app, name="cache")

log = logging.getLogger("ragcentre")


class _State:
    json: bool = False
    url: str | None = None
    # Tests inject a client factory; normally a fresh ``Client`` per command.
    client_factory: Callable[[], Client] | None = None


state = _State()


@app.callback()
def _main(
    json_output: Annotated[bool, typer.Option("--json", help="Print API responses as JSON.")] = False,
    url: Annotated[
        str | None, typer.Option("--url", help="API base URL (default RAGCENTRE_URL or http://127.0.0.1:8700).")
    ] = None,
) -> None:
    state.json = json_output
    state.url = url


# ---- helpers -----------------------------------------------------------------------------------


def _fail(message: str, code: int = 1) -> NoReturn:
    typer.echo(f"error: {redact(message)}", err=True)
    raise typer.Exit(code)


@contextmanager
def _api() -> Iterator[Client]:
    client = state.client_factory() if state.client_factory else Client(state.url)
    try:
        yield client
    except Unreachable as exc:
        _fail(str(exc))
    except ApiError as exc:
        typer.echo(f"error: {exc.code}: {exc.message}", err=True)
        for problem in exc.details.get("problems", []) or []:
            where = problem.get("node_id") or problem.get("path") or ""
            typer.echo(f"  - {where + ': ' if where else ''}{problem.get('message', problem)}", err=True)
        for problem in exc.details.get("errors", []) or []:
            typer.echo(f"  - line {problem.get('line')}: {problem.get('message')}", err=True)
        raise typer.Exit(1) from None
    finally:
        client.close()


def _emit(data: Any, human: Callable[[], None]) -> None:
    if state.json:
        typer.echo(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        human()


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    cells = [[str(h) for h in headers], *[["" if c is None else str(c) for c in row] for row in rows]]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    for n, row in enumerate(cells):
        typer.echo("  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip())
        if n == 0:
            typer.echo("  ".join("-" * w for w in widths))


def _load_file(path: Path) -> Any:
    import yaml

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        _fail(f"cannot read {path}: {exc.strerror or exc}")
    try:
        return json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    except (ValueError, yaml.YAMLError) as exc:
        _fail(f"{path} is not valid {'JSON' if path.suffix == '.json' else 'YAML'}: {exc}")


def _short(fp: str | None) -> str:
    return (fp or "")[:12]


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _settings(**overrides: object) -> Settings:
    settings = Settings.from_env()
    clean = {k: v for k, v in overrides.items() if v is not None}
    return settings.with_(**clean) if clean else settings


# ---- serve / worker ----------------------------------------------------------------------------


@app.command()
def serve(
    host: Annotated[str | None, typer.Option(help="Bind address (default RAGCENTRE_HOST or 127.0.0.1).")] = None,
    port: Annotated[int | None, typer.Option(help="Port (default RAGCENTRE_PORT or 8700).")] = None,
    workers: Annotated[int | None, typer.Option(help="Worker threads (default RAGCENTRE_WORKERS or 4).")] = None,
    allow_no_token: Annotated[bool, typer.Option(help="Allow a non-loopback bind without an API token.")] = False,
) -> None:
    """Run the API server and the ingestion workers."""
    import uvicorn

    from ragcentre.api.app import create_app
    from ragcentre.context import open_context

    settings = _settings(host=host, port=port, workers=workers)
    if not settings.api_token and not allow_no_token:
        try:
            is_loopback = ipaddress.ip_address(settings.host).is_loopback
        except ValueError:
            is_loopback = settings.host.lower() == "localhost"
        if not is_loopback:
            raise typer.BadParameter("Set RAGCENTRE_API_TOKEN or pass --allow-no-token.", param_hint="--host")
    configure_logging(settings.log_level)
    ctx = open_context(settings)
    try:
        ctx.start_workers(settings.workers)
        api = create_app(ctx)
        log.info("serving", extra={"host": settings.host, "port": settings.port, "workers": settings.workers})
        config = uvicorn.Config(api, host=settings.host, port=settings.port, log_level=settings.log_level.lower())
        uvicorn.Server(config).run()
    finally:
        ctx.close()


@app.command("rebuild-broken-indexes")
def rebuild_broken_indexes_command() -> None:
    """Rebuild quarantined legacy indexes in the configured data directory."""
    from ragcentre.context import open_context
    from ragcentre.pipeline.planner import rebuild_broken_indexes

    ctx = open_context(_settings())
    try:
        typer.echo(f"Queued {rebuild_broken_indexes(ctx)} indexes for rebuild.")
    finally:
        ctx.close()


@app.command()
def worker(
    workers: Annotated[int | None, typer.Option(help="Worker threads (default RAGCENTRE_WORKERS or 4).")] = None,
) -> None:
    """Run ingestion workers only, against the same data directory as a server."""
    from ragcentre.context import open_context

    settings = _settings(workers=workers)
    configure_logging(settings.log_level)
    ctx = open_context(settings)
    stop = threading.Event()

    def _handle(signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)
    try:
        ctx.start_workers(settings.workers)
        log.info("workers running", extra={"workers": settings.workers})
        while not stop.wait(0.5):
            pass
    finally:
        ctx.close()


# ---- providers ---------------------------------------------------------------------------------


def _provider_rows(items: Sequence[dict[str, Any]]) -> None:
    rows = []
    for p in items:
        secrets = ", ".join(f"{k}{'' if s['set'] else ' (unset)'}" for k, s in p["secrets"].items())
        rows.append([p["name"], p["kind"], p["type"], _short(p["fingerprint"]), p["used_by_pipelines"], secrets])
    _table(["NAME", "KIND", "TYPE", "FINGERPRINT", "PIPELINES", "SECRETS"], rows)


@provider_app.command("add")
def provider_add(
    file: Annotated[Path, typer.Option("-f", "--file", help="Provider YAML or JSON: name, kind, type, config.")],
) -> None:
    """Create a provider from a file.

    The file may also list environment-variable secrets, set right after creation:
    ``secrets: {api_key: {env: OPENAI_API_KEY}}``. Secret values never belong in files; use
    ``provider set-secret NAME KEY --value-stdin`` for stored secrets.
    """
    spec = _load_file(file)
    if not isinstance(spec, dict):
        _fail(f"{file} must contain a mapping with name, kind, type and config")
    secrets = spec.pop("secrets", None) or {}
    for key, ref in secrets.items():
        if not isinstance(ref, dict) or set(ref) != {"env"}:
            _fail(f"secret {key!r} in {file}: only environment references ({{env: VAR}}) are allowed in files")
    with _api() as api:
        created = api.post("/providers", json=spec)
        for key, ref in secrets.items():
            created = api.put(f"/providers/{created['id']}/secrets/{key}", json={"env": ref["env"]})
    _emit(created, lambda: typer.echo(f"Created {created['kind']} provider {created['name']} ({created['id']})."))


@provider_app.command("list")
def provider_list() -> None:
    """List providers."""
    with _api() as api:
        items = list(api.paginate("/providers"))
    _emit(items, lambda: _provider_rows(items))


@provider_app.command("edit")
def provider_edit(
    name: Annotated[str, typer.Argument(help="Provider name or id.")],
    file: Annotated[Path, typer.Option("-f", "--file", help="YAML or JSON mapping of config fields to change.")],
) -> None:
    """Update a provider's operational settings."""
    config = _load_file(file)
    if not isinstance(config, dict):
        _fail(f"{file} must contain a mapping of config fields to change")
    with _api() as api:
        p = api.provider(name)
        updated = api.call("PATCH", f"/providers/{p['id']}", json={"config": config})
    _emit(updated, lambda: typer.echo(f"Updated provider {updated['name']}."))


@provider_app.command("delete")
def provider_delete(name: Annotated[str, typer.Argument(help="Provider name or id.")]) -> None:
    """Delete an unused provider."""
    with _api() as api:
        p = api.provider(name)
        api.delete(f"/providers/{p['id']}")
    _emit({"deleted": p["id"]}, lambda: typer.echo(f"Deleted provider {p['name']}."))


@provider_app.command("test")
def provider_test(
    name: Annotated[str, typer.Argument(help="Provider name or id.")],
    deep: Annotated[
        bool,
        typer.Option("--deep", help="For a parser: parse a sample document; for a vector store: check live data."),
    ] = False,
) -> None:
    """Check that a provider is reachable and working."""
    with _api() as api:
        p = api.provider(name)
        result = api.post(f"/providers/{p['id']}/test", params={"deep": "true"} if deep else None)

    def human() -> None:
        status = "ok" if result["ok"] else "FAILED"
        typer.echo(f"{p['name']}: {status} in {result['latency_ms']:.0f} ms")
        if result.get("dimensions"):
            typer.echo(f"  dimensions: {result['dimensions']}")
        if result.get("service"):
            s = result["service"]
            typer.echo(f"  service: {s['name']} {s['version']}")
        if result.get("message"):
            typer.echo(f"  {result['message']}")
        for finding in (result.get("check") or {}).get("findings", []):
            if finding["level"] != "info":
                typer.echo(f"  {finding['level']}: {finding['check']}: {finding['message']}")
                if finding.get("lost"):
                    typer.echo(f"      loses: {finding['lost']}")

    _emit(result, human)
    if not result["ok"]:
        raise typer.Exit(1)


@parser_app.command("check")
def parser_check(
    base_url: Annotated[str, typer.Argument(help="The service's base URL, e.g. http://127.0.0.1:8701.")],
    file: Annotated[
        Path | None, typer.Option("--file", help="Parse this file instead of the built-in two-page sample PDF.")
    ] = None,
    api_key_env: Annotated[
        str | None, typer.Option("--api-key-env", help="Environment variable holding the service's bearer token.")
    ] = None,
    api_key: Annotated[
        str | None,
        typer.Option("--api-key", help="The bearer token itself (it shows in shell history: prefer --api-key-env)."),
    ] = None,
    job_timeout: Annotated[float, typer.Option("--job-timeout", help="Seconds to wait for an async job.")] = 600.0,
    request_timeout: Annotated[float, typer.Option("--timeout", help="Seconds to wait for each request.")] = 600.0,
    probe_errors: Annotated[
        bool,
        typer.Option("--probe-errors/--no-probe-errors", help="Also send an unreadable file to see how errors look."),
    ] = True,
) -> None:
    """Check that a service speaks ragcentre.parser/v1 and see which RAGCentre features its output feeds.

    Needs no running RAGCentre. Exits 1 when the service cannot be used as it is; warnings name the
    feature each problem costs."""
    import os

    import httpx

    from ragcentre.providers.parsers.check import check_parser

    if api_key_env is not None:
        api_key = os.environ.get(api_key_env)
        if not api_key:
            _fail(f"environment variable {api_key_env} is not set")
    upload = None
    if file is not None:
        if not file.is_file():
            _fail(f"{file} is not a file")
        upload = (file.name, file.read_bytes(), mimetypes.guess_type(file.name)[0] or "application/octet-stream")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    with httpx.Client(timeout=httpx.Timeout(request_timeout, connect=10.0), headers=headers) as client:
        report = check_parser(client, base_url, file=upload, job_timeout=job_timeout, probe_errors=probe_errors)

    def human() -> None:
        marks = {"error": "FAIL", "warn": "warn", "info": "ok  "}
        for finding in report.findings:
            typer.echo(f"{marks[finding.level]}  {finding.check}: {finding.message}")
            if finding.lost:
                typer.echo(f"      loses: {finding.lost}")
        warnings = sum(f.level == "warn" for f in report.findings)
        verdict = "usable" if report.ok else "NOT usable"
        typer.echo(f"{report.base_url}: {verdict}, {warnings} warning(s), {report.seconds:.1f} s")

    _emit(report.as_dict(), human)
    if not report.ok:
        raise typer.Exit(1)


def _store_env_secrets(type_: str, keys: Sequence[str]) -> dict[str, str]:
    """Read each secret from ``RAGCENTRE_<TYPE>_<KEY>`` (uppercased), never from the command line."""
    import os

    secrets: dict[str, str] = {}
    for key in keys:
        env_var = f"RAGCENTRE_{type_.upper()}_{key.upper()}"
        value = os.environ.get(env_var)
        if value:
            register_secret(value)
            secrets[key] = value
    return secrets


def _print_store_check(name: str, check: dict[str, Any]) -> None:
    shown = [f for f in check["findings"] if f["level"] != "info"]
    typer.echo(
        f"{name}: {'usable' if check['ok'] else 'NOT usable'} — {check['records']} records on "
        f"{check['collection']}, {len(check['metrics'])} metric(s), {check['seconds']:.1f} s"
    )
    for finding in shown:
        typer.echo(f"  {finding['level']}: {finding['check']}: {finding['message']}")
        if finding.get("lost"):
            typer.echo(f"      loses: {finding['lost']}")
    if not shown:
        typer.echo("  all checks passed.")


@store_app.command("list")
def store_list(
    markdown: Annotated[
        bool, typer.Option("--markdown", help="Print the README's Markdown table of the built-in stores.")
    ] = False,
) -> None:
    """List the vector store types, their extras and what each one guarantees.

    The figures are each type's best case; an instance can report less (``store check`` shows what a
    live store gives). ``--markdown`` prints the table that the README embeds, which a test keeps in
    step with the registry."""
    from ragcentre.stores import docs

    if markdown:
        typer.echo(docs.markdown_table())
        return
    headers, rows = docs.plain_rows()
    data = [dict(zip((h.lower().replace(" ", "_") for h in headers), row, strict=True)) for row in rows]
    _emit(data, lambda: _table(headers, rows))


@store_app.command("check")
def store_check(
    provider: Annotated[str | None, typer.Argument(help="Registered provider name or id.")] = None,
    type_: Annotated[
        str | None, typer.Option("--type", help="Store type to check directly, e.g. sqlite_vec or qdrant.")
    ] = None,
    config: Annotated[
        str | None, typer.Option("--config", help="Store config as JSON, with --type (default {}).")
    ] = None,
) -> None:
    """Check a vector store against live data and see which guarantees it gives.

    With a provider name, the running server runs the check (like ``provider test --deep``). With
    ``--type`` it builds the store directly, with no server, and reads each secret from the
    environment as ``RAGCENTRE_<TYPE>_<KEY>`` (uppercased) — never from the command line. Either way
    it writes to a temporary collection, always removes it, and exits 1 when the store is not usable."""
    if (provider is None) == (type_ is None):
        _fail("give either a provider name, or --type (with --config)")

    if type_ is not None:
        from ragcentre.stores.check import check_store
        from ragcentre.stores.registry import vector_store_types

        try:
            raw = json.loads(config or "{}")
        except ValueError as exc:
            _fail(f"--config is not valid JSON: {exc}")
        if not isinstance(raw, dict):
            _fail("--config must be a JSON object")
        types = vector_store_types()
        if type_ not in types:
            _fail(f"unknown store type {type_!r}. Installed types: {', '.join(sorted(types))}.")
        t = types[type_]
        try:
            model = t.config_model.model_validate(raw)
        except Exception as exc:
            _fail(f"invalid {type_} config: {exc}")
        try:
            store = t.factory(model, _store_env_secrets(type_, t.secret_keys), _settings().data_dir)
        except Exception as exc:
            _fail(f"could not build {type_}: {exc}")
        try:
            report = check_store(store, store_type=type_).as_dict()
            for finding in report["findings"]:
                finding["message"] = redact(finding["message"])
        finally:
            store.close()
        _emit(report, lambda: _print_store_check(report["store_type"], report))
        if not report["ok"]:
            raise typer.Exit(1)
        return

    assert provider is not None
    with _api() as api:
        p = api.provider(provider)
        if p.get("kind") != "vector_store":
            _fail(f"provider {p['name']!r} is not a vector_store")
        result = api.post(f"/providers/{p['id']}/test", params={"deep": "true"})

    def human() -> None:
        status = "ok" if result["ok"] else "FAILED"
        typer.echo(f"{p['name']}: {status} in {result['latency_ms']:.0f} ms")
        check = result.get("check")
        if check:
            _print_store_check(p["name"], check)
        elif result.get("message"):
            typer.echo(f"  {result['message']}")

    _emit(result, human)
    if not result["ok"]:
        raise typer.Exit(1)


@provider_app.command("set-secret")
def provider_set_secret(
    name: Annotated[str, typer.Argument(help="Provider name or id.")],
    key: Annotated[str, typer.Argument(help="Secret key, e.g. api_key.")],
    env: Annotated[str | None, typer.Option("--env", help="Read the secret from this environment variable.")] = None,
    value_stdin: Annotated[
        bool, typer.Option("--value-stdin", help="Read the value from standard input and store it encrypted.")
    ] = False,
) -> None:
    """Set a provider secret, as an environment reference or a stored (encrypted) value."""
    if (env is None) == (not value_stdin):
        _fail("give exactly one of --env VAR or --value-stdin")
    body: dict[str, str]
    if env is not None:
        body = {"env": env}
    else:
        value = sys.stdin.read().rstrip("\r\n")
        if not value:
            _fail("no secret value on standard input")
        body = {"value": value}
    with _api() as api:
        p = api.provider(name)
        updated = api.put(f"/providers/{p['id']}/secrets/{key}", json=body)
    source = f"environment variable {env}" if env else "stored value"
    _emit(updated, lambda: typer.echo(f"Set {key} on {p['name']} ({source})."))


# ---- corpora -----------------------------------------------------------------------------------


@corpus_app.command("create")
def corpus_create(
    name: Annotated[str, typer.Argument(help="Corpus name.")],
    schema: Annotated[
        Path | None,
        typer.Option("--schema", help="Metadata schema YAML/JSON: {fields: {lang: {type: str, default: en}}}."),
    ] = None,
) -> None:
    """Create a corpus."""
    body: dict[str, Any] = {"name": name}
    if schema is not None:
        raw = _load_file(schema) or {}
        body["metadata_schema"] = raw if isinstance(raw, dict) and "fields" in raw else {"fields": raw}
    with _api() as api:
        corpus = api.post("/corpora", json=body)
    _emit(corpus, lambda: typer.echo(f"Created corpus {corpus['name']} ({corpus['id']})."))


@corpus_app.command("list")
def corpus_list() -> None:
    """List corpora."""
    with _api() as api:
        items = list(api.paginate("/corpora"))

    def human() -> None:
        rows = []
        for c in items:
            active = next((p["name"] for p in c["pipelines"] if p["id"] == c["active_pipeline_id"]), "")
            live = [p for p in c["pipelines"] if not p["archived"]]
            rows.append([c["name"], c["document_count"], len(live), active, ", ".join(c["metadata_schema"]["fields"])])
        _table(["NAME", "DOCUMENTS", "PIPELINES", "ACTIVE", "METADATA FIELDS"], rows)

    _emit(items, human)


@corpus_app.command("activate")
def corpus_activate(
    corpus: Annotated[str, typer.Argument(help="Corpus name or id.")],
    pipeline: Annotated[str, typer.Argument(help="Pipeline name or id.")],
    force: Annotated[bool, typer.Option("--force", help="Activate even if the pipeline is not ready.")] = False,
) -> None:
    """Make a pipeline the corpus's default for queries."""
    with _api() as api:
        c = api.corpus(corpus)
        p = api.pipeline(pipeline, c["id"])
        updated = api.put(f"/corpora/{c['id']}/active-pipeline", json={"pipeline_id": p["id"], "force": force})
    _emit(updated, lambda: typer.echo(f"{p['name']} is now the active pipeline of {c['name']}."))


# ---- documents ---------------------------------------------------------------------------------


def _typed_metadata(pairs: Sequence[str], fields: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep or not key:
            _fail(f"--metadata expects key=value, got {pair!r}")
        ftype = (fields.get(key) or {}).get("type", "str")
        value: Any = raw
        try:
            if ftype == "int":
                value = int(raw)
            elif ftype == "float":
                value = float(raw)
            elif ftype == "bool":
                if raw.lower() not in ("true", "false", "1", "0", "yes", "no"):
                    raise ValueError(raw)
                value = raw.lower() in ("true", "1", "yes")
        except ValueError:
            _fail(f"metadata {key} must be a {ftype}, got {raw!r}")
        out[key] = value
    return out


@docs_app.command("add")
def docs_add(
    corpus: Annotated[str, typer.Argument(help="Corpus name or id.")],
    paths: Annotated[list[Path], typer.Argument(help="Files to upload.")],
    metadata: Annotated[
        list[str] | None, typer.Option("--metadata", "-m", help="key=value, typed by the corpus schema. Repeatable.")
    ] = None,
    external_id: Annotated[
        str | None, typer.Option("--external-id", help="External id (one file only; default: the file name).")
    ] = None,
    no_external_id: Annotated[
        bool, typer.Option("--no-external-id", help="Upload without an external id (every upload is new).")
    ] = False,
) -> None:
    """Upload documents. Re-adding a file with the same external id and content does no work."""
    if external_id is not None and len(paths) != 1:
        _fail("--external-id can only be used with a single file")
    for path in paths:
        if not path.is_file():
            _fail(f"{path} is not a file")
    results: list[dict[str, Any]] = []
    with _api() as api:
        c = api.corpus(corpus)
        meta = _typed_metadata(metadata or [], c["metadata_schema"]["fields"])
        for path in paths:
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            data: dict[str, str] = {"metadata": json.dumps(meta)} if metadata else {}
            ext = None if no_external_id else (external_id or path.name)
            if ext is not None:
                data["external_id"] = ext
            with path.open("rb") as fh:
                resp = api.request(
                    "POST", f"/corpora/{c['id']}/documents", files={"file": (path.name, fh, mime)}, data=data
                )
            doc = resp.json()
            doc["_created"] = resp.status_code == 201
            results.append(doc)
            if not state.json:
                verb = "added" if doc["_created"] else f"exists, revision {doc['revision']}"
                typer.echo(f"{path.name}: {verb} ({doc['id']})")
    if state.json:
        typer.echo(json.dumps(results, indent=2, ensure_ascii=False))


@docs_app.command("list")
def docs_list(corpus: Annotated[str, typer.Argument(help="Corpus name or id.")]) -> None:
    """List a corpus's documents with their ingestion state per pipeline."""
    with _api() as api:
        c = api.corpus(corpus)
        items = list(api.paginate(f"/corpora/{c['id']}/documents"))
    names = {p["id"]: p["name"] for p in c["pipelines"]}

    def human() -> None:
        rows = []
        for d in items:
            states = ", ".join(
                f"{names.get(i['pipeline_id'], i['pipeline_id'][:8])}: {i['state']}" for i in d["ingestion"]
            )
            rows.append([d["id"], d["filename"], d["external_id"] or "", d["size_bytes"], d["status"], states])
        _table(["ID", "FILENAME", "EXTERNAL ID", "BYTES", "STATUS", "INGESTION"], rows)

    _emit(items, human)


@docs_app.command("rm")
def docs_rm(document_id: Annotated[str, typer.Argument(help="Document id.")]) -> None:
    """Delete a document (its chunks and vectors are removed in the background)."""
    with _api() as api:
        doc = api.delete(f"/documents/{document_id}")
    _emit(doc, lambda: typer.echo(f"Deleting {doc['filename']} ({doc['id']})."))


# ---- pipelines ---------------------------------------------------------------------------------


def _print_plan(plan: dict[str, Any], names: dict[str, str]) -> None:
    if not plan["valid"]:
        typer.echo("The pipeline is not valid:")
        for p in plan["problems"]:
            where = p.get("node_id") or p.get("path") or ""
            typer.echo(f"  - {where + ': ' if where else ''}{p['message']}")
        return
    rows = []
    for n in plan["nodes"]:
        reuse = "reused" if n["reused"] else "new"
        if n["shared_with"]:
            reuse += f" (shared with {', '.join(names.get(i, i) for i in n['shared_with'])})"
        rows.append([n["node_id"], n["type"], _short(n["node_fp"]), reuse, n["runs_to_create"]])
    _table(["NODE", "TYPE", "FINGERPRINT", "WORK", "RUNS TO CREATE"], rows)
    typer.echo(f"Total runs to create: {plan['total_runs_to_create']}")


@pipeline_app.command("create")
def pipeline_create(
    corpus: Annotated[str, typer.Argument(help="Corpus name or id.")],
    file: Annotated[Path, typer.Option("-f", "--file", help="Pipeline YAML or JSON.")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Validate and show the plan without saving.")] = False,
) -> None:
    """Create a pipeline (or preview its cost with --dry-run)."""
    body = _load_file(file)
    with _api() as api:
        c = api.corpus(corpus)
        if dry_run:
            plan = api.post(f"/corpora/{c['id']}/pipelines", json=body, params={"dry_run": "true"})
            names = {p["id"]: p["name"] for p in c["pipelines"]}
            _emit(plan, lambda: _print_plan(plan, names))
            if not plan["valid"]:
                raise typer.Exit(1)
            return
        p = api.post(f"/corpora/{c['id']}/pipelines", json=body)

    def human() -> None:
        typer.echo(f"Created pipeline {p['name']} ({p['id']}) on {c['name']}: {p['status']}.")
        shared = [n["node_id"] for n in p["nodes"] if n["shared_with"]]
        if shared:
            typer.echo(f"Reuses work from other pipelines for: {', '.join(shared)}")

    _emit(p, human)


@pipeline_app.command("list")
def pipeline_list(
    corpus: Annotated[str, typer.Argument(help="Corpus name or id.")],
    archived: Annotated[bool, typer.Option("--archived", help="List archived pipelines instead.")] = False,
) -> None:
    """List a corpus's pipelines."""
    with _api() as api:
        c = api.corpus(corpus)
        items = list(api.paginate(f"/corpora/{c['id']}/pipelines", archived=str(archived).lower()))

    def human() -> None:
        rows = [[p["name"], p["id"], p["status"], _pct(p["coverage"]), "yes" if p["is_active"] else "",
                 _short(p["fingerprint"])] for p in items]  # fmt: skip
        _table(["NAME", "ID", "STATUS", "COVERAGE", "ACTIVE", "FINGERPRINT"], rows)

    _emit(items, human)


def _print_status(name: str, st: dict[str, Any]) -> None:
    typer.echo(f"{name}: {st['status']}, {_pct(st['coverage'])} of {st['document_count']} documents indexed")
    rows = []
    for n in st["nodes"]:
        k = n["counts"]
        rows.append([n["node_id"], n["type"], k["succeeded"], k["pending"], k["running"], k["failed"], k["blocked"]])
    _table(["NODE", "TYPE", "DONE", "PENDING", "RUNNING", "FAILED", "BLOCKED"], rows)
    for f in st["recent_failures"][:5]:
        typer.echo(f"  failed: {f['document_filename']} at {f['node_type']}: {f['error_code']}: {f['error_message']}")


@pipeline_app.command("status")
def pipeline_status(
    pipeline: Annotated[str, typer.Argument(help="Pipeline name or id.")],
    watch: Annotated[bool, typer.Option("--watch", help="Wait until the pipeline stops building.")] = False,
    interval: Annotated[float, typer.Option(help="Seconds between checks with --watch.")] = 1.0,
) -> None:
    """Show per-node progress. With --watch, exits 0 when ready and 1 when degraded."""
    with _api() as api:
        p = api.pipeline(pipeline)
        st = api.get(f"/pipelines/{p['id']}/status")
        if watch:
            last = None
            while st["status"] == "building":
                line = f"{p['name']}: building, {_pct(st['coverage'])} of {st['document_count']} documents indexed"
                if line != last and not state.json:
                    typer.echo(line)
                    last = line
                time.sleep(interval)
                st = api.get(f"/pipelines/{p['id']}/status")
    _emit(st, lambda: _print_status(p["name"], st))
    if watch and st["status"] != "ready":
        raise typer.Exit(1)


@pipeline_app.command("diff")
def pipeline_diff(
    a: Annotated[str, typer.Argument(help="Pipeline name or id.")],
    b: Annotated[str, typer.Argument(help="Pipeline name or id.")],
) -> None:
    """Show which nodes two pipelines share and how they differ."""
    with _api() as api:
        pa = api.pipeline(a)
        pb = api.pipeline(b, pa["corpus_id"])
        diff = api.get(f"/pipelines/{pa['id']}/diff", against=pb["id"])

    def human() -> None:
        typer.echo(f"a = {pa['name']}, b = {pb['name']}")
        rows = []
        for n in diff["nodes"]:
            where = "both" if n["in_a"] and n["in_b"] else ("a only" if n["in_a"] else "b only")
            rows.append([n["node_id"], n["type"], where, "same" if n["same"] else "differs"])
        _table(["NODE", "TYPE", "IN", "WORK"], rows)
        for n in diff["nodes"]:
            for ch in n["changes"]:
                typer.echo(f"  {n['node_id']}.{ch['path']}: {json.dumps(ch['a'])} -> {json.dumps(ch['b'])}")
        if diff["retrieval_same"]:
            typer.echo("Retrieval settings: same")
        else:
            typer.echo("Retrieval settings differ:")
            for ch in diff["retrieval_changes"]:
                typer.echo(f"  {ch['path']}: {json.dumps(ch['a'])} -> {json.dumps(ch['b'])}")

    _emit(diff, human)


@pipeline_app.command("archive")
def pipeline_archive(pipeline: Annotated[str, typer.Argument(help="Pipeline name or id.")]) -> None:
    """Archive a pipeline (its work is kept until garbage collection)."""
    with _api() as api:
        p = api.pipeline(pipeline)
        archived = api.post(f"/pipelines/{p['id']}/archive")
    _emit(archived, lambda: typer.echo(f"Archived {archived['name']}."))


# ---- query -------------------------------------------------------------------------------------


def _pages(r: dict[str, Any]) -> str:
    if r.get("page_start") is None:
        return ""
    if r["page_end"] in (None, r["page_start"]):
        return f"p.{r['page_start']}"
    return f"pp.{r['page_start']}-{r['page_end']}"


@app.command()
def query(
    corpus: Annotated[str, typer.Argument(help="Corpus name or id.")],
    question: Annotated[str, typer.Argument(help="The query text.")],
    pipeline: Annotated[
        str | None, typer.Option("--pipeline", help="Pipeline name or id (default: the active one).")
    ] = None,
    top_k: Annotated[int | None, typer.Option("--top-k", help="Number of results.")] = None,
    filter_json: Annotated[
        str | None, typer.Option("--filter", help='Metadata filter as JSON, e.g. {"lang": "en"}.')
    ] = None,
    allow_partial: Annotated[bool, typer.Option("--allow-partial", help="Query a pipeline still building.")] = False,
    full: Annotated[bool, typer.Option("--full", help="Print whole chunk texts.")] = False,
) -> None:
    """Search a corpus."""
    body: dict[str, Any] = {"query": question, "allow_partial": allow_partial}
    if top_k is not None:
        body["top_k"] = top_k
    if filter_json is not None:
        try:
            body["filter"] = json.loads(filter_json)
        except ValueError as exc:
            _fail(f"--filter is not valid JSON: {exc}")
    with _api() as api:
        c = api.corpus(corpus)
        if pipeline is not None:
            body["pipeline_id"] = api.pipeline(pipeline, c["id"])["id"]
        res = api.post(f"/corpora/{c['id']}/query", json=body)

    def human() -> None:
        if res["partial"]:
            typer.echo("(partial: the pipeline is still building)")
        if not res["results"]:
            typer.echo("No results.")
        for r in res["results"]:
            where = " > ".join(r["heading_path"])
            label = r["document"]["external_id"] or r["document"]["filename"]
            head = "  ".join(x for x in (f"{r['rank']}.", f"{r['score']:.4f}", label, _pages(r), where) if x)
            typer.echo(head)
            text = " ".join(r["text"].split())
            if not full and len(text) > 240:
                text = text[:237] + "..."
            typer.echo(f"   {text}")
        total = res["timings_ms"].get("total")
        if total is not None:
            typer.echo(f"({len(res['results'])} results in {total:.0f} ms)")

    _emit(res, human)


@cache_app.command("clear")
def cache_clear(
    transforms: Annotated[
        bool,
        typer.Option(
            "--transforms",
            help="Forget generated query rewrites, so the next query asks the language model again. "
            "Use it after the model behind a provider changes.",
        ),
    ] = False,
) -> None:
    """Empty a cache."""
    if not transforms:
        _fail("Choose what to clear, for example --transforms.", 2)
    with _api() as api:
        report = api.post("/admin/cache/clear", json={"transforms": True})
    _emit(report, lambda: typer.echo(f"Cleared {report['transforms_deleted']} cached query transforms."))


# ---- evaluation --------------------------------------------------------------------------------


@eval_app.command("import")
def eval_import(
    corpus: Annotated[str, typer.Argument(help="Corpus name or id.")],
    name: Annotated[str, typer.Argument(help="Dataset name.")],
    file: Annotated[Path, typer.Argument(help="JSON Lines file of questions and targets.")],
) -> None:
    """Import an evaluation dataset."""
    if not file.is_file():
        _fail(f"{file} is not a file")
    with _api() as api:
        c = api.corpus(corpus)
        with file.open("rb") as fh:
            ds = api.post(
                f"/corpora/{c['id']}/eval/datasets",
                files={"file": (file.name, fh, "application/x-ndjson")},
                data={"name": name},
            )
    _emit(ds, lambda: typer.echo(f"Imported {ds['item_count']} items as dataset {ds['name']} ({ds['id']})."))


def _parse_k(raw: str | None) -> list[int] | None:
    if raw is None:
        return None
    try:
        return [int(x) for x in raw.split(",") if x.strip()]
    except ValueError:
        _fail(f"--k expects comma-separated integers, got {raw!r}")


@eval_app.command("run")
def eval_run(
    dataset: Annotated[str, typer.Argument(help="Dataset name or id.")],
    pipelines: Annotated[list[str], typer.Argument(help="Pipeline names or ids to compare.")],
    k: Annotated[str | None, typer.Option("--k", help="Comma-separated k values (default 1,3,5,10).")] = None,
    allow_partial: Annotated[bool, typer.Option("--allow-partial", help="Allow pipelines still building.")] = False,
    wait: Annotated[bool, typer.Option("--wait", help="Wait for the run to finish, then show it.")] = False,
) -> None:
    """Start an evaluation run."""
    with _api() as api:
        ds = api.dataset(dataset)
        ids = [api.pipeline(p, ds["corpus_id"])["id"] for p in pipelines]
        body: dict[str, Any] = {"dataset_id": ds["id"], "pipeline_ids": ids, "allow_partial": allow_partial}
        k_values = _parse_k(k)
        if k_values is not None:
            body["k_values"] = k_values
        run = api.post("/eval/runs", json=body)
        if not wait:
            _emit(run, lambda: typer.echo(f"Started run {run['id']} ({run['progress']['total']} queries)."))
            return
        if not state.json:
            typer.echo(f"Started run {run['id']} ({run['progress']['total']} queries).")
        while run["status"] in ("pending", "running"):
            time.sleep(0.5)
            run = api.get(f"/eval/runs/{run['id']}")
        detail = api.get(f"/eval/runs/{run['id']}")
    _emit(detail, lambda: _print_run(detail))
    if detail["status"] != "finished":
        raise typer.Exit(1)


def _print_run(run: dict[str, Any]) -> None:
    summaries = run.get("summaries") or []
    progress = run["progress"]
    typer.echo(f"Run {run['id']} on {run['dataset_name']}: {run['status']} ({progress['done']}/{progress['total']})")
    if not summaries:
        return
    names = {s["pipeline_id"]: s["pipeline_name"] for s in summaries}
    pair = len(summaries) == 2
    by_metric = {(c["a"], c["b"], c["metric"]): c for c in run.get("comparisons") or []}
    headers = ["METRIC", *[s["pipeline_name"] for s in summaries]]
    if pair:
        headers += ["DIFF (b-a)", "95% CI", "SIG"]
    rows: list[list[Any]] = []
    for metric in summaries[0]["metrics"]:
        row: list[Any] = [metric, *[f"{s['metrics'][metric]:.3f}" for s in summaries]]
        if pair:
            c = by_metric.get((summaries[0]["pipeline_id"], summaries[1]["pipeline_id"], metric))
            if c is not None:
                row += [
                    f"{c['diff']:+.3f}",
                    f"[{c['ci_low']:+.3f}, {c['ci_high']:+.3f}]",
                    "*" if c["significant"] else "",
                ]
        rows.append(row)
    extras: list[tuple[str, Callable[[dict[str, Any]], str]]] = [
        ("latency p50 ms", lambda s: f"{s['latency_ms']['p50']:.1f}"),
        ("latency p95 ms", lambda s: f"{s['latency_ms']['p95']:.1f}"),
        ("errors", lambda s: str(s["error_count"])),
        ("chunks", lambda s: str(s["chunk_count"])),
        ("mean chunk chars", lambda s: f"{s['mean_chunk_chars']:.0f}"),
    ]
    for label, fmt in extras:
        rows.append([label, *[fmt(s) for s in summaries]] + (["", "", ""] if pair else []))
    _table(headers, rows)
    significant = [c for c in run.get("comparisons") or [] if c["significant"]]
    if pair:
        typer.echo("* the 95% paired-bootstrap interval excludes zero (a significant difference).")
    elif significant:
        typer.echo("Significant differences (b - a, 95% paired bootstrap):")
        for c in significant:
            typer.echo(
                f"  {c['metric']}: {names.get(c['b'], c['b'])} - {names.get(c['a'], c['a'])} = {c['diff']:+.3f} "
                f"[{c['ci_low']:+.3f}, {c['ci_high']:+.3f}]"
            )
    else:
        typer.echo("No significant differences.")


@eval_app.command("show")
def eval_show(run_id: Annotated[str, typer.Argument(help="Evaluation run id.")]) -> None:
    """Show a run: metrics side by side, with significant differences marked."""
    with _api() as api:
        run = api.get(f"/eval/runs/{run_id}")
    _emit(run, lambda: _print_run(run))


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
