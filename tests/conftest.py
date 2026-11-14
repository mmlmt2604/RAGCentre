from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ragcentre.api.app import create_app
from ragcentre.context import AppContext, open_context
from ragcentre.settings import Settings
from ragcentre.testing.fakes import FakeServices


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", workers=0)


@pytest.fixture
def fakes() -> FakeServices:
    return FakeServices()


@pytest.fixture
def ctx(settings: Settings, fakes: FakeServices) -> Iterator[AppContext]:
    context = open_context(settings, http_client_factory=fakes.client_factory)
    yield context
    context.close()


@pytest.fixture
def client(ctx: AppContext) -> Iterator[TestClient]:
    with TestClient(create_app(ctx)) as c:
        yield c
