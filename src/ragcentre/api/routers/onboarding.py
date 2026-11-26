"""Human-session-only durable team setup endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ragcentre.api.authz import principal_of, require_team
from ragcentre.api.deps import get_ctx
from ragcentre.api.permissions import SessionRoute
from ragcentre.context import AppContext
from ragcentre.onboarding import service
from ragcentre.onboarding.models import DraftWrite, OperationCreate, SetupOperation, SetupState, StepName

router = APIRouter(route_class=SessionRoute, tags=["team setup"])


@router.get("/teams/{team_id}/setup", response_model=SetupState)
def get_setup(team_id: str, request: Request, ctx: AppContext = Depends(get_ctx)) -> SetupState:
    require_team(principal_of(request), team_id)
    return service.get_state(ctx, principal_of(request), team_id)


@router.put("/teams/{team_id}/setup-draft", response_model=SetupState)
def put_draft(team_id: str, body: DraftWrite, request: Request, ctx: AppContext = Depends(get_ctx)) -> SetupState:
    require_team(principal_of(request), team_id)
    return service.save_draft(ctx, principal_of(request), team_id, body.expected_version, body.draft)


@router.post("/teams/{team_id}/setup-operations", response_model=SetupOperation)
def create_operation(
    team_id: str, body: OperationCreate, request: Request, ctx: AppContext = Depends(get_ctx)
) -> SetupOperation:
    require_team(principal_of(request), team_id)
    return service.create_operation(ctx, principal_of(request), team_id, body)


@router.get("/teams/{team_id}/setup-operations/{operation_id}", response_model=SetupOperation)
def get_operation(
    team_id: str, operation_id: str, request: Request, ctx: AppContext = Depends(get_ctx)
) -> SetupOperation:
    require_team(principal_of(request), team_id)
    actor = service.require_setup(ctx, principal_of(request), team_id)
    return service.operation(ctx, actor, team_id, operation_id)


@router.post("/teams/{team_id}/setup-operations/{operation_id}/steps/{step}", response_model=SetupOperation)
def execute_step(
    team_id: str, operation_id: str, step: StepName, request: Request, ctx: AppContext = Depends(get_ctx)
) -> SetupOperation:
    require_team(principal_of(request), team_id)
    return service.execute_step(ctx, principal_of(request), team_id, operation_id, step)
