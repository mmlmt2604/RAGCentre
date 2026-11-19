## pyproject extras

No change. The `chroma` extra already exists (`chromadb>=1.5.9`); 1.5.9 is the version this task
was implemented and tested against (installed with `uv sync --all-extras` on Python 3.13).

## registry BUILTIN

No change. `"chroma": "ragcentre.stores.chroma:STORE_TYPE"` already exists.

## docker-compose service (host port 58000; compose project `rc-t-chroma`)

```yaml
  chroma:
    image: chromadb/chroma:1.5.9   # tag NOT pulled or run here; confirm it exists before merging
    ports:
      - "127.0.0.1:58000:8000"
    healthcheck:
      # /api/v2/heartbeat was verified against `chroma run` from chromadb 1.5.9 (see DECISIONS below)
      test: ["CMD-SHELL", "curl -sf http://localhost:8000/api/v2/heartbeat || exit 1"]
      interval: 5s
      timeout: 3s
      retries: 20
```

If the image has no `curl`, use a TCP check instead. `tests/test_m9_chroma_server.py` skips when
nothing answers on `RAGCENTRE_TEST_CHROMA_HOST`/`RAGCENTRE_TEST_CHROMA_PORT` (default
`localhost:58000`).

## ci stores matrix entry

Service `chroma`, extra `chroma`, Python 3.13, test modules `tests/test_m9_chroma.py` (in-process;
already in the job) and `tests/test_m9_chroma_server.py` (needs the service on port 58000).
Chroma Cloud has no CI entry (no credentials); see DECISIONS.

## README store-table row

| `chroma` | `ragcentre[chroma]` | cosine, l2, dot | filter-exact; immediate; no `max_dim`; `max_batch` 1000 (300 on Chroma Cloud) | `{mode: cloud, tenant: …, database: …}` + secret `api_key` |

## DECISIONS.md

M9, after the existing Chroma items (numbers continue from the last M9 item; renumber on apply):

N. **Chroma deployment modes.** `ChromaConfig` gains `mode` (`local` | `http` | `cloud`),
   `tenant`, `database`, `cloud_host` (default `api.trychroma.com`), `token_header` (default
   `Authorization`) and `prefix` (default empty). `mode` is optional for configs written before
   this phase: `host` set → `http`, otherwise `local`. `local` uses `PersistentClient` (path
   default `<data dir>/chroma`); `http` uses `HttpClient(host, port, ssl, headers)`; `cloud`
   uses `CloudClient(tenant, database, api_key, cloud_host)` with the `api_key` secret (required).
   `secret_keys` is `("token", "api_key")`: `token` for `http`, `api_key` for `cloud`.
   `cloud` rejects `host`/`path`, `http` requires `host` and rejects `path`, `local` rejects
   `host`. (plan V4.3)

N+1. **`token_header`.** `Authorization` sends `Bearer <token>`; any other header name sends the
   raw token (`x-chroma-token` for Chroma Cloud style auth and proxies). It applies to `http`
   mode only: `CloudClient` fixes `X-Chroma-Token` itself.

N+2. **Chroma Cloud batch limit.** `mode=cloud` sets the instance `capabilities.max_batch` to
   300 and skips the server's reported limit. Other modes keep `min(server-reported, 1000)`. The
   registry type's `capabilities` keep the best case of 1000. `ChromaStore.capabilities` is now a
   plain (non-`ClassVar`) attribute, set per instance.

N+3. **Tenant and database.** `local` mode creates a missing tenant and database in the embedded
   directory through `AdminClient` before opening the client (Chroma refuses to open a missing
   one). `http` and `cloud` never create them: the server owns them, and a missing one surfaces
   as a `StoreError` naming the tenant and database. Client construction failures in `http` and
   `cloud` are wrapped in `StoreError` for the same reason.

N+4. **HNSW options.** `hnsw_max_neighbors` and `hnsw_ef_construction` are immutable
   (`VectorStoreType.immutable_fields`; update answers 409 `immutable_field`). They are passed in
   `configuration.hnsw` (`max_neighbors`, `ef_construction`) and recorded in the collection
   metadata as `rc_hnsw_max_neighbors` / `rc_hnsw_ef_construction`, next to the existing `rc_dim`
   and `rc_fields`. A key is written only when its option is set (Chroma's bindings reject `None`
   in collection metadata), so an unset option reads back as `None`. `ensure_collection` raises
   `CollectionMismatch` when the recorded pair differs from the store's, including when options
   are requested for a collection that was built without them. Collections built before this
   phase therefore still open and query with a store that sets no options.
   `ef_search` is mutable: it is passed at creation and re-applied on every `ensure_collection`
   with `Collection.modify(configuration={"hnsw": {"ef_search": …}})` when it differs.

N+5. **Chroma `prefix`.** Prepended to the logical `rc_…` name to give the physical collection
   name, which is re-validated against Chroma's naming rule (3-63 characters, alphanumeric at both
   ends, `[a-zA-Z0-9._-]` inside, no `..`, not an IPv4 literal; `StoreError` otherwise). The config
   accepts only `[a-zA-Z0-9._-]` and no `..`.

**Verified against the installed `chromadb` 1.5.9** (read from source, not from memory):
`CloudClient(tenant, database, api_key, settings, *, cloud_host, cloud_port, enable_ssl)` and
that it fixes `X-Chroma-Token` and raises "Permission denied" without a reachable server;
`HttpClient(host, port, ssl, headers, settings, tenant, database)`; `PersistentClient(path,
settings, tenant, database)`; `HnswIndexConfig`/`HNSWConfiguration` fields and
`UpdateHNSWConfiguration` (only `ef_search` and the runtime options are updatable, so
`max_neighbors`/`ef_construction` are creation-only); `get_or_create_collection` ignores
`configuration`/`metadata` for an existing collection; `check_index_name`; `Collection.modify`.

**Run, not only read** (Python 3.13, chromadb 1.5.9):
- the whole contract suite plus the options contract in-process (`PersistentClient`,
  `EphemeralClient`), including tenant/database/prefix/HNSW options set;
- the same contract and options contract against a real Chroma server, started with
  `chroma run --port 58000` from the same package (not the Docker image): `http` mode, tenant and
  database created with `AdminClient`, error for a missing tenant;
- a collection written with the old metadata layout opens and queries;
- the 409 on changing `hnsw_*` through `/v1/providers`.

**Only read / mocked, not run**: `mode=cloud` against Chroma Cloud (no credentials; `CloudClient`
construction and the 300 batch are asserted with a mocked `chromadb.CloudClient`, and the live
behaviour of the 300-record write limit is taken from the plan); the `chromadb/chroma` Docker
image and its healthcheck; `x-chroma-token` against a real proxy. Chroma Cloud tests should be
marked `cloud` and run by the lead when a secret is available.

**Platform note.** `chromadb` installs and imports on Python 3.13 here; the project's
`.python-version` is 3.13 in this worktree. It does not install on 3.15 (no `onnxruntime`
wheel), as M9 item 2 already records.
