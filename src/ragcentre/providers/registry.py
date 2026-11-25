"""Provider-type registry (section 8.5).

Types register through the ``ragcentre.provider_types`` entry-point group (each entry point is a
``ProviderTypeSpec``); vector store types come from ``ragcentre.vector_stores`` and are wrapped
here. Built-in types are always present, so a source checkout works without installed metadata.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from functools import cache
from importlib import import_module
from importlib.metadata import entry_points
from typing import Any

from ragcentre.providers.base import BuildEnv, ProviderTypeSpec
from ragcentre.stores.registry import VectorStoreType, vector_store_types

log = logging.getLogger("ragcentre.providers")

GROUP = "ragcentre.provider_types"
BUILTIN = (
    "ragcentre.providers.parsers.builtin_text:SPEC",
    "ragcentre.providers.parsers.http:SPEC",
    "ragcentre.providers.chunkers.http:SPEC",
    "ragcentre.providers.embedders.builtin_hash:SPEC",
    "ragcentre.providers.embedders.openai_compatible:SPEC",
    "ragcentre.providers.rerankers.http:SPEC",
    "ragcentre.providers.llms.openai_compatible:SPEC",
    "ragcentre.connectors.s3:SPEC",
    "ragcentre.connectors.sharepoint:SPEC",
    "ragcentre.connectors.confluence:SPEC",
    "ragcentre.connectors.gdrive:SPEC",
    "ragcentre.connectors.notion:SPEC",
)


def _load(target: str) -> ProviderTypeSpec:
    module_name, _, attr = target.partition(":")
    value = getattr(import_module(module_name), attr)
    if not isinstance(value, ProviderTypeSpec):
        raise TypeError(f"{target} is not a ProviderTypeSpec")
    return value


def _store_spec(st: VectorStoreType) -> ProviderTypeSpec:
    def factory(config: Any, secrets: dict[str, str], env: BuildEnv) -> Any:
        if st.info_protocol == "ragcentre.vector_store/v1" and st.type == "http":
            from ragcentre.stores.http import HttpStore

            return HttpStore.connect(
                url=config.url,
                timeout=config.timeout,
                api_key=secrets.get("api_key") or None,
                http_client_factory=env.http,
            )
        return st.factory(config, secrets, env.data_dir)

    return ProviderTypeSpec(
        kind="vector_store",
        type=st.type,
        label=st.label,
        description=st.description,
        config_model=st.config_model,
        # A store's fingerprint is its provider id (see manager.fingerprint_for), so these fields
        # do not enter it. Listing them here makes ProviderManager.update treat them as permanent
        # (409 immutable_field) and the API/UI report them as fixed after creation.
        fingerprint_fields=st.immutable_fields,
        secret_keys=st.secret_keys,
        factory=factory,
        capabilities=st.capabilities,
        captured_fields=st.captured_fields,
        info_protocol=st.info_protocol,
    )


@cache
def provider_types() -> Mapping[tuple[str, str], ProviderTypeSpec]:
    found: dict[tuple[str, str], ProviderTypeSpec] = {}
    for target in BUILTIN:
        spec = _load(target)
        found[spec.key] = spec
    for ep in entry_points(group=GROUP):
        try:
            value = ep.load()
        except Exception:
            log.warning("could not load provider type", extra={"entry_point": ep.name}, exc_info=True)
            continue
        if isinstance(value, ProviderTypeSpec) and value.key not in found:
            found[value.key] = value
    for st in vector_store_types().values():
        spec = _store_spec(st)
        found.setdefault(spec.key, spec)
    return found


def get_type(kind: str, type_: str) -> ProviderTypeSpec | None:
    return provider_types().get((kind, type_))
