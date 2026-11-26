"""API routers, mounted under ``/v1``."""

from fastapi import APIRouter

from ragcentre.api.routers import (
    account_onboarding,
    admin,
    auth,
    corpora,
    documents,
    eval,
    ocr,
    onboarding,
    pipelines,
    providers,
    query,
    runs,
    sources,
    teams,
    users,
)

ROUTERS: list[APIRouter] = [
    account_onboarding.router,
    admin.router,
    auth.router,
    users.router,
    teams.router,
    providers.router,
    ocr.router,
    corpora.router,
    pipelines.router,
    onboarding.router,
    documents.router,
    query.router,
    runs.router,
    eval.router,
    sources.router,
]
