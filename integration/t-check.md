# Fragment: t-check (Phase V2, live checking)

Shared files: no `pyproject.toml`, `uv.lock`, `registry.py`, docker-compose, CI or README changes are needed.
The check runs against the in-process stores, so it needs no new service or extra.

## DECISIONS.md (numbered by the lead)

- **V2.1 `check_store(store)` is a contract-lite run, not the contract suite.** `stores/check.py` creates
  `rc_check_<8 hex>`, upserts 50 seeded records (dim 8, every tenth with `lang="rare"`), settles, then for each
  supported metric creates a collection, queries and compares with brute force (`models.filters.matches` as ground
  truth). On the first metric it also runs one filter per operator (`$eq $ne $gt $gte $lt $lte $in $nin`), a selective
  filter with k above the match count, and a delete of two live ids plus one missing id. Every temporary collection is
  dropped in a `finally`. Score tolerance is 1e-3 (the contract suite uses 1e-4): the check proves a capability, it
  does not certify float precision.
- **V2.2 Settle is generic.** There is no adapter hook; for `consistency="eventual"` the check polls
  `collection_info().count` for up to 10 s. A timeout is not an error by itself: the following count step reports it.
- **V2.3 Findings.** `error` = unusable (an operation raised, an unknown id, an id that fails the filter, k exceeded).
  `warn` = works but a named capability is lost (`lost` field): top-k differs from brute force, score differs, count
  wrong, drop failed. With `filter_exact=False` filtered queries are held to recall >= 0.9 and the finding says so.
- **V2.4 Deep test.** `ProviderManager.test(deep=True)` for any `vector_store` provider returns the report under
  `check` (`ok`, `store_type`, `collection`, `records`, `metrics`, `seconds`, `findings[{level, check, message, ms, lost}]`).
  API schema: `StoreCheck` / `StoreFinding`; `ProviderTestResult.check` is `ParserCheck | StoreCheck`.
- **V2.5 CLI.** `ragcentre store check <provider>` calls the server's deep test. `ragcentre store check --type T
  [--config JSON]` builds the store in-process, no server needed. Secrets are read from the environment as
  `RAGCENTRE_<TYPE>_<KEY>` (e.g. `RAGCENTRE_QDRANT_API_KEY`); there is no secret option on argv. Exit 1 when not usable.

What was run: sqlite_vec (file) and Qdrant `:memory:` through `check_store`, the CLI (direct and via a stubbed server
client) and the deep provider test through the API; a fake adapter that fails on upsert (the temporary collection is
dropped). Not run: pgvector and Chroma servers (no Docker service in this task) and any cloud store.
