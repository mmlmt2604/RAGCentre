"""Phase V7: the README store table, `ragcentre store list`, and the example provider files."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from ragcentre.cli import app
from ragcentre.providers.manager import ENV_NAME_RE, NAME_RE
from ragcentre.providers.registry import provider_types
from ragcentre.stores import docs
from ragcentre.stores.registry import BUILTIN, vector_store_types

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples" / "providers"
STORE_NAMES = sorted(BUILTIN)


def example_files() -> list[Path]:
    return sorted(EXAMPLES.glob("*.yaml"))


def load(path: Path) -> dict[str, Any]:
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(spec, dict), path
    return spec


def store_examples() -> list[Path]:
    return [p for p in example_files() if load(p)["kind"] == "vector_store"]


# ---- the README table -------------------------------------------------------------------------


def test_readme_table_matches_the_registry() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert readme.count(docs.START) == 1 and readme.count(docs.END) == 1
    block = readme[readme.index(docs.START) : readme.index(docs.END) + len(docs.END)]
    assert block == docs.readme_block(), (
        "README's vector store table is out of date. Regenerate it with `ragcentre store list --markdown` "
        "and paste it between the vector-stores markers."
    )


def test_every_builtin_store_type_has_docs() -> None:
    assert set(docs.STORE_DOCS) == set(BUILTIN), (
        "add (or remove) a StoreDoc in ragcentre/stores/docs.py for every built-in store type"
    )
    assert set(vector_store_types()) >= set(BUILTIN)


@pytest.mark.parametrize("name", STORE_NAMES)
def test_store_doc_config_example_validates(name: str) -> None:
    t = vector_store_types()[name]
    d = docs.STORE_DOCS[name]
    t.config_model.model_validate(d.config)
    assert "|" not in docs.config_line(d)  # it sits in a Markdown table cell
    assert (EXAMPLES / d.example).is_file()
    assert load(EXAMPLES / d.example)["type"] == name
    if d.doc:
        assert (ROOT / d.doc).is_file()


def test_store_doc_extras_exist_in_pyproject() -> None:
    extras = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["optional-dependencies"]
    for name, d in docs.STORE_DOCS.items():
        if d.extra is not None:
            assert d.extra in extras, f"{name}: extra {d.extra!r} is not in pyproject.toml"


def test_table_has_one_row_per_store_and_flags_weaker_guarantees() -> None:
    lines = [ln for ln in docs.markdown_table().splitlines() if ln.startswith("| `")]
    assert [ln.split("`")[1] for ln in lines] == list(BUILTIN)
    pinecone = next(ln for ln in lines if ln.startswith("| `pinecone`"))
    assert "**no**" in pinecone and "**eventual**" in pinecone
    assert "**no**" not in next(ln for ln in lines if ln.startswith("| `qdrant`"))


# ---- `ragcentre store list` ---------------------------------------------------------------------


def test_cli_store_list_markdown_is_the_table() -> None:
    result = CliRunner().invoke(app, ["store", "list", "--markdown"])
    assert result.exit_code == 0, result.output
    assert result.output.rstrip("\n") == docs.markdown_table()


def test_cli_store_list_plain_and_json() -> None:
    result = CliRunner().invoke(app, ["store", "list"])
    assert result.exit_code == 0, result.output
    header = result.output.splitlines()[0]
    for column in ("TYPE", "INSTALL", "METRICS", "EXACT FILTERS", "CONSISTENCY", "MAX DIM", "MAX BATCH", "SECRETS"):
        assert column in header
    assert {line.split()[0] for line in result.output.splitlines()[2:]} >= set(BUILTIN)

    result = CliRunner().invoke(app, ["--json", "store", "list"])
    rows = json.loads(result.output)
    by_type = {r["type"]: r for r in rows}
    assert set(by_type) >= set(BUILTIN)
    assert by_type["pinecone"]["exact_filters"] == "no" and by_type["pinecone"]["consistency"] == "eventual"
    assert by_type["sqlite_vec"]["install"] == "built in"


# ---- API: what the UI shows next to the type picker ---------------------------------------------


def test_provider_types_expose_full_capabilities(client: TestClient) -> None:
    items = {(t["kind"], t["type"]): t for t in client.get("/v1/provider-types").json()["items"]}
    for name, t in vector_store_types().items():
        item = items[("vector_store", name)]
        caps = item["capabilities"]
        assert caps["max_dim"] == t.capabilities.max_dim
        assert caps["consistency"] == t.capabilities.consistency
        assert caps["filter_exact"] == t.capabilities.filter_exact
        assert caps["max_collections"] == t.capabilities.max_collections
        # set-once fields reach the UI as the fingerprint fields
        assert item["fingerprint_fields"] == list(t.immutable_fields)


# ---- example provider files ---------------------------------------------------------------------


@pytest.mark.parametrize("path", example_files(), ids=lambda p: p.name)
def test_example_provider_file_shape(path: Path) -> None:
    spec = load(path)
    assert set(spec) <= {"name", "kind", "type", "config", "secrets"}, path
    assert {"name", "kind", "type", "config"} <= set(spec)
    assert NAME_RE.match(spec["name"])
    assert (spec["kind"], spec["type"]) in provider_types()
    assert isinstance(spec["config"], dict)
    for key, ref in (spec.get("secrets") or {}).items():
        # `provider add` accepts only environment references, so no example can carry a value
        assert isinstance(ref, dict) and set(ref) == {"env"}, f"{path.name}: secret {key}"
        assert ENV_NAME_RE.match(ref["env"])


@pytest.mark.parametrize("path", store_examples(), ids=lambda p: p.name)
def test_store_example_config_and_secrets_validate(path: Path) -> None:
    spec = load(path)
    t = vector_store_types()[spec["type"]]
    t.config_model.model_validate(spec["config"])
    assert set(spec.get("secrets") or {}) <= set(t.secret_keys)
    # no credentials in a URL either (comments may show the shape of one)
    body = "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")
    )
    assert not re.search(r"://[^/\s@]*:[^/\s@]*@", body)


def test_every_store_type_has_a_provider_example() -> None:
    covered = {load(p)["type"] for p in store_examples()}
    assert covered >= set(BUILTIN) - {"sqlite_vec"}
    assert "sqlite_vec" in covered
