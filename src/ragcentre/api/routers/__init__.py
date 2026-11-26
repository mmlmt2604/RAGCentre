"""API routers, mounted under ``/v1``."""

from fastapi import APIRouter

from ragcentre.api.routers import admin, auth, corpora, documents, eval, keys, ocr, pipelines, providers, query, runs

ROUTERS: list[APIRouter] = [
    admin.router,
    auth.router,
    keys.router,
    providers.router,
    ocr.router,
    corpora.router,
    pipelines.router,
    documents.router,
    query.router,
    runs.router,
    eval.router,
]
