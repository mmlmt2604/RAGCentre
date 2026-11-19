## pyproject extras

No change — the `qdrant` extra already exists at `qdrant-client>=1.20.0`, and 1.20.0 is the
version this task was implemented and tested against.

## registry BUILTIN

No change — `"qdrant": "ragcentre.stores.qdrant:STORE_TYPE"` already exists.

## docker-compose service

None. Qdrant is tested in-process with the client's embedded `:memory:` mode (no Docker needed).

## ci stores matrix entry

None. The Qdrant tests run in the default `uv run pytest` (in-process), like `sqlite_vec`.

## README store-table row

None yet — the store table is added by `t-docs-ui` (V7).

## DECISIONS.md

M9, after item 4 (Qdrant):

11. **Qdrant deployment options.** `QdrantConfig` gains `prefer_grpc` (default false),
    `grpc_port` (default 6334) and `timeout` (seconds, optional) which are passed through to
    `QdrantClient` for server deployments only; the embedded local mode ignores them. `prefix`
    (default empty) is prepended to the physical collection name, so a shared cluster or a
    Qdrant Cloud role restricted to `ragcentre_*` works. The physical name is re-validated
    against Qdrant's naming rule (`^[A-Za-z0-9_][A-Za-z0-9_-]{0,254}$`, i.e. 1-255 URL-safe
    characters, not starting with `-`); an invalid result raises `StoreError`. (§10.5, plan V4.2)

12. **Qdrant index options are immutable and recorded.** `hnsw_m`, `hnsw_ef_construction` and
    `quantization` (`none` | `scalar` | `binary`) shape the index and are listed in
    `VectorStoreType.immutable_fields`. They are stored in the collection `metadata` under
    `ragcentre.options`; `ensure_collection` raises `CollectionMismatch` when the recorded
    options differ from the store's configuration. Collections built before this phase have no
    `options` key and read as the defaults (`quantization="none"`, no HNSW build parameters), so
    they still open and query with a default store. `hnsw_ef` is a query-time parameter, sent as
    `search_params` on each `query_points` call, and is mutable. (§10.5, plan V4.2)

13. **Qdrant needs >= 1.16.** The adapter keeps its catalog in collection `metadata`, which was
    added in Qdrant 1.16.0. `healthcheck` reads the server version from `QdrantClient.info()` and
    fails with a message naming the version when it is below 1.16. The embedded local mode
    reports the client's compatibility version (1.20.0), so it passes.

14. **Qdrant `max_dim` stays `None`.** The installed `qdrant-client` 1.20.0 exposes no vector
    dimension limit, so `Capabilities.max_dim` is left unset.

**Verified against the installed `qdrant-client` 1.20.0** (not from memory): the `QdrantClient`
constructor keyword names (`location`, `url`, `port`, `grpc_port`, `prefer_grpc`, `api_key`,
`prefix` — note `prefix` here is a REST URL path prefix, unrelated to our collection-name
`prefix`, so we never pass ours to it — `timeout`, `path`); the `create_collection` /
`update_collection` keyword `metadata`, `hnsw_config` (`HnswConfigDiff(m, ef_construct)`) and
`quantization_config` (`ScalarQuantization`/`BinaryQuantization`); `query_points` keyword
`search_params` (`SearchParams(hnsw_ef)`); `info()` returning `VersionInfo.version`; and that
`CollectionConfig` reads back `metadata`, `hnsw_config.m`/`ef_construct` and `params.vectors`.

**Observed in local mode** (read, not run against a live server): the embedded `:memory:` and
path modes round-trip collection `metadata` and `hnsw_config` but **do not apply or round-trip
`quantization_config`** (it reads back as `None`); quantization therefore works only on a real
server, which is why the options are recorded in `metadata` regardless. The local mode also
ignores `search_params` (exact search) and payload indexes; both are harmless there.
