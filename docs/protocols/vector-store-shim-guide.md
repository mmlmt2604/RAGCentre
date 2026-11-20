# Write your own vector-store shim

A **shim** is a small web service that puts any vector database behind
[`ragcentre.vector_store/v1`](vector-store-v1.md). RAGCentre creates collections and sends it
records and filtered top-k queries; the shim translates those to the database's own calls. This
guide shows the shortest way to write one, how to translate RAGCentre's filter language, and how to
report scores. The reference shim, [`examples/vector_store_shims/memory/`](../../examples/vector_store_shims/memory/),
implements everything here over an in-memory brute-force index and runs through the same contract
suite as the built-in adapters.

## 1. What the service must do

Eight operations over a named collection of `(id, vector, metadata)` records. Each maps to one
endpoint; the protocol document has the exact request and response bodies:

| Endpoint | Your database call |
|---|---|
| `GET /info` | nothing — report your name, version and capabilities |
| `GET /health` | a cheap liveness check (`SELECT 1`, `client.heartbeat`, …) |
| `PUT /collections/{name}` | create a collection/table/index with `dim` and `metric`, or verify an existing one matches |
| `GET /collections/{name}` | return the stored shape and record count |
| `DELETE /collections/{name}` | drop it (idempotent) |
| `POST /collections/{name}/upsert` | insert-or-replace records |
| `POST /collections/{name}/delete` | delete records by id (idempotent) |
| `POST /collections/{name}/query` | filtered top-k nearest neighbours |

You keep the collection's dimension, metric and field types yourself — in the database's own
metadata slot where it has one, or in a small side table. RAGCentre does not tell you where; it
only re-sends the shape on every `PUT`, which is how you detect a mismatch.

The quickest correct implementation is brute force: store records and, on query, score every record
that matches the filter and keep the top `k`. That is exactly what the memory shim does, and it
passes the contract suite. Start there, then swap the scan for the database's vector index.

## 2. Scores

Return the mathematical score, not a database-specific relevance score. Higher is better.

| Metric | Return | If the database gives you … convert it |
|---|---|---|
| `cosine` | cosine similarity | cosine distance `d = 1 − cos` → `1 − d`; similarity `s` → `s` |
| `dot` | the raw dot product | inner product `s` → `s`; negative inner product `d` → `−d`; `1 − dot` → `1 − d` |
| `l2` | negative Euclidean distance −√Σ(xᵢ−yᵢ)² | distance `d` → `−d`; **squared** distance `d` → `−√d` |

The last row is the one that bites: Pinecone, Milvus, Weaviate, LanceDB, Elasticsearch and
OpenSearch all return a squared or transformed Euclidean value in one mode or another. Check the
vendor's docs and, better, run a two-record round trip and confirm you get the same ordering and
scores as a brute-force calculation. For a zero vector, cosine similarity is `0`. Do not advertise
`dot` if your database needs unit-length vectors and cannot return the raw dot product for
arbitrary inputs.

The memory shim's reference is `ragcentre.vectors`: `cosine_similarity`, `dot` and `l2_distance`
(with `l2` negated).

## 3. The filter language

RAGCentre sends filters in its own JSON language, the same one used for queries and evaluation:

```json
{"$and": [{"lang": {"$in": ["en", "fr"]}}, {"year": {"$gte": 2020}}]}
```

```
filter   := { field: scalar }                       # shorthand for $eq
          | { field: { op: value } }
          | { "$and": [filter, …] }
          | { "$or":  [filter, …] }
          | { field1: …, field2: … }                # several keys: implicit $and
op       := $eq | $ne | $gt | $gte | $lt | $lte | $in | $nin
scalar   := str | int | float | bool
```

Ranges apply to `int` and `float` fields only. Every field is `document_id` or one you declared in
`fields`; every record carries every field, with defaults filled in, so you never have to decide
what `$ne` means for a missing field.

### Translating it

You do not have to parse the JSON yourself if your language has the matching operations. Two
approaches, both valid:

1. **Evaluate it.** If your database has no filter pushdown (or you are brute-forcing), parse the
   JSON into a tree and evaluate each record. In Python, `ragcentre.models.filters` already does
   both sides: `parse_filter(raw, schema)` validates and normalises into `Cond`/`And`/`Or` nodes,
   and `matches(filter, metadata)` is the reference evaluator. The memory shim uses exactly these.
2. **Compile it.** Translate the tree to the database's query language and push it into the search.
   Walk the nodes:

   ```python
   from ragcentre.models.filters import And, Cond, Or, parse_filter

   def compile(f):
       if isinstance(f, And):
           return {"must": [compile(i) for i in f.items]}      # e.g. Qdrant/Elasticsearch
       if isinstance(f, Or):
           return {"should": [compile(i) for i in f.items]}
       assert isinstance(f, Cond)
       values = list(f.value) if isinstance(f.value, tuple) else [f.value]
       return {"field": f.field, "op": f.op, "values": values}
   ```

   Then map each operator to your database (`$eq` → equality, `$in` → membership, `$gt`/`$gte`/
   `$lt`/`$lte` → range, `$ne`/`$nin` → negation, `$and` → conjunction, `$or` → disjunction).
   Whatever syntax you emit, follow your database's quoting rules for values and identifiers — a
   field name or string that reaches the database as code is an injection, the same as SQL. The
   built-in SQL compilers in `ragcentre.stores.filter_sql` are a worked example of the discipline:
   values are bound parameters, field names pass a regular expression and are quoted.

**Filter before search, never after.** Apply the filter inside the nearest-neighbour search, or
score only the records that match (as the memory shim does). Fetching an unfiltered top-`k` and then
discarding non-matches can return fewer than `k` — or none — when matches exist, which silently
corrupts retrieval. If your database genuinely cannot guarantee filtered top-`k` even in its best
mode, advertise `filter_exact: false` and oversample to approximate, but never return a record that
fails the filter.

## 4. Capabilities

`/info.capabilities` tells RAGCentre what your service can do. `metrics` (a non-empty list of
`"cosine"`, `"l2"` and/or `"dot"`) and `max_batch` (largest upsert batch) are required; the rest
default sensibly:

```json
{
  "metrics": ["cosine", "l2", "dot"],
  "max_batch": 1000,
  "max_dim": null,
  "consistency": "immediate",
  "filter_exact": true,
  "max_collections": null
}
```

- `max_dim`: set it to the database's real dimension ceiling, so RAGCentre fails an oversized
  collection at create time instead of mid-ingestion.
- `consistency`: `"eventual"` when a successful write may not be visible to a query yet (Pinecone,
  Elasticsearch before a refresh). RAGCentre surfaces it; the contract suite's `settle` hook is how
  its tests wait.
- `filter_exact`: `false` only when the database cannot guarantee filtered top-`k` (see above).
- `max_collections`: informational.

Only advertise a metric or guarantee you have tested against a running database.

## 5. Errors

Answer every non-2xx with `{"error": {"code", "message"}}`. The codes and the status each maps to
are in the protocol document. In short: use `4xx` for "this call is wrong and retrying will not
help" (`invalid_request`, `collection_not_found`, `collection_mismatch`, `unauthorized`) and
`503 unavailable` for "try again later". RAGCentre retries writes, never reads, so a failing
`/query` should be a `5xx` or `503`, not a silently short result.

## 6. Check it

The contract suite is the acceptance test. The memory shim runs through it in
`tests/test_v1_http_store.py`, which starts the shim in-process and points `ragcentre.stores.http`
at it — protocol and client together:

```sh
uv run pytest -q tests/test_v1_http_store.py
```

Once the `ragcentre store check` command is available, it runs a live check against a running shim
after you have registered it as a provider. Register it, point it at `http://127.0.0.1:8702`, and run the
check; then run a query or two by hand with curl to see the raw responses.

## Checklist

- [ ] `/info` answers without credentials with the right `protocol`, a name, a version, and honest capabilities
- [ ] `PUT /collections/{name}` is idempotent and answers `409 collection_mismatch` on a different `dim`/`metric`
- [ ] Queries filter **before** search and return at most `k` matches, sorted by descending score
- [ ] Scores follow the table in §2 (check squared vs unsquared Euclidean)
- [ ] Records are inserted-or-replaced by id; delete and drop are idempotent
- [ ] Errors use the `{error: {code, message}}` envelope
- [ ] The contract suite passes against your service
