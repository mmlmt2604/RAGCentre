"""Authorization: resolve a request to a principal and enforce team ownership.

Every non-global resource hangs off a corpus, and a corpus has exactly one owning team, so the
whole model collapses to one question: "may this principal touch this corpus?". Documents,
pipelines, node runs, evaluation and (future) connectors inherit their team through ``corpus_id``;
providers carry their own ``team_id`` (``NULL`` = system/built-in only).

Handlers call the ``require_*`` functions here, never ``request.state.principal`` directly.
Cross-team resource ids raise ``NotFound`` (they must be indistinguishable from ids that do not
exist), while "you may read but not write" raises ``Forbidden``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, cast

from fastapi import Request

from ragcentre.context import AppContext
from ragcentre.errors import Forbidden, NotFound

# Matches the literal inserted by migration 0005.
DEFAULT_TEAM_ID = "00000000-0000-0000-0000-000000000001"

Scope = Literal["admin", "team", "read"]


@dataclass(frozen=True)
class Principal:
    scope: Scope
    team_id: str | None  # None for unscoped administration
    actor_kind: str = "token"
    actor_id: str | None = None
    session_id: str | None = None
    csrf_token: str | None = None
    role: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.scope == "admin"

    @property
    def unrestricted(self) -> bool:
        return self.is_admin and self.actor_kind != "user"


def principal_of(request: Request) -> Principal:
    return cast(Principal, request.state.principal)


# Routes a read-scoped team token may call. Every other route needs a full-team or admin token.
# A new route defaults to full-token only: add it here deliberately, never by omission.
READ_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/v1/health"),
        ("GET", "/v1/auth/whoami"),
        ("GET", "/v1/corpora"),
        ("GET", "/v1/corpora/{corpus_id}"),
        ("GET", "/v1/documents/{document_id}"),
        ("GET", "/v1/documents/{document_id}/content"),
        ("GET", "/v1/documents/{document_id}/chunks"),
        ("GET", "/v1/documents/{document_id}/parsed"),
        ("GET", "/v1/pipelines/{pipeline_id}"),
        ("GET", "/v1/pipelines/{pipeline_id}/status"),
        ("POST", "/v1/corpora/{corpus_id}/query"),
    }
)


def _template_to_regex(template: str) -> str:
    """Convert a Starlette route template (simple ``{param}`` segments) to a regex fragment."""
    parts = template.split("{")
    out = re.escape(parts[0])
    for part in parts[1:]:
        _name, rest = part.split("}", 1)
        out += "[^/]+" + re.escape(rest)
    return out


# Compiled matchers for the middleware, which sees only the raw path (route matching happens later).
_READ_ALLOWLIST_REGEX: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (method, re.compile("^" + _template_to_regex(template) + "$")) for method, template in READ_ALLOWLIST
)


def read_allowed(method: str, path: str) -> bool:
    return any(m == method and rx.match(path) for m, rx in _READ_ALLOWLIST_REGEX)


def require_admin(p: Principal) -> None:
    if not p.is_admin:
        raise Forbidden("This action requires the admin token.")


# ---- team resolution ---------------------------------------------------------------------------
# Each raises NotFound when the resource is absent. team_of_provider returns None for a built-in.


def team_of_corpus(ctx: AppContext, corpus_id: str) -> str:
    row = ctx.db.one("SELECT team_id FROM corpora WHERE id = ?", (corpus_id,))
    if row is None:
        raise NotFound(f"Corpus {corpus_id} was not found.")
    return str(row["team_id"])


def team_of_document(ctx: AppContext, document_id: str) -> str:
    row = ctx.db.one(
        "SELECT team_id FROM corpora c JOIN documents d ON d.corpus_id = c.id WHERE d.id = ?", (document_id,)
    )
    if row is None:
        raise NotFound(f"Document {document_id} was not found.")
    return str(row["team_id"])


def team_of_pipeline(ctx: AppContext, pipeline_id: str) -> str:
    row = ctx.db.one(
        "SELECT team_id FROM corpora c JOIN pipelines p ON p.corpus_id = c.id WHERE p.id = ?", (pipeline_id,)
    )
    if row is None:
        raise NotFound(f"Pipeline {pipeline_id} was not found.")
    return str(row["team_id"])


def team_of_dataset(ctx: AppContext, dataset_id: str) -> str:
    row = ctx.db.one(
        "SELECT c.team_id FROM corpora c JOIN eval_datasets d ON d.corpus_id = c.id WHERE d.id = ?", (dataset_id,)
    )
    if row is None:
        raise NotFound(f"Dataset {dataset_id} was not found.")
    return str(row["team_id"])


def team_of_eval_run(ctx: AppContext, run_id: str) -> str:
    row = ctx.db.one(
        "SELECT c.team_id FROM corpora c JOIN eval_datasets d ON d.corpus_id = c.id "
        "JOIN eval_runs r ON r.dataset_id = d.id WHERE r.id = ?",
        (run_id,),
    )
    if row is None:
        raise NotFound(f"Evaluation run {run_id} was not found.")
    return str(row["team_id"])


def team_of_node_run(ctx: AppContext, run_id: int) -> str:
    row = ctx.db.one("SELECT team_id FROM corpora c JOIN node_runs r ON r.corpus_id = c.id WHERE r.id = ?", (run_id,))
    if row is None:
        raise NotFound(f"Node run {run_id} was not found.")
    return str(row["team_id"])


def team_of_provider(ctx: AppContext, provider_id: str) -> str | None:
    row = ctx.db.one("SELECT team_id FROM providers WHERE id = ?", (provider_id,))
    if row is None:
        raise NotFound(f"Provider {provider_id} was not found.")
    return None if row["team_id"] is None else str(row["team_id"])


def team_of_source(ctx: AppContext, source_id: str) -> str:
    row = ctx.db.one(
        "SELECT c.team_id FROM corpora c JOIN sync_sources s ON s.corpus_id = c.id WHERE s.id = ?", (source_id,)
    )
    if row is None:
        raise NotFound(f"Source {source_id} was not found.")
    return str(row["team_id"])


# ---- checks ------------------------------------------------------------------------------------
# Admin always passes. Otherwise the resource's team must equal the principal's team.


def require_corpus(ctx: AppContext, p: Principal, corpus_id: str) -> None:
    if p.unrestricted:
        return
    if p.team_id != team_of_corpus(ctx, corpus_id):
        raise NotFound(f"Corpus {corpus_id} was not found.")


def require_document(ctx: AppContext, p: Principal, document_id: str) -> None:
    if p.unrestricted:
        return
    if p.team_id != team_of_document(ctx, document_id):
        raise NotFound(f"Document {document_id} was not found.")


def require_pipeline(ctx: AppContext, p: Principal, pipeline_id: str) -> None:
    if p.unrestricted:
        return
    if p.team_id != team_of_pipeline(ctx, pipeline_id):
        raise NotFound(f"Pipeline {pipeline_id} was not found.")


def require_dataset(ctx: AppContext, p: Principal, dataset_id: str) -> None:
    if p.unrestricted:
        return
    if p.team_id != team_of_dataset(ctx, dataset_id):
        raise NotFound(f"Dataset {dataset_id} was not found.")


def require_eval_run(ctx: AppContext, p: Principal, run_id: str) -> None:
    if p.unrestricted:
        return
    if p.team_id != team_of_eval_run(ctx, run_id):
        raise NotFound(f"Evaluation run {run_id} was not found.")


def require_node_run(ctx: AppContext, p: Principal, run_id: int) -> None:
    if p.unrestricted:
        return
    if p.team_id != team_of_node_run(ctx, run_id):
        raise NotFound(f"Node run {run_id} was not found.")


def require_provider(ctx: AppContext, p: Principal, provider_id: str) -> None:
    if p.unrestricted:
        return
    team = team_of_provider(ctx, provider_id)
    if team is not None and team != p.team_id:
        raise NotFound(f"Provider {provider_id} was not found.")


def require_provider_owner(ctx: AppContext, p: Principal, provider_id: str) -> None:
    """Mutate a provider. Built-ins are admin-only; other providers are owner/admin only."""
    if p.unrestricted:
        return
    team = team_of_provider(ctx, provider_id)
    if team is None:
        if p.is_admin:
            return
        raise Forbidden("Built-in providers can only be changed by a platform administrator.")
    if team != p.team_id:
        raise NotFound(f"Provider {provider_id} was not found.")


def require_source(ctx: AppContext, p: Principal, source_id: str) -> None:
    if p.unrestricted:
        return
    if p.team_id != team_of_source(ctx, source_id):
        raise NotFound(f"Source {source_id} was not found.")
    provider = ctx.db.one(
        "SELECT p.team_id FROM sync_sources s JOIN providers p ON p.id=s.provider_id WHERE s.id=?", (source_id,)
    )
    if provider is None or provider["team_id"] != p.team_id:
        raise NotFound(f"Source {source_id} was not found.")


def require_team(p: Principal, team_id: str) -> None:
    """Admin or the team itself; a different team sees the id as not found."""
    if p.unrestricted or p.team_id == team_id:
        return
    raise NotFound(f"Team {team_id} was not found.")
