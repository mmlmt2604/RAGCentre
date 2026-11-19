# Vector-store protocol — `ragcentre.vector_store/v1`

A vector-store service stores named collections of `(id, vector, metadata)` records and answers
filtered top-k nearest-neighbour queries. Write one to plug any vector database into RAGCentre
without shipping a Python package into its process. `examples/vector_store_shims/memory/` is a
complete, runnable reference over an in-memory brute-force index, and
[`vector-store-shim-guide.md`](vector-store-shim-guide.md) walks through writing one.

RAGCentre's provider type for this protocol is `vector_store/http`.

The protocol mirrors RAGCentre's Python `VectorStore` interface one to one: eight operations over a
named collection. The service is the source of truth for a collection's shape, its filter language
is RAGCentre's own JSON filter language, and its scores follow the normalisation below. A shim
translates that language and those scores to whatever its database needs; the core never learns a
vendor dialect.

## Endpoints

All request and response bodies are JSON. Collection names appear in the URL path and follow the
naming rule below. `{name}` is a collection name, URL-encoded as usual.

| Method & path | Purpose | Success |
|---|---|---|
| `GET /info` | Protocol, service identity and capabilities | `200` |
| `GET /health` | Liveness check | `200` |
| `PUT /collections/{name}` | Create a collection, or check an existing one matches | `200`, or `409 collection_mismatch` |
| `GET /collections/{name}` | The collection's shape and record count | `200`, or `404 collection_not_found` |
| `DELETE /collections/{name}` | Drop the collection (idempotent) | `204` |
| `POST /collections/{name}/upsert` | Insert or replace records | `204` |
| `POST /collections/{name}/delete` | Delete records by id (idempotent) | `204` |
| `POST /collections/{name}/query` | Filtered top-k search | `200 {matches}` |

### `GET /info`

```json
{
  "protocol": "ragcentre.vector_store/v1",
  "service_name": "memory-shim",
  "service_version": "0.1.0",
  "capabilities": {
    "metrics": ["cosine", "l2", "dot"],
    "max_batch": 1000,
    "max_dim": null,
    "consistency": "immediate",
    "filter_exact": true,
    "max_collections": null
  }
}
```

- `protocol` must be exactly `ragcentre.vector_store/v1`.
- `service_name` and `service_version` are required, non-empty strings. RAGCentre reads them **when
  the provider is created** and keeps them with the provider. Change `service_version` whenever a
  change could alter what a query returns (a new index build, a new distance implementation, a new
  library version). A different version reported later does not silently change an existing index:
  RAGCentre pins the provider, and an HTTP store's identity is its provider, not the service.
- `capabilities` is required. `metrics` is a non-empty list of `"cosine"`, `"l2"` and/or `"dot"`;
  `max_batch` is the largest upsert batch (records per request) the service accepts and is a
  positive integer. The remaining fields are optional:
  - `max_dim` (`int | null`, default `null`): the largest vector dimension a collection may have.
  - `consistency` (`"immediate" | "eventual"`, default `"immediate"`): `"eventual"` means a
    successful write may not be visible to queries immediately. RAGCentre surfaces this in the UI.
  - `filter_exact` (`bool`, default `true`): `false` means the service cannot guarantee that `k`
    matches come back whenever `k` exist (see *Filtering*). RAGCentre shows a warning and relaxes
    its acceptance test accordingly.
  - `max_collections` (`int | null`, default `null`): informational collection-count limit.

RAGCentre reads `/info` **without credentials** when the provider is created, exactly as it does
for parsers, chunkers, embedders and rerankers. It reads only `protocol`, `service_name`,
`service_version` and `capabilities`.

### `GET /health`

Returns `200` with `{"status": "ok"}`. It checks that the service is up and can reach its backing
database. When it cannot, return `503 unavailable` (below). It is a read, so RAGCentre never
retries it.

### `PUT /collections/{name}`

Request:

```json
{"dim": 8, "metric": "cosine", "fields": {"lang": "str", "year": "int", "score": "float", "public": "bool"}}
```

- `dim` is a positive integer; `metric` is one of the metrics advertised in `/info`; `fields` maps
  each filterable metadata field to a type `"str"`, `"int"`, `"float"` or `"bool"`. `document_id`
  is always a filterable `str` field and must not appear in `fields`.
- If the collection does not exist, create it with exactly this shape and answer `200`.
- If it exists with the same `dim` and `metric`, the call is a success. New fields are **additive**:
  merge them in and answer `200`. A field already present with the same type is unchanged; a field
  present with a different type is a `409 collection_mismatch` (RAGCentre's schema is additive
  only, so this means two different corpora are sharing a name).
- If it exists with a different `dim` or `metric`, answer `409 collection_mismatch`. Never alter an
  existing collection's dimension or metric.

### `GET /collections/{name}`

```json
{"dim": 8, "metric": "cosine", "count": 200}
```

`count` is the number of records currently in the collection. A missing collection is
`404 collection_not_found`.

### `DELETE /collections/{name}`

Drops the collection and its metadata. **Idempotent**: deleting a missing collection is also `204`.

### `POST /collections/{name}/upsert`

```json
{"records": [{"id": "…", "vector": [0.1, -0.2, …], "metadata": {"lang": "en", "year": 2024}}]}
```

- At most `max_batch` records per request; RAGCentre splits larger writes itself.
- A record with the same `id` replaces the old record entirely, metadata included.
- `id` is a string (RAGCentre sends UUIDs); `vector` is a list of numbers with the collection's
  `dim`; `metadata` is a flat object whose values are strings, integers, floats or booleans.
- Upserting into a missing collection is `404 collection_not_found`. A record whose vector length
  differs from the collection is `400 invalid_request`.

### `POST /collections/{name}/delete`

```json
{"ids": ["…", "…"]}
```

Deletes those ids. **Idempotent**: missing ids are ignored, and deleting from a missing collection
is also `204`.

### `POST /collections/{name}/query`

```json
{"vector": [0.1, -0.2, …], "k": 10, "filter": {"lang": {"$in": ["en", "fr"]}}}
```

Response:

```json
{"matches": [{"id": "…", "score": 0.87}, {"id": "…", "score": 0.82}]}
```

- `vector` is the query vector; `k` is a positive integer; `filter` is a filter in RAGCentre's JSON
  language (below), or `null`/absent for no filter.
- `matches` is sorted by descending `score`, has at most `k` entries, and contains only ids and
  scores. The core keeps chunk text itself.
- The query vector must have the collection's `dim`; otherwise `400 invalid_request`. Querying a
  missing collection is `404 collection_not_found`.

## Collection naming

The core generates names like `rc_01234567_0123456789abcdef`: lowercase letters, digits and
underscores, matching `^[a-z][a-z0-9_]{2,62}$`. A service may assume that shape but must still
validate the path parameter and answer `400 invalid_request` for anything else. If the service maps
a logical name to a physical name (case changes, a prefix, a namespace), the mapping must be
deterministic, applied on every operation, and never visible to the core.

## Records and metadata

A record's `metadata` holds exactly `document_id` (a string) plus every field in the corpus's
metadata schema, with defaults already filled in. Nothing else: chunk text and free-form attributes
stay in RAGCentre's SQLite database, which keeps payloads small. `document_id` is always filterable
as a `str` and need not be listed in `/info` or `fields`.

## Scores

Higher is always better. Return the mathematical score, not a database-specific relevance score.

| Metric | Score the service must return |
|---|---|
| `cosine` | cosine similarity |
| `dot` | the unnormalised dot product |
| `l2` | negative Euclidean distance (−√Σ(xᵢ−yᵢ)²) |

If the database reports a distance or a transformed value, convert before returning it — for
example cosine distance `d = 1 − cos` becomes `1 − d`, and a **squared** Euclidean distance `d`
becomes `−√d`. For a zero vector, cosine similarity is `0`. Do not advertise `dot` if the database
cannot return the raw dot product for arbitrary (non-unit) inputs. Scores are comparable only
within one collection; RAGCentre uses ranks for its default fusion.

## Filtering

`filter` is RAGCentre's own JSON filter language:

```
filter   := { field: scalar }                       # shorthand for $eq
          | { field: { op: value } }
          | { "$and": [filter, …] }
          | { "$or":  [filter, …] }
          | { field1: …, field2: … }                # several keys: implicit $and
op       := $eq | $ne | $gt | $gte | $lt | $lte | $in | $nin
scalar   := str | int | float | bool
```

Ranges (`$gt`, `$gte`, `$lt`, `$lte`) apply to `int` and `float` fields only; `$in` and `$nin` take
a non-empty list. RAGCentre validates filters before it sends them, so a service can assume they
are well-formed and that every field is `document_id` or a declared field — but it should still
answer `400 invalid_request` rather than crash on anything it does not understand.

The requirement is normative: **a query must apply the filter before or during the nearest-neighbour
search, never as a post-filter on an unfiltered top-k.** If a filter matches at least `k` records,
the service must return `k` matches. Discarding non-matches after ranking can return fewer than `k`
(or even none) when matches exist, which silently corrupts retrieval. If the database genuinely
cannot guarantee this even in its best filtering mode, advertise `filter_exact: false` and
approximate as closely as you can (exact mode, oversampling) — but never return a match that fails
the filter, and never move filtering into the core.

## Errors

Every non-2xx response carries:

```json
{"error": {"code": "…", "message": "…"}}
```

| HTTP status | `code` | Meaning | RAGCentre treatment |
|---|---|---|---|
| `400` | `invalid_request` | Malformed JSON, wrong vector dimension, unknown metric, bad filter, bad collection name. | Permanent: the operation fails and is not retried. |
| `401` | `unauthorized` | Missing or wrong bearer token on a protected endpoint. | Permanent. |
| `404` | `collection_not_found` | The collection does not exist. | Permanent (see below). |
| `409` | `collection_mismatch` | The collection exists with a different dimension, metric, or a conflicting field type. | Permanent. |
| `503` | `unavailable` | The service or its backing database is starting up or overloaded. May include `Retry-After`. | Retried for writes only. |

`message` is a short human-readable sentence. RAGCentre classifies failures:

- Connection errors, timeouts, `408`, `429` and `5xx` are retried with backoff **for writes only**
  (`PUT`, `DELETE /collections/{name}`, `/upsert`, `/delete`). A `Retry-After` header on `429` or
  `503` sets the minimum delay.
- Reads — `/health`, `GET /collections/{name}` and `/query` — are **never retried**: a degraded
  answer that looks complete is worse than an error.
- `400`, `401`, `404`, `409` and other `4xx` are permanent.

Use `404 collection_not_found` for "this collection does not exist". RAGCentre maps it back to
`collection_info(...) -> None` for the read, and to a "collection not found" error on query and
upsert. Delete and drop stay idempotent and answer success for a missing collection, so a not-found
there is never raised by the client.

## Authentication

When the provider has an `api_key` secret, RAGCentre sends `Authorization: Bearer <api_key>` on
every request **except** `GET /info`, which must answer without credentials (RAGCentre reads it at
creation to pin the service). `GET /health` and all data endpoints must require the token when one
is configured. Each provider receives only its own secret.

## Provider configuration

```yaml
name: my-vector-db
kind: vector_store
type: http
config:
  url: http://127.0.0.1:8702        # required; base URL of the service
  timeout: 30                       # per-request timeout in seconds, default 30
```

`url` and `timeout` are connection settings and can be changed later. The provider's identity is
its id, so a new provider means a new index; a change of `service_version` on the service side does
not silently rewrite one. Set the secret with `ragcentre provider set-secret my-vector-db api_key`.
