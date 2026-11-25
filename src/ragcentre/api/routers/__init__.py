"""API routers, mounted under ``/v1``."""

from fastapi import APIRouter

from ragcentre.api.routers import (
    admin,
    auth,
    corpora,
    documents,
    eval,
    ocr,
    pipelines,
    providers,
    query,
    runs,
    sources,
    teams,
    users,
)

ROUTERS: list[APIRouter] = [
    admin.router,
    auth.router,
    users.router,
    teams.router,
    providers.router,
    ocr.router,
    corpora.router,
    pipelines.router,
    documents.router,
    query.router,
    runs.router,
    eval.router,
    sources.router,
]
