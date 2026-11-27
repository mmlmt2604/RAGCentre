"""Host contract for OCR parser options (PR 1 of the OCR verification plan, tier A).

No engine runs. A fake shim (``ocr_contract_support.ShimFarm``) publishes an ``options_schema`` and
refuses a bad option with ``400 invalid_options`` the way an ``examples/shimkit`` service does. Each
test drives RAGCentre through its HTTP API (or CLI) and asserts what a user can see: what the shim
received, node runs, parsed output, query results and error responses.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from ocr_contract_support import (
    ENGINE_A,
    ENGINE_B,
    LABEL_A,
    LABEL_B,
    PROBE_TEXT,
    TABLE_CODE,
    Host,
    ShimFarm,
    canonical,
    open_host,
    pipeline_body,
    route,
)
from ragcentre import cli
from ragcentre.client import Client

PDF = "application/pdf"
JOBS = pytest.mark.parametrize("use_jobs", [False, True], ids=["sync", "async"])


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Host]:
    with open_host(tmp_path, monkeypatch) as opened:
        yield opened


@pytest.fixture
def fakes(host: Host) -> ShimFarm:
    return host.farm


def plan_node(host: Host, body: dict[str, Any], node_id: str = "parse") -> dict[str, Any]:
    plan = host.try_create(body, dry_run=True)
    assert plan.status_code == 200, plan.text
    node: dict[str, Any] = next(n for n in plan.json()["nodes"] if n["node_id"] == node_id)
    return node


def problem_paths(response: Any) -> list[str]:
    return [p["path"] for p in response.json()["error"]["details"]["problems"]]


# ---- the options a route saves are the options the shim receives ---------------------------------


@JOBS
def test_route_options_reach_the_shim_exactly_as_saved(host: Host, fakes: ShimFarm, use_jobs: bool) -> None:
    fakes.parser_async = use_jobs
    host.add_parser("docling")
    options = {"ocr": False, "tables": "accurate", "dpi": 150, "page_range": [2, 3]}
    doc = host.upload("manual.pdf")
    pid = host.pipeline(pipeline_body("tuned", [route(PDF, "docling", **options)]))

    saved = host.get(f"/v1/pipelines/{pid}")["config"]["nodes"][0]["routes"][0]["options"]
    assert canonical(saved) == canonical(options)
    (call,) = fakes.parses()
    assert (call.filename, call.mime) == ("manual.pdf", PDF)
    # canonical() keeps false and 0 apart, so a value that changed type on the way would show.
    assert canonical(call.options) == canonical(options)
    # The engine acted on the option, and the parse shows it.
    assert any(TABLE_CODE in text for text in host.texts(doc["id"], pid))


def test_a_route_without_options_sends_an_empty_object(host: Host, fakes: ShimFarm) -> None:
    host.add_parser("docling")
    host.upload("manual.pdf")
    body = pipeline_body("bare", [{"mime": PDF, "provider": "docling"}])
    host.pipeline(body)
    (call,) = fakes.parses()
    assert call.options == {}


def test_each_route_sends_its_own_options_to_its_own_engine(host: Host, fakes: ShimFarm) -> None:
    host.add_parser("docling", ENGINE_A)
    host.add_parser("scanner", ENGINE_B)
    host.upload("manual.pdf", b"pdf bytes")
    host.upload("scan.png", b"png bytes", "image/png")
    routes = [route(PDF, "docling", dpi=100), route("image/*", "scanner", dpi=300, ocr=True)]
    host.pipeline(pipeline_body("two-engines", routes))

    on_a, on_b = fakes.parses(LABEL_A), fakes.parses(LABEL_B)
    assert [(c.filename, canonical(c.options)) for c in on_a] == [("manual.pdf", canonical({"dpi": 100}))]
    assert [(c.filename, canonical(c.options)) for c in on_b] == [("scan.png", canonical({"dpi": 300, "ocr": True}))]


# ---- J3: change an option, compare, switch back ---------------------------------------------------


def test_changing_a_route_option_reparses_and_switching_back_costs_nothing(host: Host, fakes: ShimFarm) -> None:
    host.add_parser("docling")
    doc = host.upload("manual.pdf")
    a = host.pipeline(pipeline_body("fast", [route(PDF, "docling")]))
    assert host.parse_run(doc["id"], a)["stats"]["cache_hit"] is False
    assert not any(TABLE_CODE in t for t in host.texts(doc["id"], a))

    # Before saving, the plan says which work is new. An identical parse step is reused.
    changed = pipeline_body("accurate", [route(PDF, "docling", tables="accurate")])
    assert plan_node(host, changed)["reused"] is False
    assert plan_node(host, pipeline_body("fast-copy", [route(PDF, "docling")]))["reused"] is True

    b = host.pipeline(changed)
    assert host.parse_node_fp(a) != host.parse_node_fp(b)
    diff = host.get(f"/v1/pipelines/{a}/diff", against=b)
    (parse,) = [n for n in diff["nodes"] if n["node_id"] == "parse"]
    assert parse["same"] is False
    (change,) = parse["changes"]
    assert change["a"][0]["options"] == {} and change["b"][0]["options"] == {"tables": "accurate"}

    # The document was parsed again with the new option, not served from the first parse.
    assert host.parse_run(doc["id"], b)["stats"]["cache_hit"] is False
    assert [canonical(c.options) for c in fakes.parses()] == [canonical({}), canonical({"tables": "accurate"})]
    assert any(TABLE_CODE in t for t in host.texts(doc["id"], b))
    assert not any(TABLE_CODE in t for t in host.texts(doc["id"], a))

    # A fact that only the changed parse holds: B answers it and A cannot.
    assert any(TABLE_CODE in passage for passage in host.query(b, f"pump part code {TABLE_CODE}"))
    assert not any(TABLE_CODE in passage for passage in host.query(a, f"pump part code {TABLE_CODE}"))

    # Back to A: it is still ready, still reads as before, and parsing it again calls no engine.
    host.client.put(f"/v1/corpora/{host.corpus_id}/active-pipeline", json={"pipeline_id": a}).raise_for_status()
    assert not any(TABLE_CODE in t for t in host.texts(doc["id"], a))
    assert host.retry_parse(doc["id"], a)["stats"]["cache_hit"] is True
    assert host.retry_parse(doc["id"], b)["stats"]["cache_hit"] is True
    assert len(fakes.parses()) == 2
    assert any(TABLE_CODE in t for t in host.texts(doc["id"], b))


def test_an_option_written_out_at_its_default_is_different_work(host: Host, fakes: ShimFarm) -> None:
    """Known behaviour, pinned so that a change is noticed. ``{}`` and ``{"ocr": true}`` ask the shim for
    the same thing (its default for ``ocr`` is true) but they are different route options, so they get
    different fingerprints and the document is parsed again."""
    host.add_parser("docling")
    doc = host.upload("manual.pdf")
    bare = host.pipeline(pipeline_body("bare", [route(PDF, "docling")]))
    spelled = pipeline_body("spelled", [route(PDF, "docling", ocr=True)])
    assert plan_node(host, spelled)["reused"] is False
    explicit = host.pipeline(spelled)

    assert host.parse_node_fp(bare) != host.parse_node_fp(explicit)
    assert host.parse_run(doc["id"], explicit)["stats"]["cache_hit"] is False
    assert [canonical(c.options) for c in fakes.parses()] == [canonical({}), canonical({"ocr": True})]


def test_the_order_of_option_keys_is_not_a_change(host: Host, fakes: ShimFarm) -> None:
    host.add_parser("docling")
    host.upload("manual.pdf")
    first = host.pipeline(
        pipeline_body("one", [{"mime": PDF, "provider": "docling", "options": {"dpi": 150, "ocr": True}}])
    )
    again = pipeline_body("two", [{"mime": PDF, "provider": "docling", "options": {"ocr": True, "dpi": 150}}])
    assert plan_node(host, again)["reused"] is True
    second = host.pipeline(again)
    assert host.parse_node_fp(first) == host.parse_node_fp(second)
    assert len(fakes.parses()) == 1


# ---- J4: bad options ------------------------------------------------------------------------------

BAD_OPTIONS = [
    pytest.param({"tabels": False}, "tabels", id="unknown-key"),
    pytest.param({"ocr": "yes"}, "ocr", id="string-for-boolean"),
    pytest.param({"dpi": "high"}, "dpi", id="string-for-integer"),
    pytest.param({"dpi": 150.5}, "dpi", id="float-for-integer"),
    pytest.param({"dpi": True}, "dpi", id="boolean-for-integer"),
    pytest.param({"dpi": 71}, "dpi", id="below-minimum"),
    pytest.param({"dpi": 401}, "dpi", id="above-maximum"),
    pytest.param({"tables": "slow"}, "tables", id="not-in-enum"),
    pytest.param({"page_range": [3]}, "page_range", id="too-few-items"),
    pytest.param({"page_range": [0, 2]}, "page_range", id="item-below-minimum"),
]


@pytest.mark.parametrize(("options", "key"), BAD_OPTIONS)
def test_pipeline_creation_rejects_options_the_schema_does_not_allow(
    host: Host, options: dict[str, Any], key: str
) -> None:
    host.add_parser("docling")
    body = pipeline_body("bad", [route(PDF, "docling", **options)])
    path = f"nodes[0].routes[0].options.{key}"

    created = host.try_create(body)
    assert created.status_code == 422
    assert created.json()["error"]["code"] == "invalid_pipeline"
    assert problem_paths(created) == [path]

    planned = host.try_create(body, dry_run=True).json()
    assert planned["valid"] is False and [p["path"] for p in planned["problems"]] == [path]
    assert host.get(f"/v1/corpora/{host.corpus_id}/pipelines")["items"] == []  # nothing was saved


@pytest.mark.parametrize(
    "options", [{"dpi": 72}, {"dpi": 400}, {"page_range": [1, 1]}, {"ocr": False, "tables": "fast"}]
)
def test_options_at_the_edge_of_the_schema_are_accepted(host: Host, options: dict[str, Any]) -> None:
    host.add_parser("docling")
    assert host.try_create(pipeline_body("edge", [route(PDF, "docling", **options)])).status_code == 201


def test_the_rejected_option_is_found_in_the_route_that_holds_it(host: Host) -> None:
    host.add_parser("docling", ENGINE_A)
    host.add_parser("scanner", ENGINE_B)
    routes = [route(PDF, "docling", dpi=150), route("image/*", "scanner", dpi=9000, tabels=1)]
    rejected = host.try_create(pipeline_body("second-route", routes))
    assert rejected.status_code == 422
    assert sorted(problem_paths(rejected)) == [
        "nodes[0].routes[1].options.dpi",
        "nodes[0].routes[1].options.tabels",
    ]


@JOBS
def test_a_shim_that_rejects_the_options_fails_the_parse_with_a_clear_error(
    host: Host, fakes: ShimFarm, use_jobs: bool
) -> None:
    fakes.parser_async = use_jobs
    # /info publishes no schema, so the host cannot check the options when the pipeline is saved.
    # The shim still refuses an option it does not know, with 400 invalid_options.
    fakes.parser_options_schema = None
    host.add_parser("docling")
    doc = host.upload("manual.pdf")

    typo = host.pipeline(pipeline_body("typo", [route(PDF, "docling", tabels=False)]))  # accepted at save time
    run = host.parse_run(doc["id"], typo)
    assert run["status"] == "failed"
    assert run["error_code"] == "provider_error"
    assert run["attempt"] == 1  # a 400 is permanent, so it is not retried
    assert "docling" in run["error_message"] and "400" in run["error_message"]
    state = host.get(f"/v1/pipelines/{typo}/status")
    assert state["status"] == "degraded"
    assert [r["id"] for r in state["recent_failures"]] == [run["id"]]
    assert host.client.get(f"/v1/documents/{doc['id']}/parsed", params={"pipeline_id": typo}).status_code == 404
    assert len(fakes.parses()) == 1

    # The document is fine. The same route without the typo parses it.
    fixed = host.pipeline(pipeline_body("fixed", [route(PDF, "docling", tables="accurate")]))
    assert host.parse_run(doc["id"], fixed)["status"] == "succeeded"


# ---- the OCR check --------------------------------------------------------------------------------


def wait_for_ocr_state(host: Host, provider_id: str, state: str, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        services = host.get("/v1/ocr-services")["services"]
        mine = next((s for s in services if s["provider_id"] == provider_id), None)
        if mine is not None and mine["state"] == state:
            return mine  # type: ignore[no-any-return]
        if time.monotonic() > deadline:
            raise AssertionError(f"OCR state never became {state!r}: {mine}")
        time.sleep(0.02)


def test_the_ocr_check_sends_default_options_only(host: Host, fakes: ShimFarm) -> None:
    fakes.answer = {"schema_version": "1.0", "blocks": [{"type": "paragraph", "text": PROBE_TEXT}]}
    provider = host.add_parser("docling")
    # A route that is tuned. The check knows nothing about it and must not borrow its options.
    host.upload("manual.pdf")
    host.pipeline(pipeline_body("tuned", [route(PDF, "docling", dpi=150, tables="accurate")]))
    earlier = len(fakes.parses())

    host.client.post(f"/v1/providers/{provider['id']}/ocr-check").raise_for_status()
    wait_for_ocr_state(host, provider["id"], "ready")

    checks = fakes.parses()[earlier:]
    assert checks, "the OCR check never parsed its test page"
    assert [canonical(c.options) for c in checks] == [canonical({})] * len(checks)


# ---- revision -------------------------------------------------------------------------------------


def test_revision_cannot_be_changed_after_the_provider_exists(host: Host) -> None:
    created = host.add_parser("docling", revision="r1")
    refused = host.client.patch(f"/v1/providers/{created['id']}", json={"config": {"revision": "r2"}})
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "immutable_field"
    assert refused.json()["error"]["details"]["field"] == "revision"
    now = host.get(f"/v1/providers/{created['id']}")
    assert (now["config"]["revision"], now["fingerprint"]) == ("r1", created["fingerprint"])
    # Tuning settings can change, and they leave the fingerprint alone.
    tuned = host.client.patch(f"/v1/providers/{created['id']}", json={"config": {"timeout_seconds": 30}})
    assert tuned.status_code == 200 and tuned.json()["fingerprint"] == created["fingerprint"]


def test_a_new_provider_with_another_revision_forces_a_reparse(host: Host, fakes: ShimFarm) -> None:
    """The way to force a new fingerprint, since the revision of a provider cannot be edited."""
    first = host.add_parser("docling")
    second = host.add_parser("docling-r2", revision="r2")
    assert first["fingerprint"] != second["fingerprint"]

    doc = host.upload("manual.pdf")
    a = host.pipeline(pipeline_body("on-first", [route(PDF, "docling")]))
    b = host.pipeline(pipeline_body("on-second", [route(PDF, "docling-r2")]))
    assert host.parse_node_fp(a) != host.parse_node_fp(b)
    assert host.parse_run(doc["id"], b)["stats"]["cache_hit"] is False
    assert len(fakes.parses()) == 2


# ---- the same checks from the command line ---------------------------------------------------------


def test_the_cli_names_the_changed_option_and_refuses_a_bad_one(
    host: Host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host.add_parser("docling")
    host.upload("manual.pdf")
    a = host.pipeline(pipeline_body("fast", [route(PDF, "docling")]))
    b = host.pipeline(pipeline_body("accurate", [route(PDF, "docling", tables="accurate")]))

    class InProcessClient(Client):
        def close(self) -> None:  # the TestClient is shared by every command of the test
            pass

    monkeypatch.setattr(cli.state, "client_factory", lambda: InProcessClient("http://testserver", http=host.client))

    diff = CliRunner().invoke(cli.app, ["pipeline", "diff", a, b])
    assert diff.exit_code == 0, diff.output
    assert "accurate" in diff.output and "differs" in diff.output

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(pipeline_body("typo", [route(PDF, "docling", tabels=False)])))
    for extra in (["--dry-run"], []):
        refused = CliRunner().invoke(cli.app, ["pipeline", "create", host.corpus_id, "-f", str(bad), *extra])
        assert refused.exit_code == 1, refused.output
        assert "tabels" in refused.output
    assert len(host.get(f"/v1/corpora/{host.corpus_id}/pipelines")["items"]) == 2
