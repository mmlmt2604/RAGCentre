## pyproject extras

```toml
redis = [
    "redis>=8.1.0",
]
```

8.1.0 is the only redis-py version this task was built and tested against (Python 3.13, `uv add
--optional redis redis` resolved 8.1.0). The adapter uses only long-standing client API:
`Redis.from_url(..., protocol=3)`, `execute_command`, `pipeline`, `lock`, `scan_iter`. A lower bound
older than 8.1.0 was not tried. `uv.lock`: the lead runs `uv lock` (nothing committed here).

## registry BUILTIN

```python
    "redis": "ragcentre.stores.redis:STORE_TYPE",
```

(Add after `"http"`. Used locally to run `ragcentre store check --type redis`, then reverted.)

## docker-compose service (host port 56379; compose project `rc-t-redis`)

```yaml
  redis:
    image: redis:8   # resolved to Redis 8.10.3 with the search module (redisearch 81001) on 2026-11-20
    ports:
      - "127.0.0.1:56379:6379"
    healthcheck:
      # redis-cli is in the image and `curl` is not; verified: `docker exec ... redis-cli ping` -> PONG
      test: ["CMD", "redis-cli", "ping"]
      interval: 2s
      timeout: 2s
      retries: 30
```

Verified against the real image: `docker compose -p rc-t-redis up -d --wait` reported the container
`Healthy`, and `which curl` inside it prints nothing. `PING` only proves the server is up; the search
module is built into `redis:8` (MODULE LIST shows `search`, `vectorset`, `bf`, `timeseries`, `ReJSON`),
so no extra readiness check is needed for this image. (`redis/redis-stack` would also work; not run.)

## ci stores matrix entry

Add a service to the `stores` job and the module to its pytest list:

```yaml
      redis:
        image: redis:8
        ports:
          - 56379:6379
        options: >-
          --health-cmd "redis-cli ping"
          --health-interval 2s --health-timeout 2s --health-retries 30
```

Test module `tests/test_vs_redis.py` (extra `redis`, already covered by `uv sync --frozen
--all-extras` once the lead has added the extra and re-locked). The tests default to
`redis://127.0.0.1:56379/0`; override with `RAGCENTRE_TEST_REDIS_URL`. If the job becomes a matrix, the
entry is: service `redis` (`redis:8`, port 56379), extra `redis`, module `tests/test_vs_redis.py`.

## README store-table row

| `redis` | `ragcentre[redis]` | cosine, l2, dot | filter-exact; immediate; no `max_dim`; `max_batch` 256 | `{url: redis://localhost:6379/0, algorithm: flat}` + optional secret `password` |

Also add Redis to the list of optional stores in the "Provider" bullet and the "Optional vector
stores" paragraph (`ragcentre[redis]`). Needs Redis 8 or Redis Stack (the search module).

## DECISIONS.md

## Vector-store compatibility — V6 Redis (2026-11-20, branch v0.1.13)

1. **Store `redis`** (`stores/redis.py`, extra `redis`). Config `url` (`redis://`, `rediss://`,
   `unix://`; may carry credentials), `algorithm: flat | hnsw` (default `flat`, **immutable**),
   `ef_runtime` (hnsw only, mutable, default 100 when unset because the server's 10 gives poor
   recall), `prefix` (`[A-Za-z0-9_.:-]*`). Secret `password`, which replaces any password in the url.
   `immutable_fields = ("algorithm",)`. The URL is never put in an error message.
2. **Layout.** One search index per collection, named `<prefix><collection>`, over HASH keys
   `<prefix><collection>:<id>`. Vector field `__rc_vector` (FLOAT32 blob), `document_id` always an
   indexed TAG, schema fields TAG (str, bool as `true`/`false`; `CASESENSITIVE`) or NUMERIC (int, float).
   Field names starting `__rc_` are rejected.
3. **Catalog** (decision for plan 4.3): a *string* key `ragcentre:collection:<prefix><collection>`
   holding the `InStoreCatalog` JSON (dim, metric, fields, options `{algorithm, tag_encoding: 1}`), not
   a hash. Reason: an index cannot distinguish int/float or str/bool, so field types need a home; the
   catalog helper already stores one JSON string per collection. `collection_info` reports the
   dimension and metric of the **index** (`FT.INFO`), so a lost or stale catalog cannot misreport them.
   `ensure_collection` runs under a Redis lock (`ragcentre:lock:<name>`, 120 s expiry), so separate
   processes get one creator. Recovery: a catalog entry without its index is purged (entry and records
   under its prefix) and rebuilt; an index without a catalog entry is adopted when dim, metric and
   algorithm match. Adding a field is `FT.ALTER ... SCHEMA ADD`, which re-indexes existing hashes in the
   background (`indexing:1`, verified); `ensure_collection` waits up to 30 s for it and then proceeds.
4. **String values are encoded, not merely escaped.** Verified on the server, a native TAG is trimmed of
   leading/trailing whitespace, split on the separator (`,`), truncated at NUL, case-folded unless
   `CASESENSITIVE`, rejected when empty (without `INDEXEMPTY`) and no longer matched beyond ~2-4 KB.
   Escaping cannot fix these. So a string is stored as `s` + UTF-8 bytes with everything except ASCII
   letters and digits written `_xx` (hex); values whose encoding exceeds 512 characters are stored as
   `h` + SHA-256 hex. Equality is exact and a tag contains only `[A-Za-z0-9_]`, so no value can add
   query syntax. Cost: values are unreadable in `redis-cli`; `tag_encoding` is in the catalog options.
5. **Query**: `(<filter>)=>[KNN k @__rc_vector $B [EF_RUNTIME $EF] AS __rc_score]`, `RETURN 1`,
   `LIMIT 0 k` (the default LIMIT is 10), `DIALECT 2`. The filter is the KNN pre-filter (no
   post-filtering). Hybrid policy left on the server default. `ef_runtime` must be a `$param`; a literal
   is a syntax error (verified).
6. **Scores, verified on Redis 8.10.3 for FLAT and HNSW**: COSINE returns `1 - cos`; **IP returns
   `1 - dot`** (not `-dot`); **L2 returns the squared distance**. Scores: cosine `1 - s`, dot `1 - s`,
   l2 `-sqrt(s)`. All three metrics, `dot` included, work for non-unit vectors.
7. **Timeouts are not short results.** The server default is `search-timeout` 500 ms with policy
   `return`: a timed-out query returns fewer (here zero) results with `warning: ["Timeout limit was
   reached"]`. The adapter connects with RESP3 (the warning is not visible in RESP2) and raises a
   `StoreError` on any search warning. A `protocol` in the url is rejected, because URL options override
   keyword arguments in redis-py. Open: a `timeout` config field (or telling users to raise
   `search-timeout`) for very large collections.
8. **Capabilities**: `metrics` all three; `max_batch` 256; `consistency="immediate"` (a HASH write is
   indexed before the command returns: 1500 writes with interleaved queries missed none on FLAT and
   HNSW, and the contract's concurrent test passes; the server's `WORKERS` default is 10 and it did not
   matter); `filter_exact=True` (see 9); `max_dim=None` (the server accepted FLAT and HNSW indexes up to
   1,000,000 dimensions at creation; a 3072-dim collection stores and queries, the 65,536-dim one was only
   created and queried for its shape, nothing larger was stored).
9. **`filter_exact` evidence**: contract suite exact on 200 records for FLAT and HNSW; additionally, ad
   hoc on 20,000 records (dim 32, cosine) with 0.5%-selective and 12%-selective filters and `$ne`: FLAT
   recall 1.0 and HNSW 0.99 to 1.0, no short result in 20 queries each. HNSW is approximate even without
   a filter; `filter_exact` means k filtered matches come back when they exist.
10. **Numbers**: NUMERIC is a double, so integers beyond 2**53 lose precision in filters. Floats use
    `repr(float)`, so equality on floats is exact for the stored text.
11. **Not supported / not run**: Redis Cluster, ACL users restricted below `FT.*`, Redis Stack
    (`redis/redis-stack`) and Redis < 8, redis-py other than 8.1.0, TLS (`rediss://`; the code path is
    the client's own and was not run), and `FT.ALTER` on collections of millions of records.
12. **Verified (run)**: contract suite on the real `redis:8` image (8.10.3) for FLAT, HNSW with
    `ef_runtime=200` and HNSW with the default ef; `CollectionOptionsContract` (flat vs hnsw); live tests for
    the layout, scale per metric on a 3-4-5 triangle, 26 hostile strings (and 600/5000 characters)
    through `$eq/$ne/$in/$nin`, numeric edges, long values, 3072 and 65,536 dims, `FT.ALTER`
    re-indexing, half-created and lost-catalog recovery, concurrent ensures from separate clients and threads,
    prefixes; `ragcentre store check --type redis` for flat and hnsw; a second container with
    `--requirepass` for the secret, url credentials and the secret overriding the url (no password leaks
    into output). **Not verified**: how the server picks `ADHOC_BF` vs `BATCHES` under the default hybrid policy
    (left to the server; only its results were checked, see 9), and any documented tag-length limit (the
    2000/4096 boundary was observed, not read).
