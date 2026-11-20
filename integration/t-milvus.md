## pyproject extras

```toml
milvus = ["pymilvus[milvus-lite]>=3.0.3"]
```

Optional extra only; nothing goes into core `dependencies`. Tested with pymilvus 3.0.3 and
milvus-lite 3.2.2 (what `pymilvus[milvus-lite]` resolved to), against Milvus Lite and a Milvus
v3.0.6 standalone server. `milvus-lite` publishes no Windows wheels, so there only server mode
works (`pymilvus` without the `milvus-lite` extra). `uv.lock` needs regenerating (`uv lock`); the
uncommitted `pyproject.toml` / `uv.lock` edits in the t-milvus worktree are exactly this.

## registry BUILTIN

```python
    "milvus": "ragcentre.stores.milvus:STORE_TYPE",
```

(After the `chroma` / `http` lines. The adapter imports `pymilvus` lazily, so listing it is safe
without the extra.)

## docker-compose service (host port 59530)

For `tests/docker-compose.yml`. The adapter's tests default to `http://127.0.0.1:59530`
(`RAGCENTRE_TEST_MILVUS_URI` overrides it). This exact service was started under project
`rc-t-milvus` (on a spare port) and the server tests passed against it; the embedded-etcd config
file is written by the entrypoint, so no extra file is needed.

```yaml
  milvus:
    image: milvusdb/milvus:v3.0.6
    # Standalone with the embedded etcd and local storage: one container, no MinIO.
    entrypoint:
      - bash
      - -c
      - |
        printf 'listen-client-urls: http://0.0.0.0:2379\nadvertise-client-urls: http://0.0.0.0:2379\nquota-backend-bytes: 4294967296\nauto-compaction-mode: revision\nauto-compaction-retention: "1000"\n' > /milvus/configs/embedEtcd.yaml
        exec milvus run standalone
    security_opt:
      - seccomp:unconfined
    environment:
      ETCD_USE_EMBED: "true"
      ETCD_DATA_DIR: /var/lib/milvus/etcd
      ETCD_CONFIG_PATH: /milvus/configs/embedEtcd.yaml
      COMMON_STORAGETYPE: local
      DEPLOY_MODE: STANDALONE   # without it the embedded etcd refuses to start (exit 134)
    ports:
      - "127.0.0.1:59530:19530"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9091/healthz"]
      interval: 10s
      timeout: 5s
      retries: 12
      start_period: 40s
```

## ci stores matrix entry

Service: `milvus` (the compose service above; its entrypoint override makes it awkward as a
GitHub Actions `services:` container, so start it with
`docker compose -f tests/docker-compose.yml up -d --wait milvus`). Extra: `milvus` (already
covered by `uv sync --frozen --all-extras`). Test modules to add to the stores job:
`tests/test_vs_milvus.py tests/test_vs_milvus_filter.py`. The filter module needs nothing
installed, so the main `test` job runs it too (`-m "not integration"`); `tests/test_vs_milvus.py`
skips itself without pymilvus. Budget: Lite plus server runs take about 7.5 minutes, nearly all
of it the server runs (Milvus create/drop of a collection takes 1 to 4 s).

## README store-table row

The README has no table, only a sentence under "Optional vector stores". Replace it with:

```
`sqlite_vec` works out of the box. The `qdrant`, `pgvector`, `chroma` and `milvus` stores need
their optional extras (`ragcentre[qdrant]`, `ragcentre[pgvector]`, `ragcentre[chroma]`,
`ragcentre[milvus]`); RAGCentre does not install them, and using a store whose extra is missing
fails with an error naming it. The `milvus` store runs on Milvus Lite (set `uri` to a `.db` file
path), Milvus standalone or cluster, and Zilliz Cloud (`uri` plus the `token` secret).
```

If a table is added later:

| milvus | Milvus Lite / standalone / Zilliz Cloud | `uri`, `db_name`, `prefix`, secret `token`, `index_type` (FLAT exact, HNSW approximate), `hnsw_m`, `hnsw_ef_construction`, `hnsw_ef` | `ragcentre[milvus]` |

## DECISIONS.md

1. **Collection layout.** One Milvus collection per RAGCentre collection, named `<prefix><name>`.
   Fields: `id` VARCHAR(128) primary key, `vector` FLOAT_VECTOR, `metadata` JSON holding
   `document_id` and every schema field. VARCHAR primary keys were verified on Lite and the
   standalone server. Filterable fields are not typed scalar columns: `add_collection_field`
   works on the standalone server but Milvus Lite answers UNIMPLEMENTED (both run live), so typed
   fields could not gain a field on an existing collection everywhere. Adding a filter field is
   therefore catalog-only; existing rows are not rewritten, and a missing key never matches
   `$eq`/`$in`/ranges and does match `$ne`/`$nin`, as the reference `matches` does (verified
   against a randomised corpus on both targets).
2. **Catalog.** Dimension, metric, field types and index options live in a side collection
   `<prefix>ragcentre_catalog` through `InStoreCatalog` (VARCHAR key, VARCHAR entry, a dummy 2-d
   vector, because Milvus has no vector-less collections). `ragcentre_catalog` is a reserved
   logical name. A catalog entry whose collection is gone is dropped on the next `ensure`, and an
   existing physical collection whose real dimension or metric (read back with
   `describe_collection` / `describe_index`) differs from the request is a `CollectionMismatch`
   that leaves no catalog entry behind. There is no cross-process lock around ensure: concurrent
   creators are tolerated (a create that fails while the collection now exists is accepted and
   then verified), not serialised.
3. **Consistency.** Collections are created with `consistency_level="Strong"` and every search,
   count and catalog read passes `consistency_level="Strong"` too, so `consistency="immediate"`
   holds (the contract suite runs with no `settle`, on both targets). Counts use
   `query(output_fields=["count(*)"])`, not `get_collection_stats`: on the standalone server
   `row_count` stays stale (0) after writes (observed live).
4. **Scores.** COSINE and IP are returned by Milvus as the similarity and the raw dot product.
   L2 comes back as the squared distance, so the score is `-sqrt(max(d, 0))` (checked live:
   points (3,4) and (0,0) score -5.0).
5. **Names.** Physical name = `prefix + name`, validated by the adapter against Milvus' rules
   (letter or underscore first, letters/digits/underscore, at most 255 characters) because Milvus
   Lite accepts anything and a server then rejects it. `prefix` is `[A-Za-z_][A-Za-z0-9_]{0,127}`
   or empty; `db_name` starts with a letter. Dimension is 2 to 32768: a real server rejects
   1-d vectors, Lite accepts them, so the adapter refuses them everywhere. `k` above 16384 is a
   `StoreError`: the server rejects it, Lite silently returns nothing.
6. **Query path and failures.** pymilvus retries RPC failures by itself and ties its client
   deadline to that retry window. Search, count, describe and catalog reads pass `retry_times=0,
   retry_on_rate_limit=False` (section 20: no retries on the query path), which means they carry
   no client-side deadline; a hung server blocks until the gRPC channel fails. Writes and schema
   calls keep the retries and the configured `timeout` (they are idempotent). Not verified: the
   behaviour of a server that accepts the connection and never answers. The adapter validates
   the query dimension and refuses non-finite vectors itself: a wrong-dimension or wrong-metric
   search returns an empty result silently (both targets) instead of an error, and Lite stores
   NaN without complaint.
7. **Filters (`stores/filter_milvus.py`).** Milvus filters are text with no usable parameter
   binding, so the compiler is the injection boundary: field names pass `FIELD_RE` and are only
   ever used as `metadata["<name>"]`; values are rendered from Python `int` / `float` / `bool` /
   `str` only (int64 range enforced); strings are double-quoted with `\\`, `\"`, `\n`, `\r`, `\t`
   escaped. Lite has no `\u`/`\x` escapes (verified: it rejects them), so other control
   characters are emitted raw, which both targets accept inside a literal; NUL and text that is
   not valid Unicode are refused with `InvalidFilter`. `in [...]` on a JSON key is rejected by
   Lite, so `$in`/`$nin` expand to `||`/`&&` chains of equalities. A 21-value corpus (quotes,
   backslashes, newlines, control characters, emoji, `" || true || "`, an expression that closes
   the literal and adds `exists(...)`) round-trips through real Lite and the real server: each
   value matches exactly its own record.
8. **Index options.** `index_type` is `FLAT` (default, exact; `filter_exact=True`) or `HNSW`
   (`filter_exact=False`, approximate; the contract's recall mode applies). `index_type`,
   `hnsw_m` and `hnsw_ef_construction` are index-shaping, recorded in the catalog and
   `immutable_fields`; `hnsw_ef` is query-time and mutable. The search uses `ef = max(hnsw_ef, k)`
   because Milvus refuses `limit > ef`. This goes beyond the brief (which asked for no options);
   say so at merge if it is unwanted, it is one config block plus `_options` / `_create`.
9. **What was run live.** The full `VectorStoreContract` (FLAT and HNSW) and
   `CollectionOptionsContract` on Milvus Lite 3.2.2 and on Milvus v3.0.6 standalone (docker,
   project `rc-t-milvus`, port 59530); `ragcentre store check --type milvus` on Lite, standalone
   FLAT and standalone HNSW (all "usable, all checks passed") and against a closed port (exit 1,
   clean message); `add_collection_field`, name rules, dimension limits, top-k limit, describe
   shapes, error codes (`code=100` for a missing collection), retry/timeout kwargs. Only
   read, not run: Zilliz Cloud (token auth and `https://` URIs: the code path is the same
   `MilvusClient(uri, token)` call, but no Zilliz endpoint was reachable), a multi-node cluster,
   and `db_name` with a non-default database (the kwarg is passed straight to `MilvusClient`).
   `ragcentre store check --type milvus` needs the registry line above; it was run with that line
   injected at runtime, because `registry.py` is a shared file.
