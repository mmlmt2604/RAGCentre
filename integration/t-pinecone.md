## pyproject extras

```toml
pinecone = ["pinecone>=10.3.0,<11"]
```

Optional extra only. Tested with `pinecone` 10.3.0 (the 2026-07 API; 10.x moved index descriptions to a
`schema`, which the adapter reads, so an older 9.x client is not supported and `<11` keeps a future major
from surprising it). Add the entry point next to the other stores:

```toml
[project.entry-points."ragcentre.vector_stores"]
pinecone = "ragcentre.stores.pinecone:STORE_TYPE"
```

Also add the `cloud` marker so the opt-in hosted tests do not warn:
`markers = ["integration: needs external services (Docker)", "cloud: needs a hosted service and credentials (opt-in)"]`.
`uv.lock` needs `uv lock`. The package name is `pinecone` (not `pinecone-client`).

## registry BUILTIN

```python
    "pinecone": "ragcentre.stores.pinecone:STORE_TYPE",
```

(After the `weaviate` line. The adapter imports `pinecone` lazily, so listing it is safe without the extra.)

## docker-compose service (host ports 55080-55100)

Optional: Pinecone Local, the emulator. Each index it creates listens on its own port counted up from `PORT`,
and the index description tells the client `localhost:<port>`, so **the host ports must equal the container
ports** and one block exposes 21 of them (the controller plus 20 indexes; the contract tests keep at most 3
indexes alive at once). Another emulator (`tests-pinecone-1`, from another session) already uses
5080-5100, so this block uses 55080-55100. Started under project `rc-t-pinecone` and the whole live suite
passed against it (`RAGCENTRE_TEST_PINECONE_URL=http://localhost:55080`).

```yaml
  pinecone:
    image: ghcr.io/pinecone-io/pinecone-local:latest
    environment:
      PORT: "55080"
      PINECONE_HOST: localhost
    ports:
      - "127.0.0.1:55080-55100:55080-55100"
```

**No `healthcheck:`.** The image has no shell and no wget/curl (`docker exec ... sh` fails: executable file
not found), so a container healthcheck cannot work. Wait from outside instead:
`curl -sf --retry 30 --retry-connrefused --retry-delay 2 http://localhost:55080/indexes` (answers
`{"indexes":[]}` a moment after start; checked). The image is `linux/amd64` only (it ran under emulation on an
arm64 host). Image checked: `ghcr.io/pinecone-io/pinecone-local@sha256:0f944585830a30c37260ac6534c7c206280490947e1ddbb716474166b517b6c7`,
created 2025-02-27, which is old: see the findings below.

## ci stores matrix entry

Add `tests/test_vs_pinecone.py` to the `stores` job's pytest list. Without a server it still runs the unit tests
and the **fake-client contract suite** (about 90 tests, no extra needed); the emulator tests (`integration`)
skip when nothing answers at `RAGCENTRE_TEST_PINECONE_URL` (default `http://localhost:5080`). To run them in CI
add a `pinecone` service (the block above; GitHub `services:` can map the 21 ports) and a wait step
(`curl` loop above), and set `RAGCENTRE_TEST_PINECONE_URL: http://localhost:55080`. Extra: `pinecone` (covered by
`uv sync --frozen --all-extras`).

Hosted Pinecone (`cloud`, opt-in, **not run by this task**): set the secret `RAGCENTRE_TEST_PINECONE_API_KEY`
(and optionally `RAGCENTRE_TEST_PINECONE_CLOUD` / `RAGCENTRE_TEST_PINECONE_REGION`, default aws / us-east-1) in the
job's environment from a repository secret; the class `TestPineconeCloudContract` creates `rct-*` serverless
indexes (billed, a few seconds to a minute each) and deletes them; it skips when the variable is absent. Keep
it out of pull-request runs from forks.

## README store-table row

The README has no table, only a sentence under "Optional vector stores". Add `pinecone` to that sentence
("... `chroma` ... and `pinecone` stores need their optional extras (`ragcentre[pinecone]`) ...") and add a
line: "The `pinecone` store uses an index you created (RAGCentre never creates or deletes Pinecone indexes) and
one namespace per collection; it is eventually consistent." If a table is added later:

| pinecone | Pinecone (hosted) and Pinecone Local | `index_name` (required, you create it), `prefix` (namespace prefix), `controller_host` (Pinecone Local), `index_host`, `timeout`, secret `api_key` | `ragcentre[pinecone]` |

## DECISIONS.md

## Vector-store compatibility: V5 Pinecone (2026-11-20, branch v0.1.13)

1. **Layout.** One RAGCentre collection is one Pinecone namespace (`prefix + collection name`) inside one
   user-created index (`index_name`). RAGCentre never creates, configures or deletes an index: the test setup
   does that (emulator: `POST /indexes`; hosted: `pc.indexes.create`). Records are `(id, vector, metadata)` with
   the core's flat metadata; nothing else is stored. Only plain dense vector indexes are accepted (the 2026-07
   description must show exactly a dense `_values` field, optionally `_sparse_values`); a schema/document index
   is refused with a message saying so, because it is read and written through another API.
2. **ensure_collection** describes the index every time and raises `CollectionMismatch` naming the index when
   the dimension or metric differs, then records the collection in the catalog. It writes nothing to Pinecone.
   Because an index has one dimension and one metric, a collection cannot be re-created with another shape
   after `drop_collection` (it is a `CollectionMismatch`); the contract test that expects this is overridden in
   the test module to say so. Instance `capabilities.metrics` is therefore only the index's metric (the type's
   best case, shown in the UI, still lists all three), so `ragcentre store check` exercises the one metric the
   index has and needs an index of dimension 8 (its fixed test dimension).
3. **Catalog.** `LocalCatalog` (`<data dir>/store_catalogs/pinecone.db`), instance key
   `<index_host or controller_host or "pinecone">/<index_name>`, options `{}` (there are no index-shaping
   options, so `immutable_fields` is empty). The key contains no secret. The hosted key does not identify the
   Pinecone project, so two projects with an index of the same name under one data dir would share field
   types; use different `prefix`es or data dirs. Losing the file orphans the collections (rebuildable).
4. **drop_collection** is `delete(delete_all=True)` on the namespace (an unknown namespace, which the hosted
   service answers with 404, is accepted) plus forgetting the catalog entry; the namespace itself stays until
   Pinecone expires empty ones. `delete_namespace` / `describe_namespace` were not used because Pinecone Local
   answers 404 for both (run), so `collection_info().count` and the contract `settle` use
   `describe_index_stats` per-namespace `vectorCount`.
5. **Scores.** `cosine` and `dotproduct` come back as the similarity and the dot product; `euclidean` comes back
   as the **squared** distance (ascending): score = `-sqrt(max(d, 0))`. Run on the emulator with
   `q=[1,0,0]` against `[1,2,3]`, `[3,2,1]`, `[0,0,5]` (returned 13, 9, 26 = squared). The hosted service was not
   run; the docs say the same.
6. **Filters** (`compile_filter`) are the identity except lists of booleans: Pinecone Local answers 400 "the $in
   operator must be followed by a list of strings or a list of numbers" for `[true]`, so `$in` becomes an `$or` of
   `$eq` and `$nin` an `$and` of `$ne`. Run on the emulator: `$ne` and `$nin` match records that lack the field
   (as `models.filters.matches` does); `$eq` of a boolean matches only that boolean; the empty string is
   a value; ranges take numbers only; `$eq` 2020.0 matches the integer 2020. A randomised test (6,000 filter/metadata
   pairs, including missing fields) checks the compiled filter, evaluated with those emulator semantics, against
   `matches`. **Not shown for the hosted service**: that `$ne` / `$nin` match a missing field (Pinecone's docs do not
   say); if it turns out they do not, wrap them as `$or` with `{"$exists": false}`. The window where it matters is
   records written before a schema field existed, until the re-index rewrites them.
7. **Limits** are validated before anything is sent, with `StoreError`: ids 1-512 ASCII characters, metadata
   40 KB per record counted in UTF-8 bytes, no nulls or nesting, integers beyond 2**53 (numbers are stored as
   doubles), non-finite numbers, key names starting with `$`; query `k` above 10,000. `max_batch` is 1,000; the
   adapter also splits a batch under 1.5 MB of encoded JSON (the 2 MB request limit binds long before 1,000 vectors
   for wide vectors). `max_dim` 20,000 (read: `pinecone/schema_builder.py` `_DIMENSION_MAX`). The 1,000-result limit
   when metadata is returned is irrelevant: the adapter never asks for metadata or values. The emulator enforces
   none of the size limits (a 41 KB metadata value and a 1,001-vector upsert were accepted), so those numbers come
   from the documentation and the client source, not from a run.
8. **All-zero vectors.** The emulator accepts them, and under `cosine` the query then returns a `null` score that
   makes the client fail to parse the whole result (the namespace is unusable for queries). The adapter refuses an
   all-zero vector under `cosine` at upsert and query (`StoreError`). The hosted service is believed to reject
   all-zero dense vectors for every metric (unverified); the adapter leaves that to the server.
9. **Consistency and exactness.** `consistency="eventual"` (documented). The emulator is immediate (queries saw
   writes at once), so the eventual path is exercised only by the fake client, which delays visibility; the contract
   `settle` waits until the namespace count equals the expected count *and* a `fetch` of the newest writes shows
   their metadata (an overwrite does not change the count). `filter_exact=False`, conservatively: Pinecone does
   not document that a filtered top-k returns k matches when k exist, and the emulator (which answered every
   filtered contract query, including the selective one, exactly) is no evidence about the hosted ANN index. The
   live emulator test class checks exactness anyway; the fake and (opt-in) hosted classes use the recall check.
10. **Client and errors.** `pinecone` 10.3.0 retries transient errors itself (RetryConfig; read, not tuned); the
    adapter adds no retry of its own. SDK errors become `StoreError` with the api key replaced by `***`;
    401/403 add "check the api_key secret". `Pinecone(...)` receives the key explicitly (the environment variable
    `PINECONE_API_KEY` is never read), and with `controller_host` and no secret a placeholder key is used (the emulator
    accepts any).
11. **Emulator quirk worked around.** The 10.x client cannot parse the emulator's older index description (it
    has `dimension`/`metric` and no `schema`: `ResponseParsingError`). With `controller_host` set, that one case
    falls back to `GET <controller>/indexes/<name>` through `httpx`. The hosted path (`Pinecone.indexes.describe`)
    reads the documented 2026-07 schema; it was exercised with the client's own `IndexModel` decoded from a JSON
    fixture, not against the hosted service.
12. **Run versus read.** Run: the whole contract suite (plus the adapter's tests, 32 emulator tests) against
    Pinecone Local (`ghcr.io/pinecone-io/pinecone-local`, image sha256:0f94458...b7c7, created 2025-02-27) with
    `pinecone` 10.3.0, on `tests-pinecone-1` (5080) and on our own container (55080); `ragcentre store check --type
    pinecone` (passed on a cosine index of dimension 8; a dimension-4 index and a missing index give the
    documented errors); the fake-client contract suite; probes of scores, filter operators, bool lists, zero vectors,
    deletes in an unknown namespace, namespace stats and the missing `describe_namespace` / `delete_namespace`.
    **Only read, not run (no API key was available):** the hosted service in every respect (serverless and pod
    indexes, `indexes.describe` on the real API, `indexes.create` in the cloud test setup, eventual consistency
    and its timing, 404 on delete in an unknown namespace, the 2 MB / 1,000-vector / 40 KB / 10,000 / 1,000 limits,
    `$ne` on a missing field, filtered top-k completeness, namespace quotas, private endpoints / `index_host`,
    gRPC). `TestPineconeCloudContract` is written and skipped without `RAGCENTRE_TEST_PINECONE_API_KEY`; the lead
    can run it once a key is available through a secret request (it creates and deletes billed `rct-*` indexes).
    The capability flags `filter_exact=False` and `consistency="eventual"` stay at their conservative values until then.
