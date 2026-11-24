"""Provider calls on the query path."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, TypeVar

from ragcentre.errors import ExecutionError, ProviderFailed

if TYPE_CHECKING:
    from ragcentre.context import AppContext

T = TypeVar("T")


def provider_call(ctx: AppContext, provider_id: str, call: Callable[[], T]) -> T:
    """The query path does not retry: a provider failure is a 502 naming the provider."""
    try:
        return call()
    except ExecutionError as exc:
        name = exc.provider or ctx.providers.get(provider_id).name
        raise ProviderFailed(f"{name} failed: {exc.message}", details={"provider": name, "cause": exc.code}) from None
