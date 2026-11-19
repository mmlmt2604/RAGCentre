## pyproject extras
none. `src/ragcentre/stores/http.py` uses `httpx` (already a core dependency). The reference shim
`examples/vector_store_shims/memory/` has its own `pyproject.toml` (fastapi, uvicorn; `ragcentre`
as the `local` extra) and is not part of the main project's dependencies. No `uv.lock` change.

Optional: entry point for symmetry with the other built-ins (not needed, BUILTIN already loads it):
`http = "ragcentre.stores.http:STORE_TYPE"` under `[project.entry-points."ragcentre.vector_stores"]`.

## registry BUILTIN
"http": "ragcentre.stores.http:STORE_TYPE"

## docker-compose service
none (in-process test target).

## ci stores matrix entry
none. `tests/test_v1_http_store.py` runs in the default `uv run pytest` (the shim runs in-process).

## README store-table row
| `http` | none (core `httpx`) | cosine, l2, dot (as the service reports) | exact filters, consistency and max dim as reported by the service's `/info` | `type: http`, `config: {url: http://127.0.0.1:8702}`, secret `api_key`; any database behind a `ragcentre.vector_store/v1` service ([protocol](docs/protocols/vector-store-v1.md), [guide](docs/protocols/vector-store-shim-guide.md)) |

## DECISIONS.md
Phase V3: HTTP protocol `ragcentre.vector_store/v1` and the built-in `http` store (t-http).

1. **The protocol mirrors the eight methods one to one** (plan Decision 7). `PUT /collections/{name}`
   is `ensure_collection` (200, or 409 `collection_mismatch`; fields are additive, a changed field
   type is a mismatch), `GET` is `collection_info` (404 `collection_not_found` becomes `None`),
   `DELETE` and `/delete` are idempotent (204), `/upsert` is 204, `/query` returns
   `{matches: [{id, score}]}`. Filters travel as RAGCentre's JSON language (`to_json`), so shims
   translate and the core never learns a dialect. Errors are `{error: {code, message}}` with
   `invalid_request` (400), `unauthorized` (401), `collection_not_found` (404),
   `collection_mismatch` (409), `unavailable` (503). The score convention (cosine similarity, raw dot
   product, −Euclidean, with squared-distance conversion) and filter-before-search are normative.
2. **`/info` shape**: `{protocol, service_name, service_version, capabilities}`, with
   `capabilities` = `metrics`, `max_batch` (required) and `max_dim`, `consistency`, `filter_exact`,
   `max_collections` (optional, defaults as in `Capabilities`). This differs from the parser's
   `name`/`version` keys on purpose: the task brief specified `service_name`/`service_version`.
3. **`/info` is read without credentials, and the bearer token is added to the client only after
   it** (as for parsers). A test records the headers of `/info` and `/health` to prove it.
4. **Capabilities are per instance and captured at creation** from `/info` (strictly validated; a
   bad `/info` raises `StoreError` when the provider is created). They are not refreshed, so a
   service that changes limits needs a new provider (or restart of RAGCentre). The type-level
   `STORE_TYPE.capabilities` is the best case for listing in the UI.
5. **Retries: writes only.** `PUT`, `DELETE /collections/{name}`, `/upsert`, `/delete` get up to 3
   attempts (connection errors, timeouts, 408, 429, 5xx; `Retry-After` honoured, capped at 5 s;
   0.25 s exponential backoff). `/health`, `GET /collections/{name}` and `/query` are never
   retried (§20). A 4xx is permanent even on a write. Tests: 503 twice then success for PUT,
   exactly one attempt for a failing query, exactly one for a 400 write.
6. **Config**: `url` (required), `timeout` (default 30 s, 0 < t <= 3600); secret `api_key`. Both
   are mutable connection fields, no `immutable_fields`. The store's identity is its provider id
   (as for every store), so a service-side version change never rewrites an index.
7. **Reference shim** (`examples/vector_store_shims/memory/`, package `memory_shim`): FastAPI over
   dicts, exact scoring with `ragcentre.vectors`, filters with `parse_filter` + `matches`, options
   `api_key` / `MEMORY_SHIM_API_KEY`, `max_batch`, `max_dim`. The service module is
   `memory_shim.service` (not `app`), because a submodule called `app` would shadow the lazy
   `memory_shim.app` attribute that `uvicorn memory_shim:app` loads.
8. **What was run vs only read.** Run: the full `VectorStoreContract` suite over HTTP against the
   in-process shim (`TestClient`), in two configurations (no limits; `max_dim=8`, `max_batch=50`,
   which also runs `test_max_dim_is_enforced`); error-path tests (404, 409, 400, 401); a manual
   round trip over real HTTP with `uvicorn` and a bearer token (connect, health, ensure, upsert,
   filtered query, info, drop, wrong key gives 401). Only read, not run: nothing here depends on a
   vendor library. `ragcentre store check` does not exist on this branch (it comes with t-check), so
   the plan's acceptance item "`store check` works against the shim" is for the lead to run at merge.
