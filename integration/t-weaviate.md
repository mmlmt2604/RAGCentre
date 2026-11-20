# Fragment: t-weaviate (Phase V5, `weaviate`)

Files owned by this task: `src/ragcentre/stores/weaviate.py`, `tests/test_vs_weaviate.py`.
Everything below is for the lead to apply.

## pyproject extras

Tested with weaviate-client 4.24.1 (resolved by `uv add --optional weaviate weaviate-client` on 2026-11-20)
against a Weaviate 1.36.2 server. The adapter uses `Configure.Vectors.self_provided` / `vector_config=`
and `Reconfigure.Vectors.update`, which need a v4 client new enough to have them (4.24.1 is the only
version run).

```toml
[project.optional-dependencies]
weaviate = [
    "weaviate-client>=4.24.1",
]

[project.entry-points."ragcentre.vector_stores"]
weaviate = "ragcentre.stores.weaviate:STORE_TYPE"
```

## registry BUILTIN

```python
    "weaviate": "ragcentre.stores.weaviate:STORE_TYPE",
```

## docker-compose service (host ports 58080 / 50051)

The image has `wget` (used for the healthcheck); it has no `curl`. `ENABLE_MODULES` is empty so no
vectorizer module is loaded (the adapter never uses one). Run in this task as project `rc-t-weaviate`
on 58081 / 50052, because another session's `tests-weaviate-1` held 58080 / 50051; the block below is
that file with the plan's ports.

```yaml
  weaviate:
    image: cr.weaviate.io/semitechnologies/weaviate:1.36.2
    environment:
      AUTHENTICATION_ANONYMOUS_ACCESS_ENABLED: "true"
      DEFAULT_VECTORIZER_MODULE: none
      ENABLE_MODULES: ""
      CLUSTER_HOSTNAME: node1
      PERSISTENCE_DATA_PATH: /var/lib/weaviate
    ports:
      - "127.0.0.1:58080:8080"
      - "127.0.0.1:50051:50051"
    healthcheck:
      test: ["CMD-SHELL", "wget -q -O /dev/null http://localhost:8080/v1/.well-known/ready || exit 1"]
      interval: 3s
      timeout: 3s
      retries: 30
```

## ci stores matrix entry

The `stores` job is a single job, not a matrix. Add a service, a wait step and the test module:

```yaml
      weaviate:
        image: cr.weaviate.io/semitechnologies/weaviate:1.36.2
        env:
          AUTHENTICATION_ANONYMOUS_ACCESS_ENABLED: "true"
          DEFAULT_VECTORIZER_MODULE: none
          ENABLE_MODULES: ""
          CLUSTER_HOSTNAME: node1
        ports:
          - 58080:8080
          - 50051:50051
```

```yaml
      - name: Wait for Weaviate
        run: curl -sf --retry 30 --retry-connrefused --retry-delay 2 http://localhost:58080/v1/.well-known/ready
```

and append `tests/test_vs_weaviate.py` to the "Store tests" pytest list. The `ubuntu-latest` runner has
`curl`; the service container needs no healthcheck there because of the wait step. The test module
needs `uv sync --all-extras` (already used by that job). The main `test` job installs only the qdrant and
pgvector extras, so there `tests/test_vs_weaviate.py` skips at import (`importorskip("weaviate")`).

## README store-table row

For the "Concepts" sentence and "Optional vector stores" paragraph: add Weaviate to the list and
`ragcentre[weaviate]` to the extras. If a store table exists (`t-docs-ui`):

| `weaviate` | `ragcentre[weaviate]` (weaviate-client v4) | Weaviate server (HTTP + gRPC), `url` or `host`/`port`/`grpc_port`, `prefix`, secret `api_key`; cosine, l2, dot; exact filters; HNSW options `hnsw_max_connections`, `hnsw_ef_construction` (set once), `hnsw_ef` |

## DECISIONS.md

Suggested heading: `## Vector-store compatibility — V5 Weaviate (2026-11-20, branch v0.1.13)`

1. **Config**: `url` (http/https; Weaviate Cloud hosts use `connect_to_weaviate_cloud`) or
   `host`/`port` (default 8080)/`secure`; `grpc_port` (default 50051; Weaviate needs both HTTP and
   gRPC); `timeout`; `prefix`; secret `api_key` (`Auth.api_key`). Exactly one of `url`/`host`.
2. **Class names**: physical name = `capitalize(prefix + logical)` (`rc_x` -> `Rc_x`, prefix
   `team_` -> `Team_rc_x`), reversible (`logical_name`). The prefix must start with a lower-case
   letter (`[a-z][a-z0-9_]{0,40}` or empty), so the map is injective for one prefix; the result is
   validated against `^[A-Z][_0-9A-Za-z]{0,254}$`.
3. **Property names**: Weaviate reserves `id`, `_id`, `_additional`, `vector` (checked live), and
   compares property names case-insensitively (`f_Name` conflicts with `f_name`, checked live). Every
   schema field `x` is stored as `f_x`; fields with capitals as `fu_` + the field with `_` doubled
   and each capital `X` as `_x` (injective, lower-case). `document_id` is always present.
4. **Record ids**: Weaviate requires UUID object ids and canonicalises them to lower case. A
   canonical lower-case UUID id is the object id; any other id string gets `uuid5` over a fixed
   namespace and is kept in the `rc_id` property, so ids round-trip exactly (tested with upper-case,
   braced, non-UUID and 200-character ids).
5. **Strings are stored wrapped in double quotes** (`"en"`) and filtered the same way. Checked live:
   `field` tokenization is exact and case-sensitive, but Weaviate trims leading and trailing
   whitespace (including U+00A0, U+3000) of indexed and filter values, so `"en "` matched `"en"`,
   and the default `word` tokenization matched `"en-US"` for `"en"` and treated `""`, `"*"` and `"?"`
   as query errors. The wrapper makes equality exact for every string. Cost: the stored values look
   quoted in Weaviate's own console.
6. **Scores** (checked live, three metrics): cosine distance `1-cos` -> `1-d` (zero vector: distance
   1, score 0, like the reference); `dot` distance `-dot` -> `-d`; `l2-squared` -> `-sqrt(d)`. The
   default 1e-4 tolerance holds; no override.
7. **Catalog** in the class `description`: `ragcentre-catalog:` + the `InStoreCatalog` JSON
   (dim, metric, fields, options), written atomically at creation. Fields are added with
   `config.add_property` first and then `config.update(description=...)` (checked live; a property
   added later is null on old objects and not indexed for them, which is fine because a new schema
   field rewrites every record). Weaviate has no compare-and-set and `config.update` rewrites the
   class, so concurrent field additions from separate clients raced (checked live: update rejected
   with "use the add property feature", and lost descriptions). `_write_catalog` re-reads, merges the
   other worker's fields, writes and verifies, up to 8 rounds. A collection whose description lacks
   the prefix was not made by RAGCentre and is refused (`StoreError`).
8. **Filters**: `Filter.by_property` with `all_of`/`any_of` (the same operators as `&`/`|`, flat for
   deep trees). `not_equal` works and is true for objects missing the property, like `matches`.
   `$in` -> `contains_any` (checked live for text, int, number and bool); `$nin` -> `all_of` of
   `not_equal` (no negated list operator is used). Range operators need `index_range_filters` on
   int and number properties (server >= 1.26, per Weaviate's docs; 1.36.2 run). No post-filtering.
   Unit tests cover every operator and hostile strings; live tests cover the same strings end to
   end.
9. **Capabilities**: `metrics` cosine/l2/dot; `max_batch` 256; `max_dim` 65535 (checked live: 65535
   inserts and queries, 65536 is rejected by the server); `consistency="immediate"` and
   `filter_exact=True` as the contract run shows on a single node (selective-filter test passes).
   Not run: a multi-node cluster with replication or tenants. A single query returns at most 10000
   results (`QUERY_MAXIMUM_RESULTS`); larger `k` raises `StoreError`.
10. **Index options**: `hnsw_max_connections`, `hnsw_ef_construction` are immutable
    (`immutable_fields`) and recorded in the catalog; changing one raises `CollectionMismatch`.
    `hnsw_ef` is the query-time `ef` (a collection setting in Weaviate), re-applied by
    `ensure_collection` through `Reconfigure.Vectors.update`; it is not immutable.
11. **Vectors** use the new named-vector API (`vector_config=Configure.Vectors.self_provided`, one
    vector called `default`), not the deprecated `vectorizer_config`. `vectorizer: none`: no module
    ever embeds anything. Weaviate fixes a collection's dimension on the first object, so the adapter
    checks dimensions itself on upsert and query.
12. **Metadata not declared by the collection** is ignored on upsert (it cannot be filtered; the
    contract suite sends such fields). The collection's catalog is re-read once per batch when a
    field is unknown, in case another worker added it.
13. **Run versus read.** Run against Weaviate 1.36.2 (Docker `cr.weaviate.io/semitechnologies/weaviate`,
    anonymous access) with weaviate-client 4.24.1: the whole contract suite plus the options contract,
    the adapter's own live tests, `ragcentre store check`, and a second server with API-key
    authentication (right key connects; wrong or missing key gives a `StoreError`). Distances,
    reserved names, tokenization, trimming, `contains_any`, `not_equal`, batch overwrite semantics,
    description and property updates, `max_dim` and the 10000-result limit were all probed live.
    Only read, not run: Weaviate Cloud (`connect_to_weaviate_cloud`, `.weaviate.cloud` hosts),
    TLS/`secure`, OIDC or bearer-token auth, multi-tenancy, replication/cluster consistency levels, other
    server or client versions, and ACORN versus flat search internals (the exactness result is the
    contract suite's, not the docs').
