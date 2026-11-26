"""Finite, user-triggered setup operations. Resource creation and step results commit together."""

from __future__ import annotations

import json
import sqlite3
from contextlib import nullcontext
from typing import Any

from ragcentre.api.authz import Principal, require_team
from ragcentre.context import AppContext
from ragcentre.errors import Conflict, Forbidden, NotFound, RagError, ValidationFailed
from ragcentre.hashing import fp, new_id
from ragcentre.locks import index_catalog_lock
from ragcentre.onboarding.models import (
    STEPS,
    DraftState,
    OperationCreate,
    ProviderDraft,
    ProviderReadiness,
    SetupDraft,
    SetupOperation,
    SetupReadiness,
    SetupState,
    SetupStep,
    StepName,
)
from ragcentre.pipeline.planner import create_pipeline
from ragcentre.providers.manager import ProviderRow
from ragcentre.timeutil import after, now_iso

LEASE_SECONDS = 600


def require_setup(ctx: AppContext, p: Principal, team_id: str) -> str:
    if p.actor_kind != "user" or not p.actor_id or not p.session_id:
        raise Forbidden("Team setup requires a signed-in user session.")
    require_team(p, team_id)
    user = ctx.db.one("SELECT status, is_platform_admin FROM users WHERE id=?", (p.actor_id,))
    membership = ctx.db.scalar("SELECT role FROM team_memberships WHERE user_id=? AND team_id=?", (p.actor_id, team_id))
    if user is None or user["status"] != "active" or not (user["is_platform_admin"] or membership == "owner"):
        raise Forbidden("Team setup requires a team owner or platform administrator.")
    if ctx.db.one("SELECT 1 FROM teams WHERE id=?", (team_id,)) is None:
        raise NotFound("Team was not found.")
    return p.actor_id


def draft_hash(draft: SetupDraft) -> str:
    return fp(draft.model_dump(mode="json"))


def get_draft(ctx: AppContext, actor: str, team: str) -> DraftState:
    row = ctx.db.one("SELECT version, draft FROM onboarding_drafts WHERE user_id=? AND team_id=?", (actor, team))
    return (
        DraftState(version=int(row["version"]), draft=SetupDraft.model_validate_json(row["draft"]))
        if row
        else DraftState(version=0, draft=SetupDraft())
    )


def save_draft(ctx: AppContext, p: Principal, team: str, expected: int, draft: SetupDraft) -> SetupState:
    with ctx.db.write() as conn:
        actor = require_setup(ctx, p, team)
        current = get_draft(ctx, actor, team)
        if current.version != expected:
            raise Conflict("Setup changed in another tab. Reload it before saving your choices.")
        conn.execute(
            "INSERT INTO onboarding_drafts (user_id, team_id, version, draft, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (user_id, team_id) DO UPDATE SET version=excluded.version, draft=excluded.draft, updated_at=excluded.updated_at",
            (actor, team, expected + 1, draft.model_dump_json(), now_iso()),
        )
    return get_state(ctx, p, team)


def operation(ctx: AppContext, actor: str, team: str, operation_id: str) -> SetupOperation:
    row = ctx.db.one(
        "SELECT * FROM onboarding_operations WHERE id=? AND user_id=? AND team_id=?", (operation_id, actor, team)
    )
    if row is None:
        raise NotFound("Setup operation was not found.")
    steps = {r["step"]: r for r in ctx.db.all("SELECT * FROM onboarding_steps WHERE operation_id=?", (operation_id,))}
    return SetupOperation(
        id=row["id"],
        input_version=row["input_version"],
        input_hash=row["input_hash"],
        draft=SetupDraft.model_validate_json(row["draft"]),
        steps=[SetupStep.model_validate({key: steps[s][key] for key in SetupStep.model_fields}) for s in STEPS],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def create_operation(ctx: AppContext, p: Principal, team: str, body: OperationCreate) -> SetupOperation:
    with ctx.db.write() as conn:
        actor = require_setup(ctx, p, team)
        existing = conn.execute(
            "SELECT id, input_hash, input_version FROM onboarding_operations WHERE user_id=? AND team_id=? AND idempotency_key=?",
            (actor, team, body.idempotency_key),
        ).fetchone()
        if existing:
            if existing["input_hash"] != body.input_hash:
                raise Conflict("This setup key has already been used with different choices.")
            return operation(ctx, actor, team, existing["id"])
        draft = get_draft(ctx, actor, team)
        if draft.version != body.draft_version or draft_hash(draft.draft) != body.input_hash:
            raise Conflict("The saved setup choices changed. Reload before starting setup.")
        if not draft.draft.corpus_name.strip():
            raise ValidationFailed("Give the corpus a name before creating setup resources.")
        if draft.draft.mode == "custom" and (draft.draft.embedder is None or draft.draft.store is None):
            raise ValidationFailed("Choose an embedder and a vector store.")
        operation_id, now = new_id(), now_iso()
        conn.execute(
            "INSERT INTO onboarding_operations (id, user_id, team_id, idempotency_key, input_version, input_hash, draft, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                operation_id,
                actor,
                team,
                body.idempotency_key,
                draft.version,
                body.input_hash,
                draft.draft.model_dump_json(),
                now,
                now,
            ),
        )
        conn.executemany(
            "INSERT INTO onboarding_steps (operation_id, step, state, updated_at) VALUES (?, ?, 'pending', ?)",
            [(operation_id, step, now) for step in STEPS],
        )
        return operation(ctx, actor, team, operation_id)


def _provider(ctx: AppContext, provider_id: str, team: str, kind: str | None = None) -> ProviderRow:
    row = ctx.providers.find(provider_id)
    if row is None or (row.team_id != team and not row.builtin) or (kind is not None and row.kind != kind):
        raise Conflict("A setup service was removed or is no longer available. Review the saved setup choices.")
    return row


def _resources(ctx: AppContext, op: SetupOperation, team: str) -> dict[str, str]:
    refs: dict[str, str] = {s.step: s.resource_id for s in op.steps if s.state == "succeeded" and s.resource_id}
    for step, kind in (("parser", "parser"), ("embedder", "embedder"), ("store", "vector_store")):
        if step in refs:
            _provider(ctx, refs[step], team, kind)
    if (
        "corpus" in refs
        and ctx.db.one("SELECT 1 FROM corpora WHERE id=? AND team_id=?", (refs["corpus"], team)) is None
    ):
        raise Conflict("The setup corpus was removed. Start a new setup operation.")
    if (
        "pipeline" in refs
        and ctx.db.one(
            "SELECT 1 FROM pipelines WHERE id=? AND corpus_id=? AND archived=0", (refs["pipeline"], refs.get("corpus"))
        )
        is None
    ):
        raise Conflict("The setup pipeline was removed or archived. Start a new setup operation.")
    return refs


def _pick(draft: SetupDraft, step: str, operation_id: str) -> ProviderDraft | None:
    if draft.mode != "trial":
        return getattr(draft, step)  # type: ignore[no-any-return]
    suffix = operation_id.replace("-", "")[:12]
    if step == "parser":
        return None
    return ProviderDraft.model_validate(
        {
            "create": {
                "name": f"trial-{step}-{suffix}",
                "kind": "embedder" if step == "embedder" else "vector_store",
                "type": "builtin_hash" if step == "embedder" else "sqlite_vec",
                "config": {"dimensions": 256} if step == "embedder" else {},
            }
        }
    )


def _prepare_provider(ctx: AppContext, team: str, op: SetupOperation, step: str) -> ProviderRow:
    pick = _pick(op.draft, step, op.id)
    if pick is None:
        row = ctx.providers.by_name("builtin-text")
        assert row is not None
        return row
    if pick.reuse is not None:
        row = ctx.providers.by_name_in_team(pick.reuse, team)
        kind = {"parser": "parser", "embedder": "embedder", "store": "vector_store"}[step]
        if row is None or row.kind != kind or (row.team_id != team and not row.builtin):
            raise NotFound("The selected setup service was not found in this team.")
        return row
    assert pick.create is not None
    body = pick.create
    return ctx.providers.prepare_create(body.name, body.kind, body.type, body.config, team_id=team)


def _pipeline_config(ctx: AppContext, draft: SetupDraft, refs: dict[str, str]) -> dict[str, Any]:
    names = {key: ctx.providers.get(refs[key]).name for key in ("parser", "embedder", "store")}
    nodes: list[dict[str, Any]] = [
        {
            "id": "parse",
            "type": "parse",
            "routes": [{"mime": "text/*", "provider": "builtin-text"}]
            + ([{"mime": "*/*", "provider": names["parser"]}] if names["parser"] != "builtin-text" else []),
        },
        {
            "id": "chunk",
            "type": "chunk",
            "inputs": ["parse"],
            "strategy": "recursive",
            "options": {"max_chars": 1000, "overlap_chars": 100},
        },
        {"id": "embed", "type": "embed", "inputs": ["chunk"], "provider": names["embedder"]},
        {
            "id": "vec",
            "type": "index_vector",
            "inputs": ["embed"],
            "provider": names["store"],
            "options": {"metric": "cosine"},
        },
    ]
    retrieval: dict[str, Any] = {"sources": [{"id": "dense", "index": "vec", "top_k": 30}], "top_k": 8}
    if draft.retrieval == "hybrid":
        nodes.append({"id": "fts", "type": "index_fts", "inputs": ["chunk"], "options": {"tokenizer": "unicode61"}})
        retrieval["sources"].append({"id": "sparse", "index": "fts", "top_k": 30})
        retrieval["fusion"] = {"method": "rrf", "k": 60}
    return {"name": draft.pipeline_name.strip(), "nodes": nodes, "retrieval": retrieval}


def provider_check_hash(ctx: AppContext, row: ProviderRow) -> str:
    return fp(
        {
            "updated_at": row.updated_at,
            "config": row.config,
            "secrets": ctx.providers.secret_states(row.id, ctx.providers.spec(row)),
        }
    )


def execute_step(ctx: AppContext, p: Principal, team: str, operation_id: str, step: StepName) -> SetupOperation:
    claim = new_id()
    with ctx.db.write() as conn:
        actor = require_setup(ctx, p, team)
        op = operation(ctx, actor, team, operation_id)
        _resources(ctx, op, team)
        own = next(s for s in op.steps if s.step == step)
        if own.state == "succeeded":
            if step == "check_services":
                # Recheck when saved credentials/settings have changed.
                refs = _resources(ctx, op, team)
                if all(_verified(ctx, _provider(ctx, refs[s], team)) for s in ("parser", "embedder", "store")):
                    return op
            else:
                return op
        if own.state == "running" and own.lease_expires_at and own.lease_expires_at > now_iso():
            raise Conflict("This setup step is already running. Refresh its status before retrying.")
        required = {
            "parser": [],
            "embedder": [],
            "store": [],
            "corpus": [],
            "pipeline": ["parser", "embedder", "store", "corpus"],
            "active": ["pipeline", "corpus"],
            "check_services": ["parser", "embedder", "store"],
        }[step]
        if any(next(s for s in op.steps if s.step == name).state != "succeeded" for name in required):
            raise Conflict("Complete the preceding setup tasks before this step.")
        conn.execute(
            "UPDATE onboarding_steps SET state='running', claim_id=?, lease_expires_at=?, attempts=attempts+1, error_code=NULL, updated_at=? WHERE operation_id=? AND step=?",
            (claim, after(LEASE_SECONDS), now_iso(), operation_id, step),
        )
    try:
        prepared = _prepare_provider(ctx, team, op, step) if step in {"parser", "embedder", "store"} else None
        checks: list[tuple[str, str, bool]] = []
        if step == "check_services":
            refs = _resources(ctx, op, team)
            for name in ("parser", "embedder", "store"):
                row = _provider(ctx, refs[name], team)
                before = provider_check_hash(ctx, row)
                result = ctx.providers.test(row.id)
                fresh = ctx.providers.get(row.id)
                # A check may refresh captured metadata. Credential/destination changes during it must fail closed.
                if row.updated_at != fresh.updated_at:
                    raise Conflict("A service changed during its connection check. Run the check again.")
                checks.append((row.id, before, bool(result["ok"])))
        lock = index_catalog_lock(ctx.settings.data_dir) if step == "pipeline" else nullcontext()
        with lock, ctx.db.write() as conn:
            require_setup(ctx, p, team)
            current = conn.execute(
                "SELECT claim_id FROM onboarding_steps WHERE operation_id=? AND step=? AND state='running'",
                (operation_id, step),
            ).fetchone()
            if current is None or current["claim_id"] != claim:
                raise Conflict("Another request resumed this setup step. Refresh its status.")
            op = operation(ctx, actor, team, operation_id)
            refs = _resources(ctx, op, team)
            resource_id = None
            if prepared is not None:
                existing = ctx.providers.find(prepared.id)
                if existing is None:
                    ctx.providers.insert_prepared(conn, prepared)
                resource_id = _provider(ctx, prepared.id, team).id
            elif step == "corpus":
                resource_id = new_id()
                try:
                    conn.execute(
                        "INSERT INTO corpora (id, name, metadata_schema, team_id, created_at) VALUES (?, ?, ?, ?, ?)",
                        (resource_id, op.draft.corpus_name.strip(), '{"fields":{}}', team, now_iso()),
                    )
                except sqlite3.IntegrityError:
                    raise Conflict("A corpus with this name already exists. Review your setup choices.") from None
            elif step == "pipeline":
                resource_id = create_pipeline(
                    ctx, refs["corpus"], _pipeline_config(ctx, op.draft, refs), connection=conn
                )
            elif step == "active":
                conn.execute(
                    "UPDATE corpora SET active_pipeline_id=? WHERE id=? AND team_id=?",
                    (refs["pipeline"], refs["corpus"], team),
                )
                resource_id = refs["pipeline"]
            elif step == "check_services":
                for provider_id, config_hash, ok in checks:
                    if provider_check_hash(ctx, ctx.providers.get(provider_id)) != config_hash:
                        raise Conflict("A service changed during its connection check. Run it again.")
                    conn.execute(
                        "INSERT INTO onboarding_provider_checks (provider_id, config_hash, checked_at, ok) VALUES (?, ?, ?, ?) ON CONFLICT (provider_id) DO UPDATE SET config_hash=excluded.config_hash, checked_at=excluded.checked_at, ok=excluded.ok",
                        (provider_id, config_hash, now_iso(), int(ok)),
                    )
            failed = step == "check_services" and not all(ok for _, _, ok in checks)
            conn.execute(
                "UPDATE onboarding_steps SET state=?, resource_id=?, claim_id=NULL, lease_expires_at=NULL, error_code=?, updated_at=? WHERE operation_id=? AND step=?",
                (
                    "failed" if failed else "succeeded",
                    resource_id,
                    "service_check_failed" if failed else None,
                    now_iso(),
                    operation_id,
                    step,
                ),
            )
            conn.execute("UPDATE onboarding_operations SET updated_at=? WHERE id=?", (now_iso(), operation_id))
        return operation(ctx, actor, team, operation_id)
    except Exception as exc:
        code = exc.error_code.value if isinstance(exc, RagError) else "setup_step_failed"
        ctx.db.execute(
            "UPDATE onboarding_steps SET state='failed', claim_id=NULL, lease_expires_at=NULL, error_code=?, updated_at=? WHERE operation_id=? AND step=? AND claim_id=?",
            (code, now_iso(), operation_id, step, claim),
        )
        raise


def _verified(ctx: AppContext, row: ProviderRow) -> bool:
    return bool(
        ctx.db.scalar(
            "SELECT ok FROM onboarding_provider_checks WHERE provider_id=? AND config_hash=?",
            (row.id, provider_check_hash(ctx, row)),
        )
    )


def indexed_documents(ctx: AppContext, corpus_id: str, pipeline_id: str) -> int:
    return int(
        ctx.db.scalar(
            "SELECT COUNT(*) FROM documents d WHERE d.corpus_id=? AND d.status='active' "
            "AND EXISTS (SELECT 1 FROM pipeline_nodes pn JOIN nodes n ON n.fingerprint=pn.node_fp WHERE pn.pipeline_id=? AND n.type LIKE 'index_%') "
            "AND NOT EXISTS (SELECT 1 FROM pipeline_nodes pn JOIN nodes n ON n.fingerprint=pn.node_fp "
            "WHERE pn.pipeline_id=? AND n.type LIKE 'index_%' AND NOT EXISTS "
            "(SELECT 1 FROM node_runs r JOIN indexes i ON i.corpus_id=r.corpus_id AND i.node_fp=r.node_fp "
            "WHERE r.document_id=d.id AND r.node_fp=pn.node_fp AND r.status='succeeded' AND r.doc_revision=d.revision AND i.broken=0 "
            "AND EXISTS (SELECT 1 FROM index_records ir WHERE ir.index_id=i.id AND ir.document_id=d.id)))",
            (corpus_id, pipeline_id, pipeline_id),
        )
    )


def readiness(ctx: AppContext, team: str, op: SetupOperation | None = None) -> SetupReadiness:
    owner = bool(
        ctx.db.scalar(
            "SELECT 1 FROM team_memberships m JOIN users u ON u.id=m.user_id WHERE m.team_id=? AND m.role='owner' AND u.status='active' LIMIT 1",
            (team,),
        )
    )
    corpus_id = (
        next((s.resource_id for s in op.steps if s.step == "corpus" and s.state == "succeeded"), None) if op else None
    )
    sql = "SELECT c.id AS corpus_id, p.* FROM corpora c JOIN pipelines p ON p.id=c.active_pipeline_id AND p.corpus_id=c.id WHERE c.team_id=? AND p.archived=0"
    args: tuple[Any, ...] = (team,)
    if corpus_id:
        sql += " AND c.id=?"
        args += (corpus_id,)
    row = ctx.db.one(sql + " ORDER BY c.created_at DESC LIMIT 1", args)
    if row is None:
        return SetupReadiness(owner=owner)
    provider_ids = json.loads(row["resolved"])["provider_ids"]
    providers = []
    for provider_id in provider_ids:
        try:
            provider = _provider(ctx, provider_id, team)
            providers.append(
                ProviderReadiness(
                    id=provider.id,
                    name=provider.name,
                    credentials={
                        k: bool(v["set"])
                        for k, v in ctx.providers.secret_states(provider.id, ctx.providers.spec(provider)).items()
                    },
                    verified=_verified(ctx, provider),
                )
            )
        except (Conflict, RagError):
            return SetupReadiness(
                owner=owner, corpus_id=row["corpus_id"], pipeline_id=row["id"], pipeline_fingerprint=row["fingerprint"]
            )
    count = indexed_documents(ctx, row["corpus_id"], row["id"])
    milestone = ctx.db.scalar(
        "SELECT succeeded_at FROM onboarding_first_results WHERE team_id=? AND corpus_id=? AND pipeline_id=? AND pipeline_fingerprint=?",
        (team, row["corpus_id"], row["id"], row["fingerprint"]),
    )
    services = bool(providers) and all(p.verified for p in providers)
    return SetupReadiness(
        owner=owner,
        corpus_id=row["corpus_id"],
        pipeline_id=row["id"],
        pipeline_fingerprint=row["fingerprint"],
        providers=providers,
        services=services,
        indexed_documents=count,
        first_result_at=milestone,
        ready=owner and services and count > 0 and milestone is not None,
    )


def record_first_result(ctx: AppContext, corpus_id: str, pipeline_id: str, result: dict[str, Any]) -> None:
    if not result.get("results") or result.get("partial"):
        return
    with ctx.db.write() as conn:
        row = conn.execute(
            "SELECT c.team_id, p.fingerprint FROM corpora c JOIN pipelines p ON p.corpus_id=c.id WHERE c.id=? AND p.id=? AND p.archived=0",
            (corpus_id, pipeline_id),
        ).fetchone()
        if row and indexed_documents(ctx, corpus_id, pipeline_id) > 0:
            conn.execute(
                "INSERT OR IGNORE INTO onboarding_first_results (team_id, corpus_id, pipeline_id, pipeline_fingerprint, succeeded_at) VALUES (?, ?, ?, ?, ?)",
                (row["team_id"], corpus_id, pipeline_id, row["fingerprint"], now_iso()),
            )


def get_state(ctx: AppContext, p: Principal, team: str) -> SetupState:
    actor = require_setup(ctx, p, team)
    draft = get_draft(ctx, actor, team)
    last = ctx.db.scalar(
        "SELECT id FROM onboarding_operations WHERE user_id=? AND team_id=? ORDER BY created_at DESC LIMIT 1",
        (actor, team),
    )
    op = operation(ctx, actor, team, last) if last else None
    return SetupState(draft=draft, draft_hash=draft_hash(draft.draft), operation=op, readiness=readiness(ctx, team, op))
