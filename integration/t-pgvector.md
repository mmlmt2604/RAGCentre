# t-pgvector fragment (plan Phase V4 item 1)

## pyproject extras
No change needed. The existing extra stays as it is, and both bounds were tested at the lower bound:
pgvector = ["pgvector>=0.5.1", "psycopg[binary]>=3.3.9"]
(tested with pgvector 0.5.1 and psycopg 3.3.9; the adapter imports only `psycopg`, never the `pgvector` package)

## registry BUILTIN
"pgvector": "ragcentre.stores.pgvector:STORE_TYPE"   # already present; no change

## docker-compose service (host port 54329)
Already present and unchanged (pgvector/pgvector:pg17, 127.0.0.1:54329:5432). Tested against server
extension 0.8.7. No new service block.

## ci stores matrix entry
Existing `stores` job already runs tests/test_m9_pgvector.py against the pgvector service on 54329. No change.

## README store-table row
| `pgvector` | `ragcentre[pgvector]` | cosine, l2, dot | filter-exact, immediate, max dim 16000 (HNSW to 2000 `vector` / 4000 `halfvec`) | `{"host": "db", "dbname": "rc", "user": "rc", "vector_type": "auto"}` + secret `password` (or `dsn`) |

## DECISIONS.md
Vector-store compatibility, V4.1 pgvector (t-pgvector):
1. **`vector_type: auto | vector | halfvec`** (immutable). `auto` makes the column `halfvec` above 2000 dims
   and `vector` otherwise; an explicit value is used as given. HNSW is built if dim <= 2000 (`vector`)
   or <= 4000 (`halfvec`); otherwise only the GIN index is created and queries are exact scans, with a
   warning. The effective type (not the config value) is what gets recorded.
2. **`hnsw_m`, `hnsw_ef_construction`** (immutable, optional; None = pgvector defaults 16/64). Recorded as
   the effective values; `ensure_collection` raises `CollectionMismatch` on any difference in
   vector_type, m or ef_construction, as well as dim/metric/fields.
3. **`ef_search`, `max_scan_tuples`** (mutable, query time, `SET LOCAL`). Default ef_search is unchanged
   (`max(40, min(k, 1000))` with iterative scan, 1000 without). `max_scan_tuples` needs pgvector >= 0.8
   and is ignored with a warning on older servers.
4. **Connection fields** `host`, `port`, `dbname`, `user`, `sslmode` plus a `password` secret build the
   libpq string with `psycopg.conninfo.make_conninfo`. The `dsn` secret still wins when both are given.
5. **`_collections` layout**: added nullable columns `vector_type`, `hnsw_m`, `hnsw_ef_construction`, added
   with `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` in setup. A NULL `vector_type` reads as `vector` and NULL
   hnsw values read as 16/64, so collections created before this change keep opening. Tested by nulling the
   columns of a live row.
6. **Capabilities.max_dim = 16000**, the type limit for both `vector` and `halfvec`.
7. Score transforms (cosine 1-d, l2 -d, dot -value) and metadata layout unchanged.
8. **Live (run against pgvector/pgvector:pg17, extension 0.8.7, psycopg 3.3.9)**: HNSW limits (vector 2000
   ok / 2001 error, halfvec 4000 ok / 4001 error), type limit 16000 / 16001 error for both, halfvec operators
   and index scan, `m` range 2..100, `ef_construction` range 4..1000, GUCs `hnsw.ef_search` (1..1000),
   `hnsw.iterative_scan` (off|relaxed_order|strict_order), `hnsw.max_scan_tuples` (default 20000), the
   migration, and the full contract suite for both `vector` and `halfvec` columns. The halfvec contract uses
   `score_tolerance = 2e-2` (float16 storage). **Only read, not run**: pgvector < 0.8 servers (no iterative
   scan path), non-`disable` sslmode connections.
