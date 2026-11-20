# Fragment: t-search (Elasticsearch and OpenSearch stores)

Branch `v0.1.13-t-search`. Owned files: `src/ragcentre/stores/{_lucene_scores,elasticsearch,opensearch}.py`,
`tests/test_vs_elasticsearch.py`, `tests/test_vs_opensearch.py`. `_lucene_scores.py` holds more than its name
says: the score inverses, the DSL filter compiler **and** `SearchEngineStore`, the adapter logic both stores
share (the two modules are thin: client calls, vector mapping, k-NN query body).

## pyproject extras

```toml
elasticsearch = [
    "elasticsearch>=8.19.3",
]
opensearch = [
    "opensearch-py>=2.8.0",
]
```

Tested (full test module each, all passing): `elasticsearch` 9.5.1 client against a 9.5.1 server;
`elasticsearch` 8.19.3 client against an 8.19.9 server; `opensearch-py` 3.2.0 and 2.8.0 against OpenSearch
3.10.0. The 9.x client sends `compatible-with=9` and an 8.x server refuses it (run: 400
`media_type_header_exception`), so a user with an 8.x server installs the 8.x client; the calls the adapter
makes are identical in both, and the extra's range allows either. `uv lock` will resolve 9.5.1 / 3.2.0.

## registry BUILTIN

```python
    "elasticsearch": "ragcentre.stores.elasticsearch:STORE_TYPE",
    "opensearch": "ragcentre.stores.opensearch:STORE_TYPE",
```

(Both modules import their client lazily through `require_extra`, so listing them is always safe.)

Also extend `tests/test_m9_optional_extras.py`: add `"elasticsearch", "opensearchpy"` to `BLOCKED` and the two
types to the `(name, config, secrets)` loop with `("elasticsearch", {"url": "http://x:9200"}, {})` and
`("opensearch", {"url": "http://x:9200"}, {})`; both raise `StoreUnavailable` naming their extra.

## docker-compose service (tests/docker-compose.yml, ports 59200 / 59201)

Healthchecks verified on the real images: both use bash `/dev/tcp` (no curl assumed) and both reached
`healthy`. Heap is limited to 512 MB each (each container then used about 1.2 GB resident).

```yaml
  elasticsearch:
    image: docker.elastic.co/elasticsearch/elasticsearch:9.5.1
    environment:
      discovery.type: single-node
      xpack.security.enabled: "false"
      ES_JAVA_OPTS: -Xms512m -Xmx512m
    ports:
      - "127.0.0.1:59200:9200"
    healthcheck:
      test: ["CMD-SHELL", "bash -c 'exec 3<>/dev/tcp/localhost/9200 && printf \"GET /_cluster/health?wait_for_status=yellow&timeout=1s HTTP/1.0\\r\\n\\r\\n\" >&3 && head -1 <&3 | grep -q \" 200 \"'"]
      interval: 5s
      timeout: 5s
      retries: 30

  opensearch:
    image: opensearchproject/opensearch:3.10.0
    environment:
      discovery.type: single-node
      DISABLE_SECURITY_PLUGIN: "true"
      DISABLE_INSTALL_DEMO_CONFIG: "true"
      OPENSEARCH_JAVA_OPTS: -Xms512m -Xmx512m
    ports:
      - "127.0.0.1:59201:9200"
    healthcheck:
      test: ["CMD-SHELL", "bash -c 'exec 3<>/dev/tcp/localhost/9200 && printf \"GET /_cluster/health?wait_for_status=yellow&timeout=1s HTTP/1.0\\r\\n\\r\\n\" >&3 && head -1 <&3 | grep -q \" 200 \"'"]
      interval: 5s
      timeout: 5s
      retries: 30
```

The images are about 2 GB each; the two JVMs need roughly 2 GB of RAM together.

## ci stores matrix entry

Add to the `stores` job `services:` (GitHub service containers cannot run a `bash` healthcheck with this quoting
reliably, so use the options form and keep the "wait" step like Chroma's):

```yaml
      elasticsearch:
        image: docker.elastic.co/elasticsearch/elasticsearch:9.5.1
        env:
          discovery.type: single-node
          xpack.security.enabled: "false"
          ES_JAVA_OPTS: -Xms512m -Xmx512m
        ports:
          - 59200:9200
      opensearch:
        image: opensearchproject/opensearch:3.10.0
        env:
          discovery.type: single-node
          DISABLE_SECURITY_PLUGIN: "true"
          DISABLE_INSTALL_DEMO_CONFIG: "true"
          OPENSEARCH_JAVA_OPTS: -Xms512m -Xmx512m
        ports:
          - 59201:9200
```

Steps: after "Wait for Chroma", add

```yaml
      - name: Wait for Elasticsearch and OpenSearch
        run: |
          curl -sf --retry 60 --retry-connrefused --retry-delay 2 "http://localhost:59200/_cluster/health?wait_for_status=yellow&timeout=1s"
          curl -sf --retry 60 --retry-connrefused --retry-delay 2 "http://localhost:59201/_cluster/health?wait_for_status=yellow&timeout=1s"
```

and add `tests/test_vs_elasticsearch.py tests/test_vs_opensearch.py` to the "Store tests" pytest list.
Measured: `test_vs_elasticsearch.py` about 2 minutes (186 tests) and `test_vs_opensearch.py` about 1.5
minutes (88 tests), because every write waits for an index refresh. About 110 of those tests need no server
(filter compiler, scores, config, catalog against an in-memory engine, request shapes) and run in the default
job whenever the extras are installed; the live ones skip cleanly when nothing answers on 59200 / 59201.

## README store-table row

```
| `elasticsearch` | Elasticsearch 8.11+ / Elastic Cloud | `ragcentre[elasticsearch]` | `dense_vector` HNSW (or quantised/flat), filter inside `knn`, 4096 dims |
| `opensearch` | OpenSearch 2.19+ | `ragcentre[opensearch]` | k-NN HNSW on `faiss` (default) or `lucene`, filter inside the query, 16000 dims |
```

Prose: "Optional vector stores: Qdrant, PostgreSQL with pgvector, Chroma, LanceDB, Elasticsearch and OpenSearch";
`ragcentre[elasticsearch]` and `ragcentre[opensearch]` in the optional-stores paragraph.

## DECISIONS.md

```
## Vector-store compatibility — V6 Elasticsearch and OpenSearch (2026-11-20, branch v0.1.13)

1. **One shared implementation.** `stores/_lucene_scores.py` has the score inverses, the DSL filter
   compiler and `SearchEngineStore` (catalog, naming, bulk writes, query post-processing). The two
   modules supply only client calls, the vector mapping and the k-NN query body. Module names
   `elasticsearch.py`/`opensearch.py` do not shadow the clients (absolute imports; a test asserts the
   resolved client is not under `ragcentre`).
2. **Catalog** in the index mapping `_meta.ragcentre` (an `InStoreCatalog`): `{dim, metric, fields,
   options}`. The index is created with `dynamic: strict`, the vector field `_rc_vector`, `document_id` and
   the schema fields, and its `_meta` in one call, so a collection never exists without a catalog. A
   concurrent create is reconciled against the winner (`CollectionMismatch` if it differs). Adding a field is
   `put_mapping` (properties and `_meta` together). An index without `_meta.ragcentre` is refused, never
   adopted. Fields starting `_rc_` are reserved. `_meta` is free-form at mapping level on both engines
   (verified).
3. **Immutable options**: Elasticsearch `index_options` (`hnsw` default, `int8_hnsw`, `int4_hnsw`,
   `bbq_hnsw`, `flat`, `int8_flat`, `int4_flat`, `bbq_flat`), OpenSearch `engine` (`faiss` default, `lucene`;
   `nmslib` is rejected by the config model and the store). Recorded in the catalog; a difference raises
   `CollectionMismatch` (options contract tests for both). Default `hnsw` because 9.x's own default is
   `int8_hnsw` (verified: `get_mapping` after creating a field without `index_options`). `bbq_*` needs 64+
   dims (the server says so; the adapter raises first). Quantised types add `rescore_vector` (oversample 3),
   which made scores exact (max error 2e-7 vs 1.6e-2 without; verified), so the contract suite passes on
   `int8_hnsw`.
4. **Filter placement**: the filter goes inside `knn` (ES: `knn.filter`; OpenSearch:
   `query.knn.<field>.filter`), never as `post_filter` or a sibling `bool` (a test asserts the request
   shape). `filter_exact=True` on both: at 20,000 documents and filters matching 0.5%, 5%, 94% and 100% of
   them, every query returned k hits when k existed (ES, faiss, lucene), recall 0.99 to 1.0 (run).
5. **Filter compiler**: `bool.filter` / `bool.should` (`minimum_should_match: 1`) / `term` / `terms` /
   `range` / `bool.must_not`. `$ne` and `$nin` are `must_not`, which keeps documents without the field, as
   `matches()` does; tested against an interpreter of the DSL on 400 random filters with random absent
   fields and against both live servers. Field names are validated (`check_field`, `fullmatch`) and checked
   against the collection's field list; values travel as JSON, so nothing is escaped by hand. Mappings:
   keyword / long / double / boolean (`double`, not `float`, so `0.3` matches `0.3`).
6. **Scores (run on both live servers, all metric/engine pairs)**: cosine `(1+cos)/2`, l2 `1/(1+d^2)`,
   dot `dot+1` (dot >= 0) or `1/(1-dot)` (dot < 0); max error vs the formula 6e-7. Same inverses for faiss
   and lucene. Elasticsearch `dot` uses `max_inner_product` (`dot_product` refuses non-unit vectors: run,
   400). No `score_tolerance` override was needed; the contract passes at `TOL=1e-4`.
7. **Candidates**: `num_candidates_factor` (default 10): ES `num_candidates = k * factor`, clamped to
   [k, 10000]; OpenSearch faiss gets `method_parameters.ef_search = k * factor` (without it, `k=300` on a
   300-document collection returned 254 hits and `k=1000` on 1000 returned 690: run). Lucene needs no
   parameter (it returned all documents) and none is sent. `k` is capped at 10,000 (result window).
8. **Writes**: bulk with `refresh=wait_for` for upsert and delete, so `consistency` is `immediate` (a test
   reads the count and a query right after each write without refreshing). Cost: each batch waits for the
   next refresh (about 1 s), so ingestion is about 500 records/s per worker; `max_batch=500`. A
   `refresh=false` + `settle`-style option is not offered because the interface has no settle step.
9. **Zero vectors with cosine are refused** by both servers (run: Elasticsearch
   `document_parsing_exception`, OpenSearch `Cannot normalize a zero-length vector`), also as a query.
   The adapter rejects such a record before sending the batch, so a bulk is never half-written.
10. **Limits**: ES `max_dim=4096` for every `index_options` type (run: 4097 and 8192 rejected). OpenSearch
    `max_dim=16000` for both engines (run: 16001 rejected). Per-instance capabilities are the class
    constants; the two stores do not discover limits from the server. Version gates (read from release
    notes, **not run on old servers**): ES >= 8.11 (`max_inner_product`, `index_options`), per type up to
    8.16 for bbq, `rescore_vector` only on >= 8.18; OpenSearch faiss >= 2.19, lucene >= 2.13. A too-old
    server gets a clear `StoreError` at `ensure_collection`.
11. **Config**: ES `url` xor `cloud_id`, `username`, `verify_certs`, `ca_certs`, `request_timeout`,
    `index_options` (I), `num_candidates_factor`, `prefix`; secrets `api_key` or `password` (both is an
    error). OpenSearch `url`, `username`, `verify_certs`, `ca_certs`, `request_timeout`, `engine` (I),
    `num_candidates_factor`, `prefix`; secret `password`. `prefix` must be a valid lowercase index prefix.
    Not offered: AWS SigV4 for OpenSearch Service, client certificates, multiple hosts.
12. **Verified (run)**: contract suite on ES (`hnsw`, `flat`, `int8_hnsw`), OpenSearch (`faiss`, `lucene`),
    options contract on both, `store check` on both (and ES with every `index_options`, bbq at 128 dims),
    20,000-document filter scale test, request shapes, concurrent ensure from 6 clients. **Only read**:
    Elastic Cloud (`cloud_id`), TLS and auth paths, OpenSearch before 3.x, Elasticsearch before 9.x except
    where DECISIONS item 13 says otherwise.
13. **Client/server versions (run)**: ES 9.5.1 client + 9.5.1 server and ES 8.19.3 client + 8.19.9 server
    both pass the whole test module (contract x3 index types incl. rescore, options contract, live tests,
    `store check`); the 9.5.1 client against the 8.19.9 server is refused by the server (compat header),
    so the extra covers both majors and the user picks the client matching the server. opensearch-py 2.8.0
    and 3.2.0 both pass against OpenSearch 3.10.0. Not run: OpenSearch 2.x servers, Elasticsearch 8.11 to
    8.18 (the version gates are from release notes).
14. **Open**: ingestion speed is bound by `refresh=wait_for` (about 1 s per 500-record batch, measured
    20,000 documents in about 40 s on each engine); an optional `refresh: false` mode with an explicit
    settle step would need an interface hook. Elasticsearch `dense_vector` over about 1,000 dimensions
    with `max_batch=500` makes bulk bodies of 15 to 25 MB; the default `http.max_content_length` is 100 MB.
```
