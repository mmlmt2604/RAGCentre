from __future__ import annotations

from fastapi.testclient import TestClient

from ragcentre.context import AppContext
from ragcentre.providers.registry import provider_types


def test_connector_s3_type_registered() -> None:
    assert ("connector", "s3") in provider_types()
    spec = provider_types()[("connector", "s3")]
    assert spec.kind == "connector"
    assert spec.secret_keys == ("access_key_id", "secret_access_key", "session_token")
    assert spec.fingerprint_fields == ("base_url", "region", "path_style")


def test_all_connector_types_registered() -> None:
    kinds = {spec.type for (kind, _type), spec in provider_types().items() if kind == "connector"}
    assert kinds == {"s3", "sharepoint", "confluence", "gdrive", "notion"}


def _connector(client: TestClient, name: str = "s3test") -> dict:
    r = client.post("/v1/providers", json={"name": name, "kind": "connector", "type": "s3", "config": {}})
    assert r.status_code == 201, r.text
    return r.json()


def test_provider_types_lists_connector(client: TestClient) -> None:
    r = client.get("/v1/provider-types")
    assert r.status_code == 200
    items = {(t["kind"], t["type"]): t for t in r.json()["items"]}
    assert ("connector", "s3") in items
    assert items[("connector", "s3")]["secret_keys"] == ["access_key_id", "secret_access_key", "session_token"]


def test_delete_connector_used_by_source_is_conflict(client: TestClient, ctx: AppContext) -> None:
    corpus = client.post("/v1/corpora", json={"name": "c"}).json()
    provider = _connector(client)
    r = client.post(
        f"/v1/corpora/{corpus['id']}/sources",
        json={"name": "s", "provider_id": provider["id"], "scope": {"bucket": "b"}, "schedule_seconds": 0},
    )
    assert r.status_code == 201, r.text
    source_id = r.json()["id"]

    r = client.delete(f"/v1/providers/{provider['id']}")
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "provider_in_use"

    # Deleting the source frees the provider.
    assert client.delete(f"/v1/sources/{source_id}").status_code == 204
    assert client.delete(f"/v1/providers/{provider['id']}").status_code == 204
    assert ctx.providers.find(provider["id"]) is None
