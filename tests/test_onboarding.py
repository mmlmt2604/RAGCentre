"""Durable setup acceptance tests on real SQLite, including real local trial retrieval."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ragcentre.accounts.users import DEFAULT_TEAM_ID, complete_setup, create_user, init_admin, set_membership
from ragcentre.api.app import create_app
from ragcentre.api.authz import Principal
from ragcentre.context import open_context
from ragcentre.errors import Conflict, NotFound
from ragcentre.onboarding import service
from ragcentre.onboarding.models import STEPS, OperationCreate, SetupDraft
from ragcentre.pipeline.executors import execute
from ragcentre.pipeline.planner import create_pipeline
from ragcentre.queue.claims import claim_run, complete
from ragcentre.settings import Settings
from ragcentre.timeutil import after

ORIGIN = "https://testserver"
PASSWORD = "correct horse battery staple"


@pytest.fixture
def local(tmp_path: Path, fakes):
    ctx = open_context(
        Settings(data_dir=tmp_path, workers=0, api_token="admin-token", auth_mode="local", browser_origin=ORIGIN),
        http_client_factory=fakes.client_factory,
    )
    user = init_admin(ctx, "admin", PASSWORD)
    app = create_app(ctx)
    with TestClient(app, base_url=ORIGIN) as client:
        login = client.post(
            "/v1/auth/login", json={"username": "admin", "password": PASSWORD}, headers={"Origin": ORIGIN}
        )
        assert login.status_code == 200, login.text
        headers = {
            "Origin": ORIGIN,
            "X-RAGCentre-CSRF": login.json()["csrf_token"],
            "X-RAGCentre-Team-ID": DEFAULT_TEAM_ID,
        }
        p = Principal("admin", DEFAULT_TEAM_ID, "user", user["id"], "test-session", role="owner")
        yield ctx, client, headers, p
    ctx.close()


def start(local: Any, *, name: str = "trial-docs", key: str = "trial", draft: SetupDraft | None = None):
    ctx, _, _, p = local
    state = service.save_draft(
        ctx,
        p,
        DEFAULT_TEAM_ID,
        service.get_draft(ctx, p.actor_id, DEFAULT_TEAM_ID).version,
        draft or SetupDraft(corpus_name=name),
    )
    return service.create_operation(
        ctx,
        p,
        DEFAULT_TEAM_ID,
        OperationCreate(draft_version=state.draft.version, input_hash=state.draft_hash, idempotency_key=key),
    )


def run(local: Any, op):
    ctx, _, _, p = local
    for step in STEPS:
        op = service.execute_step(ctx, p, DEFAULT_TEAM_ID, op.id, step)
    assert all(s.state == "succeeded" for s in op.steps)
    return op


def resources(op):
    return {s.step: s.resource_id for s in op.steps}


def test_api_draft_conflicts_and_session_only(local):
    _, client, headers, _ = local
    url = f"/v1/teams/{DEFAULT_TEAM_ID}/setup"
    initial = client.get(url, headers=headers)
    assert initial.status_code == 200, initial.text
    assert initial.json()["readiness"]["ready"] is False
    draft_url = f"/v1/teams/{DEFAULT_TEAM_ID}/setup-draft"
    body = {"expected_version": 0, "draft": {"corpus_name": "docs"}}
    assert client.put(draft_url, headers=headers, json=body).status_code == 200
    assert client.put(draft_url, headers=headers, json=body).status_code == 409
    assert client.put(draft_url, headers={"X-RAGCentre-Team-ID": DEFAULT_TEAM_ID}, json=body).status_code == 403
    assert client.get(url, headers={"Authorization": "Bearer admin-token"}).status_code == 403


@pytest.mark.parametrize(
    "payload",
    [
        {"password": "never-store-this"},
        {"api_key": "never-store-this"},
        {"completed": True},
        {
            "parser": {
                "create": {
                    "name": "parse",
                    "kind": "parser",
                    "type": "http",
                    "config": {"base_url": "https://user:password@example.test"},
                }
            }
        },
        {
            "parser": {
                "create": {
                    "name": "parse",
                    "kind": "parser",
                    "type": "http",
                    "config": {"base_url": "https://example.test?token=secret"},
                }
            }
        },
        {
            "embedder": {
                "create": {
                    "name": "embed",
                    "kind": "embedder",
                    "type": "openai_compatible",
                    "config": {
                        "base_url": "https://example.test",
                        "model": "small",
                        "headers": {"Authorization": "secret"},
                    },
                }
            }
        },
    ],
)
def test_credentials_and_unknown_fields_never_enter_drafts(local, payload):
    ctx, client, headers, _ = local
    response = client.put(
        f"/v1/teams/{DEFAULT_TEAM_ID}/setup-draft",
        json={"expected_version": 0, "draft": {"mode": "custom", **payload}},
        headers=headers,
    )
    assert response.status_code == 422, response.text
    assert ctx.db.scalar("SELECT COUNT(*) FROM onboarding_drafts") == 0
    if payload.get("password"):
        assert "never-store-this" not in response.text


def test_custom_services_saved_connection_checks_gate_first_search(local):
    ctx, client, _, p = local
    created = client.post(
        "/v1/providers",
        json={
            "name": "verify-embed",
            "kind": "embedder",
            "type": "openai_compatible",
            "config": {"base_url": "http://embedder.test/v1", "model": "probe"},
            "team_id": DEFAULT_TEAM_ID,
        },
        headers={"Authorization": "Bearer admin-token"},
    )
    assert created.status_code == 201, created.text
    draft = SetupDraft(
        mode="custom", corpus_name="custom-docs", embedder={"reuse": "verify-embed"}, store={"reuse": "builtin-text"}
    )
    state = service.save_draft(ctx, p, DEFAULT_TEAM_ID, 0, draft)
    op = service.create_operation(
        ctx,
        p,
        DEFAULT_TEAM_ID,
        OperationCreate(draft_version=state.draft.version, input_hash=state.draft_hash, idempotency_key="custom"),
    )
    with pytest.raises(NotFound):  # The saved store is not a vector store.
        service.execute_step(ctx, p, DEFAULT_TEAM_ID, op.id, "store")


def test_custom_services_with_real_checks_reaches_document_stage(local, fakes):
    ctx, client, _, p = local
    created = client.post(
        "/v1/providers",
        json={
            "name": "real-embed",
            "kind": "embedder",
            "type": "openai_compatible",
            "config": {"base_url": "http://embedder.test/v1", "model": "probe"},
            "team_id": DEFAULT_TEAM_ID,
        },
        headers={"Authorization": "Bearer admin-token"},
    )
    assert created.status_code == 201, created.text
    store = client.post(
        "/v1/providers",
        json={
            "name": "local-vec",
            "kind": "vector_store",
            "type": "sqlite_vec",
            "config": {},
            "team_id": DEFAULT_TEAM_ID,
        },
        headers={"Authorization": "Bearer admin-token"},
    )
    assert store.status_code == 201, store.text
    draft = SetupDraft(
        mode="custom",
        corpus_name="custom-docs",
        parser={"reuse": "builtin-text"},
        embedder={"reuse": "real-embed"},
        store={"reuse": "local-vec"},
    )
    state = service.save_draft(ctx, p, DEFAULT_TEAM_ID, 0, draft)
    op = service.create_operation(
        ctx,
        p,
        DEFAULT_TEAM_ID,
        OperationCreate(draft_version=state.draft.version, input_hash=state.draft_hash, idempotency_key="custom"),
    )
    op = run(local, op)
    assert service.get_state(ctx, p, DEFAULT_TEAM_ID).readiness.services is True
    assert resources(op)["embedder"] == created.json()["id"]
    # The mocked embedder answers requests, so the saved check succeeded.
    assert ctx.db.scalar("SELECT ok FROM onboarding_provider_checks WHERE provider_id=?", (created.json()["id"],)) == 1


def test_operation_idempotency_and_changed_inputs_conflict(local):
    ctx, _, _, p = local
    op = start(local)
    body = OperationCreate(draft_version=op.input_version, input_hash=op.input_hash, idempotency_key="trial")
    assert service.create_operation(ctx, p, DEFAULT_TEAM_ID, body).id == op.id
    with pytest.raises(Conflict):
        service.create_operation(ctx, p, DEFAULT_TEAM_ID, body.model_copy(update={"input_hash": "0" * 64}))
    assert ctx.db.scalar("SELECT COUNT(*) FROM onboarding_operations") == 1


def test_resource_retries_and_refresh_do_not_duplicate(local):
    ctx, _, _, p = local
    op = run(local, start(local))
    counts = [ctx.db.scalar(f"SELECT COUNT(*) FROM {table}") for table in ("providers", "corpora", "pipelines")]
    for _ in range(2):
        resumed = service.get_state(ctx, p, DEFAULT_TEAM_ID).operation
        assert resumed.id == op.id
        run(local, resumed)
    assert counts == [ctx.db.scalar(f"SELECT COUNT(*) FROM {table}") for table in ("providers", "corpora", "pipelines")]
    assert service.get_state(ctx, p, DEFAULT_TEAM_ID).readiness.services is True
    assert service.get_state(ctx, p, DEFAULT_TEAM_ID).readiness.ready is False


def test_crash_before_step_commit_rolls_back_created_provider(local, monkeypatch):
    ctx, _, _, p = local
    op = start(local)
    insert = ctx.providers.insert_prepared

    def crash(conn, row):
        insert(conn, row)
        raise RuntimeError("simulated interruption before recording result")

    monkeypatch.setattr(ctx.providers, "insert_prepared", crash)
    with pytest.raises(RuntimeError):
        service.execute_step(ctx, p, DEFAULT_TEAM_ID, op.id, "embedder")
    assert ctx.db.scalar("SELECT COUNT(*) FROM providers WHERE kind='embedder'") == 0
    monkeypatch.setattr(ctx.providers, "insert_prepared", insert)
    service.execute_step(ctx, p, DEFAULT_TEAM_ID, op.id, "embedder")
    assert ctx.db.scalar("SELECT COUNT(*) FROM providers WHERE kind='embedder'") == 1


def test_expired_running_claim_can_resume_but_live_claim_conflicts(local):
    ctx, _, _, p = local
    op = start(local)
    sql = "UPDATE onboarding_steps SET state='running', claim_id='stranded', lease_expires_at=? WHERE operation_id=? AND step='embedder'"
    ctx.db.execute(sql, (after(600), op.id))
    with pytest.raises(Conflict):
        service.execute_step(ctx, p, DEFAULT_TEAM_ID, op.id, "embedder")
    ctx.db.execute(sql, (after(-1), op.id))
    done = service.execute_step(ctx, p, DEFAULT_TEAM_ID, op.id, "embedder")
    assert next(s for s in done.steps if s.step == "embedder").state == "succeeded"
    assert ctx.db.scalar("SELECT COUNT(*) FROM providers WHERE kind='embedder'") == 1


def test_concurrent_step_retry_creates_one_resource(local):
    ctx, _, _, p = local
    op = start(local)
    gate = Barrier(2)

    def attempt():
        gate.wait()
        try:
            return service.execute_step(ctx, p, DEFAULT_TEAM_ID, op.id, "corpus")
        except Conflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert any(results)
    assert ctx.db.scalar("SELECT COUNT(*) FROM corpora") == 1


def test_operations_are_bound_to_team_and_actor(local):
    ctx, _, _, p = local
    op = start(local)
    with pytest.raises(NotFound):
        service.operation(ctx, "another-account", DEFAULT_TEAM_ID, op.id)
    with pytest.raises(NotFound):
        service.operation(ctx, p.actor_id, "another-team", op.id)
    with pytest.raises(NotFound):
        service.execute_step(ctx, p, "another-team", op.id, "corpus")


def test_deleted_resource_does_not_silently_create_replacement(local):
    ctx, _, _, p = local
    op = start(local)
    op = service.execute_step(ctx, p, DEFAULT_TEAM_ID, op.id, "embedder")
    rid = resources(op)["embedder"]
    ctx.providers.delete(rid)
    with pytest.raises(Conflict):
        service.execute_step(ctx, p, DEFAULT_TEAM_ID, op.id, "embedder")
    assert ctx.db.scalar("SELECT COUNT(*) FROM providers WHERE kind='embedder'") == 0


def test_live_role_applies_to_setup_operations(local):
    ctx, _, _, p = local
    issued = create_user(ctx, "owner", "Owner", actor=p.actor_id)
    complete_setup(ctx, issued["setup_token"], PASSWORD)
    target = issued["user"]["id"]
    set_membership(ctx, DEFAULT_TEAM_ID, "owner", "owner", actor=p.actor_id)
    owner = Principal("team", DEFAULT_TEAM_ID, "user", target, "owner-session", role="owner")
    state = service.save_draft(ctx, owner, DEFAULT_TEAM_ID, 0, SetupDraft(corpus_name="owner-docs"))
    op = service.create_operation(
        ctx,
        owner,
        DEFAULT_TEAM_ID,
        OperationCreate(draft_version=state.draft.version, input_hash=state.draft_hash, idempotency_key="owner"),
    )
    set_membership(ctx, DEFAULT_TEAM_ID, "owner", "viewer", actor=p.actor_id)
    from ragcentre.errors import Forbidden

    with pytest.raises(Forbidden):
        service.execute_step(ctx, owner, DEFAULT_TEAM_ID, op.id, "corpus")


def test_trial_reaches_real_indexed_document_and_query_milestone(local):
    ctx, client, headers, p = local
    op = run(local, start(local))
    refs = resources(op)
    cid, pid = refs["corpus"], refs["pipeline"]
    response = client.post(
        f"/v1/corpora/{cid}/documents",
        files={
            "file": ("sample.txt", b"The warranty lasts twenty four months. Claims require a receipt.", "text/plain")
        },
        data={"external_id": "sample"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    assert service.get_state(ctx, p, DEFAULT_TEAM_ID).readiness.indexed_documents == 0
    for _ in range(30):
        job = claim_run(ctx, "trial-test")
        if job is None:
            break
        complete(ctx, job, execute(ctx, job))
    readiness = service.get_state(ctx, p, DEFAULT_TEAM_ID).readiness
    assert readiness.indexed_documents == 1
    assert readiness.ready is False
    response = client.post(f"/v1/corpora/{cid}/query", json={"query": "warranty", "pipeline_id": pid}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["results"]
    readiness = service.get_state(ctx, p, DEFAULT_TEAM_ID).readiness
    assert readiness.ready is True
    assert readiness.first_result_at
    stored = json.dumps([dict(r) for r in ctx.db.all("SELECT * FROM onboarding_first_results")])
    assert "warranty" not in stored
    config = json.loads(ctx.db.scalar("SELECT config FROM pipelines WHERE id=?", (pid,)))
    config["name"] = "replacement"
    config["nodes"][1]["options"]["max_chars"] = 400
    replacement = create_pipeline(ctx, cid, config)
    ctx.db.execute("UPDATE corpora SET active_pipeline_id=? WHERE id=?", (replacement, cid))
    assert service.get_state(ctx, p, DEFAULT_TEAM_ID).readiness.ready is False
    assert service.get_state(ctx, p, DEFAULT_TEAM_ID).readiness.first_result_at is None


def test_empty_or_partial_query_does_not_mark_success(local):
    ctx, _, _, _ = local
    op = run(local, start(local))
    refs = resources(op)
    service.record_first_result(ctx, refs["corpus"], refs["pipeline"], {"results": [], "partial": False})
    service.record_first_result(
        ctx, refs["corpus"], refs["pipeline"], {"results": [{"chunk_id": "x"}], "partial": True}
    )
    assert ctx.db.scalar("SELECT COUNT(*) FROM onboarding_first_results") == 0
