# t-mongodb fragment (store `mongodb`, branch v0.1.13-t-mongodb)

Files owned: `src/ragcentre/stores/mongodb.py`, `tests/test_vs_mongodb.py`. Everything below is for the lead to apply.

## pyproject extras

```toml
mongodb = [
    "pymongo>=4.20.1",
]
```

Tested with pymongo 4.20.1 only (the lower bound is the tested version, as for the other extras). The
installed source says `SearchIndexModel(type=...)` arrived in 4.7, so 4.7 is the API floor; it was not run.
`uv lock` after adding it.

## registry BUILTIN

```python
    "mongodb": "ragcentre.stores.mongodb:STORE_TYPE",
```

(`src/ragcentre/stores/registry.py`, next to `"redis"`.) Without it `ragcentre store check --type mongodb`
answers "unknown store type"; the check below was run with this line added locally and reverted.

## docker-compose service (tests/docker-compose.yml; host port 57017)

```yaml
  # MongoDB Atlas Local: mongod 9.0.8 + mongot 1.79.1 (the search process), single-node replica set, no auth.
  # The image has its own healthcheck (`runner healthcheck`); this one polls it faster. The image has no curl.
  # Run the tests with
  #   RAGCENTRE_TEST_MONGODB_URI='mongodb://127.0.0.1:57017/?directConnection=true'
  mongodb:
    image: mongodb/mongodb-atlas-local:latest
    ports:
      - "127.0.0.1:57017:27017"
    healthcheck:
      test: ["CMD", "/usr/local/bin/runner", "healthcheck"]
      interval: 5s
      timeout: 10s
      start_period: 20s
      retries: 30
```

Verified: `docker compose -p rc-t-mongodb up -d --wait` reports Healthy after about 13 s. The tested image was
`mongodb/mongodb-atlas-local:latest`, digest `sha256:747a6c6b37b9358b45d9ecc63e4b8b6d59f8d11896d1c75f967f2520e12f4947`
(built 2026-11-09; runner v1.2.5). Consider pinning a version tag (`9.0.8` if it exists) when applying.

## ci stores matrix entry (`stores` job in .github/workflows/ci.yml)

Service (the job uses `services:`):

```yaml
      mongodb:
        image: mongodb/mongodb-atlas-local:latest
        ports:
          - 57017:27017
        options: >-
          --health-cmd "/usr/local/bin/runner healthcheck"
          --health-interval 5s --health-timeout 10s --health-start-period 20s --health-retries 30
```

Job env: `RAGCENTRE_TEST_MONGODB_URI: mongodb://127.0.0.1:57017/?directConnection=true`.
Extra: `mongodb` (in `--all-extras`). Test module to add to the `pytest` list: `tests/test_vs_mongodb.py`.
The module takes about 5 minutes against the local image (three live contract classes; searchable
visibility is polled), so it is the slowest store module. Without the service or the extra every live test
skips (the 107 non-live tests still run).

## README store-table row

| `mongodb` | MongoDB Atlas Vector Search (`$vectorSearch`), also Atlas Local and Community with `mongot` | `ragcentre[mongodb]` | secret `uri`; config `database`, `exact`, `num_candidates_factor`, `prefix`; cosine and l2; eventually consistent |

## DECISIONS.md

```
## Vector-store compatibility — V6 MongoDB (2026-11-20, branch v0.1.13)

1. **Store `mongodb`** (`stores/mongodb.py`, extra `mongodb`). Config `database` (default `ragcentre`;
   letters, digits, `_`, `-`, at most 38), `exact` (default false), `num_candidates_factor` (default 20,
   1 to 1000) and `prefix` (letters, digits, `_`, `-`, at most 32). Secret `uri` (a `mongodb://` or
   `mongodb+srv://` string). No option shapes the index (`exact` and the factor are query time), so
   `immutable_fields = ()`. The connection string is never put in an error message or the catalog; errors
   from the client are scrubbed of it.
2. **Layout.** One collection per RAGCentre collection, `<prefix><collection>`; documents
   `{_id, embedding: [double], document_id, <declared schema fields>}`. Fields that the collection does not
   declare are not stored (the contract's `test_adding_a_field` writes records with more fields than
   the collection declares). One `vectorSearch` index `rc_vector` per collection: `numDimensions`,
   `similarity` (`cosine`, or `euclidean` for l2) and a `filter` entry for `document_id` and each field.
   The vector is a plain BSON array; `binData` float32 vectors would save space; they were not tried.
3. **Catalog** (plan 4.3): a side collection `_ragcentre_collections` in the same database, documents
   `{_id: <physical name>, entry: <InStoreCatalog JSON>}` with options `{layout: 1, vector_field}`. Reason:
   a search index cannot tell int from float or str from bool. `collection_info` reports the dimension and
   metric of the **index**. `ensure_collection` reconciles the catalog, then the index, every time: a
   missing index is created, a missing filter path is added with `updateSearchIndex`, a dimension or
   similarity that disagrees raises `CollectionMismatch`, and an index whose catalog entry was lost is
   adopted when it matches. So a crash after the catalog write is repaired by the next call. Lease
   documents `lock:<physical name>` in the same collection (15-minute lease, taken with an upsert on an
   expired `expires`) serialise `ensure_collection` and `drop_collection` across threads and processes.
   `drop_collection` drops the collection first and the catalog entry second.
4. **Waiting for the index.** `ensure_collection` polls `listSearchIndexes(rc_vector)` until `status` is
   `READY` and every host's `mainIndex.definitionVersion` equals `latestDefinitionVersion` (up to 10 minutes).
   `queryable` alone is not enough: **after `updateSearchIndex` the old index stays queryable** while the
   new one builds (verified: `BUILDING`, `queryable: true`, version 0 serving, latest version 1), and a
   filter on the new field then fails with "Path ... needs to be indexed as filter". An index in status
   `DELETING` is retried (a dropped collection's index). A status `FAILED` raises.
5. **Scores, verified numerically on MongoDB 9.0.8 / mongot 1.79.1**: `cosine` returns `(1 + cos) / 2`
   (adapter returns `2s - 1`); `euclidean` returns **`1 / (1 + d²)`** with the *squared* distance (adapter
   returns `-sqrt(1/s - 1)`); `dotProduct` returns `(1 + dot) / 2` **clamped at 0** (a dot of -1 and of -3
   both scored 0.0), so a dot below -1 cannot be recovered and **`dot` is not offered** (metrics cosine, l2).
   Precision: scores are float32 near 1.0, so the Euclidean distance inverted from a near-identical vector
   carries an error up to about 2.5e-4 (ranking unaffected); the contract passes at the default `TOL`.
6. **Query.** `$vectorSearch` with `filter` (a pre-filter), `limit=k`, ANN `numCandidates = min(10000,
   k x factor)`, or `exact: true` with no `numCandidates` (the server rejects both together). `k` above 10000
   (the `numCandidates` cap, "must be within bounds [1..10000]", and `limit` must be at most `numCandidates`)
   is run exact. The compiler maps `$eq $ne $gt $gte $lt $lte $in $nin $and $or` one to one; `$not`, `$nor`,
   `$exists` also work on the server but are not needed. `$regex` and `$expr` are rejected by the server.
   A field that is not declared `filter` fails on the server ("needs to be indexed as filter"), so the
   compiler checks the catalog first. **Missing fields** (verified live and by test): `$ne` and `$nin` match
   a document without the field, every other operator does not, exactly like `models.filters.matches`.
7. **Server limits, verified**: `numDimensions` within [1..8192] (8193 is refused) so `max_dim = 8192`; a
   zero vector in a cosine collection is **silently left out of the index** and a zero *query* vector is a
   server error ("Cosine similarity cannot be calculated against a zero vector"), so the store rejects
   both; documents with a wrong-length or non-numeric vector are silently skipped, so lengths and NaN
   are checked before the write; **string values of 8191 characters or more are not matched reliably**
   (8000 characters and 4000 two-byte characters were; 8192 and up never matched themselves, and a
   query for an 8191-character value matched several longer documents), so strings over 8000 characters
   are rejected on upsert rather than stored unfilterable. Integers are doubles in the index: beyond
   2**53 filters lose precision. A missing collection or index makes `$vectorSearch` return **no rows,
   not an error**, so the adapter checks the catalog and raises `CollectionNotFound`.
8. **Capabilities**: metrics cosine and l2; `max_batch` 500 (one unordered `bulk_write` of `ReplaceOne`
   upserts, with one retry when two writers race to insert the same new `_id`); `max_dim` 8192;
   `consistency="eventual"`; `filter_exact = exact`.
9. **`filter_exact` evidence.** Declared `exact` (plan), not claimed for ANN. Measured on the local image:
   with 20,000 records (dim 32, cosine) and filters selecting 0.2% (40 records) and 4.8% (960), approximate
   search returned min(k, matches) results with recall 1.0 against exact search for k 10 and 50 and a factor
   of 1, 10 and 20 (15 queries each). Unfiltered recall at k=10 was 0.887, 0.993 and 1.0 for a factor of 1,
   10 and 20. mongot looks like it falls back to an exact scan under selective filters, but that is the local
   `mongot` Community edition; Atlas was not available, so ANN filtering stays declared inexact.
10. **Eventual consistency.** A write is visible to `$vectorSearch` after a short delay (about 0.1 to 0.3 s
    typically, and in a few cases more than 3 s in `store check` runs before this was handled). The index's
    own `numDocs` (in `listSearchIndexes`) can run **ahead of the searcher**: a check that polled it still
    got empty results. So `collection_info(...).count` is `min(numDocs, documents an exact search returns)`
    for collections of up to 2000 documents (`numDocs` alone above that), and polling it, as `store check`
    does, waits for visibility. Cost: one exact search over at most 2000 vectors per `collection_info`, which
    the retrieval guard calls before every query. If that matters on small collections, drop `_visible`.
    The contract suite's `settle` waits for the same signal plus a canary query for the last written record.
11. **Not supported / not run**: Atlas (a cloud cluster; the `uri` secret was never given, so no cloud run,
    and the cloud status fields, `numDocs` and the 8000-character limit are only assumed to match the local
    image), TLS or auth (the local image has none; `mongodb+srv://` and credentials go through pymongo
    unchanged), sharded clusters, Community without `mongot`, `quantization` and `hnswOptions`, binData
    vectors, a failover during `ensure_collection`, pymongo other than 4.20.1.
12. **Verified (run)** on the real `mongodb/mongodb-atlas-local` image (mongod 9.0.8, mongot 1.79.1,
    pymongo 4.20.1): the contract suite for `exact=false`, `exact=true` and `exact=false` with a prefix and a
    factor of 50; live tests for the layout, score formulas, missing-field semantics against `matches`,
    adding a field to a populated index, half-created and lost-catalog recovery, concurrent `ensure` from
    five clients, drop and recreate with another shape at once, 8192 dimensions and the 8193 refusal,
    long, multibyte and hostile strings, numeric edges, the `k > 10000` exact path and filtered ANN on 4000
    records; and `ragcentre store check --type mongodb` five times (3 approximate, 2 exact) all passing.
    Only **read** (docs and client source): the `$vectorSearch` filter operator list beyond the ones tried,
    Atlas limits, pymongo's API floor.
```
