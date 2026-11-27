# Single-user authentication release testing plan

Date: 2026-11-27\
Target: `feature/single-user-auth`\
Testing branch: `test/single-user-auth-release`\
Initial target commit: `00db4731ec871ea83f90521b0b32633466531a7f`\
Status: initial batch and F1–F4 fixes locally validated; release **NO-GO** pending remaining platform/deployment/integration gates. See [execution results](single-user-auth-release-results.md) for historical failures, follow-up suite counts, artifact hash and unexecuted coverage.

## 1. Purpose and release scope

Establish that a fresh installation, a restarted current-release installation, a browser user, and a CLI/MCP client can safely use the first release. Cover the complete application, with additional depth around account setup, sessions, password recovery, API-key permissions, and deployment changes.

**Owner scope decision (2026-11-27):** no version has been released. Previous-version upgrade, migration-compatibility and rollback rehearsals are **not applicable** to this release, not blocked or passed. This excludes OPS-01, OPS-05 and the predecessor-upgrade/rollback portions of other scenarios from release acceptance. Historical-schema migration cases, including DB-01 and DB-03, may remain as engineering regression tests but are not publication gates. Fresh database initialization, current-release restart/persistence, password recovery and current-release backup/restore remain applicable. This decision overrides predecessor-compatibility requirements in the original scenario inventory below.

This file is the execution specification. Creating it does not mean its tests have passed. Record the exact candidate SHA for each run; after fixes, repeat affected cases and the final required regression suite on the final candidate. Keep new tests and test infrastructure on the testing branch. Track product defects separately and rerun after incorporating fixes.

The scope includes the shipped backend, built UI, CLI, MCP package, migrations, wheel/sdist, Docker image and Compose instructions, built-in retrieval stack, optional vector stores, parser/OCR integrations, query transforms, and evaluation. Multi-user membership and teams remain outside this release.

Use current code, generated OpenAPI, and current README to determine the implemented contract. The earlier authentication design documents contain superseded response shapes and deployment details. A discrepancy affecting security or advertised behavior requires an explicit decision and a regression test; do not silently bless current behavior.

## 2. Priorities, evidence, and acceptance

| Priority | Meaning | Release treatment |
| --- | --- | --- |
| P0 | Authentication bypass, unauthorized mutation or secret access, account takeover, data loss/corruption, unusable setup/recovery | Block release until fixed and retested |
| P1 | Supported installation or core workflow fails; permissions, upgrade, packaging, or regression gate fails | Block release until fixed and retested |
| P2 | Limited usability, presentation, accessibility, or diagnostic defect with a documented workaround | Record impact; release owner explicitly accepts any deferral |

All authentication, authorization, migration, recovery, and secret-handling cases below are P0/P1. Optional integrations advertised as supported are P1 within their declared platform/configuration. An unavailable service or missing credential is a blocked case, not a pass. Cloud coverage may be excluded only with an explicit release-scope decision.

For each run, record: case ID, candidate SHA, operator, UTC time, OS/architecture, Python/Node/browser/service versions, fixture identity, command or steps, expected result, actual result, pass/fail/blocked/not-run, evidence path, defect reference, and retest SHA. Preserve JUnit reports, failure traces, sanitized logs, package hashes, and environment manifests in a private artifact location. Do not commit raw passwords, cookies, CSRF tokens, key secrets, provider credentials, or real user data. Playwright traces and screenshots can contain newly created secrets and need restricted handling.

Release gates:

- All applicable P0/P1 cases pass on the final candidate, with no unexplained skips or flaky failures; predecessor upgrade/migration/rollback cases are not applicable to this first release.
- Backend, UI, MCP, parser-shim, store, contract, build, and live browser gates pass.
- Fresh native setup, both Docker setup paths, operator-token support, and current-release backup/restore rehearsals pass.
- A real MCP client succeeds with a read key and refuses full/legacy full credentials.
- No secret leakage or data corruption; permission-denied requests have no side effects.
- The release owner records any P2 deferrals and the exact tested artifact hashes/SHA before tagging. Tagging and publishing are separate release actions.

## 3. Environments and test data

Use dedicated temporary data directories and uniquely named Compose projects/volumes. Never point tests at the repository's existing `data/` directory. Remove inherited auth, proxy, discovery, and provider settings unless the case requires them. Set discovery to `off` for deterministic core runs; explicitly enable only the fixture shim for OCR tests. Use separate environments for optional engine dependencies.

| Environment | Required coverage |
| --- | --- |
| Linux x86_64, Python 3.11 and 3.13 | Minimum Python support, full backend/MCP regression, native wheel, core live flows |
| Current macOS arm64 | Native setup/CLI/recovery, SQLite extension loading, wheel, browser smoke |
| Current project default Python | Frozen dependency installation and backend CI parity; record the interpreter because backend CI is not pinned |
| Linux, Python 3.13, all extras | CI store matrix and parser shim jobs; isolate from environments with incompatible engine dependencies |
| Node 22 + Chromium | UI CI parity, production build and complete live acceptance |
| Current Firefox and WebKit | Setup/login/logout, account/key UI, upload/query and deep-link smoke; add Playwright projects or run equivalent documented manual checks |
| Docker on Linux and Docker Desktop | Build, account init, remote-setup opt-in, persistence, restart and recovery |
| HTTPS reverse proxy | Secure cookies, forwarded scheme/host, CSRF origin handling, deployment instructions |

Fixture inventory:

- Fresh empty data directory; account-created directory; operator-token-only directory; populated current-release directory for restart and backup/restore checks. Historical pre-auth/schema-0004 fixtures may remain for optional engineering regressions; a previous-release snapshot is not required for the first release.
- Two independent browser contexts/sessions; read key; full key; legacy env token; invalid and revoked credentials. Generate ephemeral values per run.
- Small UTF-8 text and Markdown files with unique phrases, duplicate content, structured metadata, a Unicode filename, an empty file, malformed input, and payloads just below/at/above the configured upload limit including multipart overhead.
- Text PDF, scanned PDF without a text layer, multi-page/table/image PDF, and a deliberately unsupported/corrupt document. Keep expected page/text assertions deterministic.
- At least two pipelines, a completed and a failing ingestion job, an active pipeline, cached results, encrypted provider secrets with a test Fernet key, and a small evaluation JSONL dataset with known answers.
- Deterministic HTTP parser/embedder/reranker/language-model fixtures for timeout, malformed output, authentication failure, and query-transform tests. Use hosted endpoints only for separately recorded live compatibility checks.

## 4. Verified baseline and automation gaps

The target already contains `tests/test_accounts.py`, `tests/test_api_keys.py`, `tests/test_account_cli.py`, `tests/test_request_guard.py`, `tests/test_security_hardening.py`, and `ui/src/features/auth/auth.test.tsx`. These cover several happy paths and selected denial paths; they do not establish the full matrix below.

Known harness work required before claiming release coverage:

1. `ui/e2e/live/acceptance.spec.ts` navigates straight to the connected shell on a fresh installation. It does not create the account. Add a real first-run browser setup journey and authenticated contexts for existing acceptance tests.
2. `ui/e2e/live/api.ts` sends no bearer credential or session cookie. Once setup creates an account, fixture seeding/polling must authenticate too. Use a fixture-owned full key or explicit session plus CSRF; keep browser setup/session tests separate so test helpers cannot mask a broken login flow.
3. `ui/e2e/live/global-setup.ts` checks only that corpora are empty. Preserve that check and assert the intended auth state. Use distinct data directories for fresh setup, existing account, and legacy-token cases. Do not depend on alphabetical test order or a previous test's cookies. Verify isolated execution and retry behavior.
4. Add a real MCP/backend contract test; existing MCP unit mocks alone cannot prove interoperability. The MCP package already declares a `contract` pytest marker.
5. Add password-change and CLI-reset follow-up login tests. Invalidating old sessions is insufficient: a newly issued session must work after each auth-version increment.
6. Expand route-level read-key coverage to every registered route and method, cookie/CSRF security cases, migration/restore tests, concurrent setup/revocation cases, and API-key UI tests. `features/keys/ApiKeys.tsx` currently has no dedicated test file.
7. Add a clean artifact installation check that starts the server and loads the bundled UI. The current release workflow checks wheel contents, CLI help, and import only; it does not run account setup against the installed wheel. It is triggered by tags and does not depend on the CI workflow, so verify the same candidate passed CI before tagging.

Suggested additions on this testing branch: `tests/test_account_passwords.py`, `tests/test_account_lifecycle.py`, `tests/test_auth_route_matrix.py`, `tests/test_account_migration.py`, `tests/test_account_concurrency.py`, `ui/src/features/keys/ApiKeys.test.tsx`, `ui/e2e/live/auth.spec.ts`, and `packages/ragcentre-mcp/tests/test_backend_contract.py`. These were the original proposed deliverables. The [execution report](single-user-auth-release-results.md) identifies the actual test files now implemented and their results.

## 5. Detailed authentication and deployment cases

### A. First run and account validation

| ID | Setup and action | Expected result |
| --- | --- | --- |
| SET-01 | Empty directory, loopback bind, no token; open `/` and protected deep link | UI routes to account setup; no connected application shell before setup; config indicates setup required/allowed |
| SET-02 | Create valid account in browser, refresh, open another tab | One account/credential exists; initial session works; setup disappears; no password/hash in responses |
| SET-03 | Username length 2/3/64/65, uppercase/outer whitespace, invalid leading punctuation, Unicode, embedded whitespace | Valid usernames normalize consistently; invalid values return controlled validation errors without partial rows |
| SET-04 | Password 14/15 characters, 128/129 UTF-8 bytes, multibyte input, spaces, mismatched confirmation | Backend enforces minimum 15 characters and maximum 128 bytes; UI reports failures; rejected input creates no account |
| SET-05 | Submit setup again; race two browser/API setup requests and CLI init against setup | Exactly one account wins; losers get controlled conflict/setup-unavailable responses; no orphaned credential or 500 |
| SET-06 | Bind `0.0.0.0` or IPv6 non-loopback, no credential, no override | CLI refuses startup with actionable message; direct application setup handler also refuses when configured for that bind |
| SET-07 | Repeat with `RAGCENTRE_ALLOW_REMOTE_SETUP=1`; then restart without it after setup | Startup and setup succeed with explicit opt-in; existing account permits later startup; setup remains permanently unavailable |
| SET-08 | Loopback IPv4/IPv6 and `localhost`; test `--allow-no-token` separately | Bind recognition is correct; legacy development bypass behavior is documented; browser still follows the intended setup route |
| SET-09 | Legacy token set, no account; inspect UI/config and create account | Existing bearer clients continue working; setup follows bind policy; account and legacy token coexist afterward |
| SET-10 | Database write failure during account/credential creation, retry | Transaction rolls back; no half-created account; clear error; retry can complete |

For SET-01, the backend currently permits anonymous resource access with neither account nor token, even though the browser requires setup. Test this explicitly and record it as the supported loopback development contract; do not mistake UI routing for backend access control. A remotely exposed opt-in/bypass instance must be evaluated against its documented pre-setup exposure and deployment instructions.

### B. Login, sessions, and recovery

| ID | Setup and action | Expected result |
| --- | --- | --- |
| SES-01 | Valid login; normalized username; wrong password, unknown username, disabled account | Success creates fresh session; failures use generic credentials error; disabled account cannot authenticate |
| SES-02 | Inspect DB and HTTP responses after setup/login | Argon2id password hash; random session token stored only as a hash; no password/session-token leakage in JSON or logs |
| SES-03 | Login twice from independent contexts; reuse cookies; restart service | Session tokens differ; both valid sessions work until revoked/expired; valid persisted sessions survive restart |
| SES-04 | Unknown, tampered, malformed, expired, and revoked session cookies; changed account status/auth version | Protected requests fail with 401; no server exception; UI moves to sign-in |
| SES-05 | Logout session A with valid CSRF; replay its cookie; continue using B | A is revoked and cookie cleared; B remains valid; browser back/refresh exposes no live authenticated data |
| SES-06 | Change password with wrong current password or invalid new password | Validation failure; existing credentials and sessions remain usable; no auth-version increment |
| SES-07 | Change password correctly while two sessions exist, then log in with new password | Both old sessions stop working; old password fails; **new login and its subsequent protected requests succeed** |
| SES-08 | Repeat password change and login a second time | Sessions track the current auth version after every change; no permanent lockout after recovery |
| SES-09 | Run `account reset-password`, then browser login | Old sessions/password fail; new session works; account/resources survive; API keys and legacy token follow documented independent lifetime |
| SES-10 | CLI init duplicate, reset without account, confirmation mismatch, invalid credentials, cancelled prompt | Nonzero controlled failure, no partial writes or password echo; no scheduler/workers started by local account commands |
| SES-11 | Advance controlled clock around 30-day expiry, including equality boundary | Absolute expiration follows a documented boundary; reads cannot extend the lifetime unexpectedly |
| SES-12 | Simulate missing/corrupt credential hash and failed DB writes | Controlled errors, no authenticated principal or leaked hash; recovery behavior is documented |
| SES-13 | Refresh protected deep link; expire session during upload, query, polling, or key creation | One clear sign-in transition; no redirect loop, stale private cache, accidental repeated mutation, or endless spinner |

### C. Authorization, cookies, CSRF, and request guard

Build a parameterized route inventory from FastAPI routes and compare it with an explicit expected permission table. Include docs/OpenAPI, unknown API paths, OPTIONS/HEAD behavior, alternate methods, and both populated and empty auth states. Keep expected permissions independent of the implementation allowlist.

| ID | Setup and action | Expected result |
| --- | --- | --- |
| SEC-01 | Account exists; request every API route anonymously | Only intended public methods/routes are public: GET health/config/whoami and POST login/setup; protected operations reject before side effects |
| SEC-02 | Valid session or bearer against protected routes; unknown/invalid credential | Correct principal/scope, 401 with expected error envelope and challenge when unauthorized; no private response data |
| SEC-03 | Valid session plus invalid bearer; read bearer plus valid full session; legacy bearer plus session | Bearer takes precedence; invalid bearer cannot fall back to cookie; read key cannot inherit full session authority |
| SEC-04 | Session POST/PUT/PATCH/DELETE with missing, wrong, other-session and valid CSRF | Invalid combinations return 403 `csrf_invalid` without mutation; valid combination works; safe reads remain usable |
| SEC-05 | Mutate with valid CSRF and origins varying host, scheme, port, IPv6, absent/null/malformed origin | Intended same-origin policy is explicitly asserted; foreign origins denied; any hostname-only implementation discrepancy is triaged, not encoded as expected secure behavior |
| SEC-06 | Login/setup requests from a foreign origin, including with an existing cookie | Assess login/setup CSRF and unintended account/session replacement; define and test the intended protection before sign-off |
| SEC-07 | Inspect HTTP and real HTTPS/proxy cookies and logout/password-change deletion | HttpOnly, SameSite=Lax, Path=/, host-only, intended 30-day max age; HTTPS has Secure; forwarded headers cannot downgrade/misissue cookies |
| SEC-08 | Bearer read/full requests without CSRF | Bearer clients remain usable within scope; session-only routes still deny bearer credentials |
| SEC-09 | Dot segments, double slashes, backslashes, encoded slashes, trailing slash, route prefix, query strings | No authentication/allowlist bypass after proxy/router path normalization; query strings do not broaden access |
| SEC-10 | Oversized and streamed bodies, invalid/duplicate Content-Length, multipart requests | Existing guard returns controlled 400/413; auth refactor does not bypass size checks or parse unauthorized uploads |
| SEC-11 | API errors and docs/content responses | Security headers remain present, docs obey authentication, uploaded content remains sandboxed; existing security suites pass |
| SEC-12 | Inspect browser storage, query cache, logs and error payloads across logout/login | Browser no longer relies on pasted token storage; secret material is absent from diagnostics; prior authenticated data does not reappear improperly |

### D. Named API keys and external clients

| ID | Setup and action | Expected result |
| --- | --- | --- |
| KEY-01 | Session creates read/full keys; list, refresh and reopen page | Secret appears only in creation response/reveal UI; list exposes safe metadata only; DB stores token hashes |
| KEY-02 | Empty/whitespace/oversized name; invalid/missing scope; UI cancellation | Controlled validation; no unintended key; UI defaults match contract; display escapes user-supplied text |
| KEY-03 | Each read key calls every allowlisted route with real IDs | Can read corpora/documents/content/parsed/outline/chunks, pipeline status, whoami/health, and POST corpus query; result payloads stay in scope |
| KEY-04 | Read key tries every other API route/method | 403 `read_only_token` on protected forbidden routes; no provider/secret/admin/evaluation/key mutation or resource side effect |
| KEY-05 | Full key and legacy token manage normal resources; attempt account/session/key management | Resource operations succeed; session-only `/keys`, `/auth/me`, `/auth/csrf`, password change and logout remain inaccessible to bearer-only callers |
| KEY-06 | Revoke key, reuse immediately, revoke missing ID | Revoked key returns 401; missing key has controlled 404; other keys remain usable; stale UI does not show a revealed secret |
| KEY-07 | First use, repeated rapid use, use after 60 seconds, restart | `last_used_at` updates within intended throttle and persists; authentication remains correct under concurrent calls |
| KEY-08 | Browser copy/reveal/revoke flows, clipboard denial, failure/retry, keyboard navigation | Clear one-time secret warning, accessible controls, safe error states; failed create/revoke does not misreport success |
| KEY-09 | CLI with read/full/legacy/invalid credentials against live server | Full/legacy complete normal CLI workflow; read key only performs permitted commands; denied commands give useful errors |
| KEY-10 | Real MCP startup with read/full/legacy/invalid/revoked credentials | Read key passes whoami scope check; full/legacy full keys are refused by default; explicit `allow_full_token` override is tested separately; invalid/revoked credentials fail clearly |
| KEY-11 | MCP search/list/fetch over a populated corpus; revoke while MCP runs | Useful read-only results; no write tools; next backend access after revocation fails; no fallback to broader credentials |

## 6. Wider application regression

Run these journeys with a real browser session and real backend. Repeat applicable API actions with a full key; repeat retrieval with a read key. Use built-in providers first so external service failures cannot hide core regressions.

| ID | Journey | Assertions and existing coverage |
| --- | --- | --- |
| APP-01 | Provider create/list/detail/test/secret update/delete | Correct schemas, redacted secrets, immutable settings and useful connection errors; `test_m3_providers_api.py`, provider UI tests |
| APP-02 | Corpus create/update/list/detail and metadata-schema changes | Correct validation, pagination/filtering, isolation and persisted counts; `test_m6_corpora_api.py`; do not assume an unimplemented corpus-delete endpoint exists |
| APP-03 | Upload → parse → chunk → embed → index → ready | Expected content/chunks and job transitions; duplicate handling, failure/retry and cancellation where supported; `test_m6_ingestion.py`, live acceptance |
| APP-04 | Pipeline clone/plan/build/activate/archive | Parse/cache reuse, invalid config, active archive refusal, status and diff; `test_m5_pipelines.py`; lifecycle checks follow implemented operations |
| APP-05 | Dense/sparse/hybrid retrieval, filters and reranking | Known document returned with correct provenance; bounded top-k, filter isolation, empty result and downstream error behavior; `test_m7_retrieval.py`, `test_query_equivalence.py` |
| APP-06 | Query transforms and enrichment | Transform on/off, cache, generated query provenance, malformed/timeout errors and equivalent non-transformed path; `test_query_transforms.py`, `test_enrichment.py`, `test_llm_provider.py` |
| APP-07 | Evaluation dataset upload → two runs → comparison/detail | Correct metrics/items, invalid JSONL errors, polling and persistence; `test_m8_eval.py`, evaluation UI tests, live acceptance |
| APP-08 | Document detail/content/outline/chunks/download | Unicode filename, correct media/security headers, missing IDs, deterministic parsed content, no active content execution |
| APP-09 | Parser failure → inspect job → repair → retry | Correct permanent/retryable state; descendants unblock; no duplicate indices; live acceptance and activity tests |
| APP-10 | Delete/purge/GC and broken index recovery | Documents/vectors/blobs/caches converge correctly; restart does not resurrect deleted data; `test_m11_gc_and_contract.py`, CLI tests |
| APP-11 | Shell/navigation/deep link/not-found/account/key pages | Production assets load; back/reload and 401 behavior correct; API unknown paths remain JSON; `test_m11_ui_serving.py`, live smoke |
| APP-12 | Mock development mode versus live production build | Setup/login/key interactions reflect real contract; MSW cannot make live tests pass; production build does not enable mock handlers |

Run all existing backend tests, not just the listed anchors. Include contract/catalog/parser compatibility, SQLite vector correctness, blob encryption, SSRF/egress, no-ML-import checks, vector adapter regressions, UI component tests and generated API type checks.

## 7. Migration, packaging, Docker, and recovery

| ID | Procedure | Expected result |
| --- | --- | --- |
| OPS-01 | Previous-version upgrade and migration-compatibility rehearsal | Not applicable: no version has been released |
| OPS-02 | Create account/keys on a fresh installation, restart twice | Schema initialization is idempotent; account/session/key data persist; setup cannot reopen |
| OPS-03 | Interrupt fresh database initialization/startup in a disposable copy; restart | Atomic initialization or controlled recovery; no falsely recorded successful version or partial schema |
| OPS-04 | Back up the current release while stopped, including DB/WAL handling, blobs, stores and encryption config; restore into separate directory | Account login, keys, provider secrets, content and known queries work; no dependency on original machine paths |
| OPS-05 | Previous-version rollback rehearsal | Not applicable: no previous released artifact exists |
| OPS-06 | Build UI, wheel and sdist; install wheel in clean venv outside checkout, with no source/PYTHONPATH fallback | CLI/import work; native Argon2 and sqlite-vec load; server serves bundled assets; setup/login/upload/query/recovery work |
| OPS-07 | Build a wheel from sdist in a clean environment and repeat installed smoke | UI included in both artifacts; package metadata/version and dependencies correct; recorded artifact hashes identify tested files |
| OPS-08 | Fresh Compose project: account init in one-off container, then normal startup | Shared named volume contains account; no token required; browser login, ingestion/query and restart work |
| OPS-09 | Fresh project with remote setup opt-in; create account in browser; restart without opt-in | Setup works once; persisted account allows normal startup; no second account |
| OPS-10 | Fresh project with neither account/token/opt-in; legacy-token-only project | First fails startup clearly; second serves authenticated API; README instructions and browser behavior match actual deployment |
| OPS-11 | Container restart/recreate and local CLI password reset against the same mounted volume | Persistent data survives; old sessions invalidated; new login works; volume ownership permits writes without broad permissions |
| OPS-12 | HTTPS proxy, host/proxy headers, remote bind and browser deep links | Secure session operation, correct setup policy and CSRF behavior; only intended trusted proxy headers influence scheme/client identity |
| OPS-13 | Provider secrets after upgrade/restore with correct, missing and wrong Fernet key | Correct key decrypts only intended provider secrets; missing/wrong key produces controlled errors, no plaintext leakage |
| OPS-14 | Follow published setup/key/MCP/backup instructions from clean environments | Commands and screenshots/labels match release; token documentation reflects session-only exceptions and MCP's read-key requirement |

The repository's release workflow publishes on `v*` tags. Rehearse its build steps without pushing a tag. Use only disposable Compose projects; volume removal is cleanup of that project's test fixtures, never an existing deployment volume.

## 8. Optional stores, parser shims, and external services

Reproduce `.github/workflows/ci.yml` jobs with the same versions, ports, environment variables, and dependency isolation; `tests/docker-compose.yml` provides the local services. Read each adapter test's `RAGCENTRE_TEST_*` overrides before running it. The integration marker alone is insufficient: some adapter tests skip dynamically rather than using that marker.

- Stores: SQLite-vec, Qdrant, pgvector, Chroma embedded/server, LanceDB, Redis, Weaviate, Elasticsearch, OpenSearch, Milvus, Pinecone local, and MongoDB search-capable service. Require real create/index/query/filter/delete behavior and cleanup; record server versions. Use Python 3.13 for all-extras coverage. The CI backend deliberately avoids Chroma on newer Python because of native wheel compatibility.
- Hosted store claims: run separately with disposable namespaces and credentials for the supported hosted deployment when local emulation cannot prove compatibility. Report these separately from local results.
- Run shimkit and each example shim's engine-independent tests with the isolated commands in CI. Run Docling separately with its required `docling-core` dependency. These tests do not prove OCR model readiness.
- Live OCR: for each parser advertised as working in this release (Docling, MinerU, PaddleOCR, Surya, EasyOCR, docTR), run its documented live setup against a scanned fixture with no text layer. Discovery → add → test OCR → ingest → query must succeed. Distinguish service discovery from successful OCR. Exercise engine missing, timeout, invalid key, malformed response and stopped-service states.
- Remote engine mode: smoke only currently implemented/documented modes with explicit endpoints; no network scanning. Record unavailable hardware/model downloads as blocked, with a release-scope decision.

## 9. Concurrency, resilience, and performance

Use bounded workloads in isolated test environments. Record baseline and candidate on the same hardware, interpreter, data, worker count and service versions; never compare unrelated runs.

| ID | Workload | Acceptance |
| --- | --- | --- |
| RES-01 | Two simultaneous setup attempts and CLI/browser setup race | Exactly one account; controlled loser response; database remains consistent |
| RES-02 | Simultaneous key use/revocation and session use/password reset | Requests beginning after completed revocation/reset fail; permitted in-flight semantics documented; no privilege resurrection |
| RES-03 | Login limiter: fifth failed attempt, next attempt, normalized username, peer address, window advance, successful reset | Intended five-failure/60-second process-local behavior; controlled 429; no sleep-based unit tests; limitation across restarts/processes documented |
| RES-04 | Small bounded concurrent login batch with valid/invalid users; unique-username bucket growth | Verification concurrency stays within intended bound; useful latency/resource measurements; limiter state does not grow without an accepted operational bound |
| RES-05 | Restart during ingestion/indexing and after auth/key writes | No lost committed credentials, duplicate jobs, corrupted index or unusable account; retry/recovery works |
| RES-06 | Small disposable volume out of space; read-only data directory; transient DB lock | Controlled actionable errors, transaction rollback, no secret leakage; service recovers after restoring resources |
| RES-07 | Representative read-key query traffic plus session reads and background ingestion | Capture throughput, p50/p95/p99, CPU/RSS, error rate and SQLite lock errors; no unexpected 5xx or monotonic resource growth |
| RES-08 | Thirty-minute mixed query/upload/polling run with periodic login/logout and service restart | Correct results, no unbounded queue/resource growth, stable key/session validity and recoverable failures |

Initial performance investigation thresholds: more than 20% regression in p95 warm-query latency or throughput, sustained RSS growth over repeated equal batches, any unexpected database lock error, or unexpected 5xx. These are investigation triggers, not previously measured product SLAs. Account creation/login includes deliberate Argon2 cost; report cold/warm timings separately and select a user-facing budget before final sign-off.

## 10. Execution commands and order

Commands below describe future execution. Run from the repository root unless a subshell changes directory. Save full output and exit codes, and add `--junitxml=<private-evidence-path>` to pytest commands when collecting evidence. Review skips after every test run.

### Phase 1: establish fast baseline

```sh
uv sync --frozen --extra qdrant --extra pgvector
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest tests/test_accounts.py tests/test_api_keys.py tests/test_account_cli.py tests/test_request_guard.py tests/test_security_hardening.py
uv run pytest -m 'not integration and not cloud'
(cd ui && npm ci && npm run lint && npm test && npm run check:contract && npm run build)
(cd packages/ragcentre-mcp && uv sync --frozen && uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest)
```

Run minimum-Python and Python-3.13 jobs in separate environments. Inspect `check:contract` changes: generated types must agree with the backend, not be accepted merely by regenerating and committing an unexpected contract change.

### Phase 2: implement coverage gaps and run live journeys

Implement section 4's harness/coverage additions, then run all new cases plus existing regression tests. Make auth cases runnable independently with fresh fixture state. Run session invalidation and permission tests deterministically with controlled clocks/database fixtures; use real HTTP/browser paths for end-to-end checks.

```sh
(cd ui && npx playwright install --with-deps chromium && npm run e2e)
```

Run this with a fresh unique `RAGCENTRE_E2E_DATA_DIR` and available `RAGCENTRE_E2E_PORT`/`RAGCENTRE_E2E_FIXTURE_PORT` when overriding defaults. Playwright currently starts a real fixture parser and backend. Confirm inherited env tokens do not change the intended initial state. Run isolated auth tests and full suite, then one repeat from a fresh directory to detect order dependence; do not use retries to classify a failure as passed.

### Phase 3: services and deployment

In a dedicated Python-3.13 environment, install `uv sync --frozen --all-extras`. Start the disposable store stack and reproduce the explicit store test list in CI in addition to integration-marked tests. Capture service health and skip reasons. Shut down only the stack created by this run. Run shim jobs using CI's isolated commands, then live engine and hosted-service cases according to the approved release scope.

Rehearse native/Docker/proxy deployment, fresh database initialization and current-release restore cases. Build artifacts only after the UI build:

```sh
uv build
```

Install each exact artifact into a new environment outside the source checkout and execute OPS-06/07. Do not treat a source-checkout server or existing `dist/` file as evidence for a newly built artifact.

### Phase 4: final candidate

Incorporate approved fixes, record the final SHA, rerun all required gates, affected schema-initialization/deployment journeys, and the bounded resilience run. Ensure the artifact being proposed for release was built from that SHA. Complete the result ledger, defects and sign-off. No release tag or publication is part of executing this testing plan.

## 11. Work packages and completion tracking

Work packages can run independently once the fixture/auth harness exists. Assign an actual owner and evidence location before execution; keep product changes from obscuring test results.

| Work package | Dependencies | Deliverable | Status |
| --- | --- | --- | --- |
| W1 Baseline and environment inventory | None | CI-equivalent output, skip inventory, version manifest | Partial: native baseline recorded; Linux/other Python/Node 22 pending |
| W2 Password/session/CSRF/route matrix tests | W1 | Deterministic regression tests for SET/SES/SEC/KEY | Partial: confirmed recovery/origin defects fixed; added deterministic credential races, persisted recovery and strict-origin regressions pass; wider cases pending |
| W3 Authenticated browser harness and key UI | W1 | Independent auth journeys plus restored live acceptance | Partial: 16 Chromium journeys pass including isolated password-change/CLI recovery; expanded browser/accessibility cases pending |
| W4 CLI/MCP interoperability | W2 fixtures | Real backend contract results, recovery transcript | Local gate passes: CLI recovery and 14 real-backend MCP contracts, including actual stdio tools; remote CI matrix pending |
| W5 Fresh setup/backup/artifact/Docker/proxy | Candidate with relevant fixes | OPS evidence and tested artifact hashes | Partial: wheel smoke and synthetic migration/SQLite restore pass; previous-release upgrade/rollback not applicable; current-release backup/restore and full deployment rehearsal pending |
| W6 Stores/shims/live integrations | Dedicated service environments | Per-integration executed/blocked report | Partial: 547 shim tests pass, 6 engine cases skip; external-store/live-engine gates pending |
| W7 Concurrency/resilience/performance | W2/W3 and stable candidate | Measured baseline/candidate results and defects | Migration concurrency and deterministic credential-race boundaries pass; broader fault/soak/performance not started |
| W8 Final regression and sign-off | W1–W7 | Completed ledger and release decision | NO-GO: local full regression has 1,854 passes and no failures; full environment/deployment gate coverage incomplete |

Suggested scheduling: complete W1 first; use its failures to prioritize W2/W3. Run W4–W6 after their dependencies are ready; reserve a separate final-candidate pass after fixes. Estimate duration after baseline results and service availability are known rather than promising a completion date before the live-engine and migration requirements are exercised.

## 12. Result ledger and release decision template

Copy one ledger row per case and environment into the run record. Automated parameterized cases should retain individual pytest/Playwright identifiers.

| Case/environment | SHA | Expected | Actual/evidence | Result | Defect/retest |
| --- | --- | --- | --- | --- | --- |
| Example: SES-07 / Linux Python 3.13 | Pending | New password creates usable session after revoking both old sessions | Pending | Not run | Pending |

Release decision record:

```text
Candidate SHA:
Wheel/sdist/image identifiers and hashes:
Run dates and evidence location:
Required environments completed:
P0/P1 open defects: [must be zero]
P2 accepted deferrals and owner:
Skipped/blocked tests and explicit scope decisions:
Fresh setup / legacy token / upgrade / restore / Docker / HTTPS results:
Backend / UI / browser / MCP / store / shim results:
Performance and resilience findings:
Documentation verified:
Decision: GO / NO-GO
Release owner and date:
```

A green CI badge alone does not complete this plan. The decision must include the installed artifact, authentication lifecycle, deployment, fresh schema initialization/current-release restore and real client results described above; predecessor upgrade/migration/rollback is not applicable.

## 13. Expanded coverage model

The additional cases below are required alongside sections 5–9. Each table row is a scenario family: generate individually named tests for every listed boundary, credential, method, or failure point. A row with ten input variants is not complete after one example passes.

### Coverage dimensions and selection rules

| Dimension | Values to exercise | Selection rule |
| --- | --- | --- |
| Installation/auth state | Fresh/no token, token only, account only, account + token, upgraded populated directory | Full auth/config/setup/startup matrix; representative application journey in each applicable state |
| Principal | Anonymous, valid session, invalid/expired/revoked session, read key, full key, legacy token, invalid/revoked key | Every registered API method × principal; include mixed bearer/cookie cases |
| CSRF | Absent, correct, incorrect, different session, stale after login/reset | Every session-authenticated mutation; bearer-only routes tested separately |
| Resource state | Present, absent, wrong corpus, archived/deleted, pending/running/failed/ready | Every applicable endpoint family; never substitute only nonexistent IDs for successful read authorization |
| Input shape | Valid, missing, null, wrong primitive/container type, unknown field, boundary lengths/numbers | Every request model and shared pagination/filter parser |
| Dependency result | Success, timeout, refused connection, 401/403, 429, 5xx, malformed schema, partial result | Every HTTP provider kind using deterministic local fixtures |
| Lifecycle | First use, repeat, concurrent use, process restart, backup restore | Every persisted auth/resource/cache type |
| Client | Browser, backend API, CLI, MCP | Equivalent supported operations; client-specific failures tested independently |

Use exhaustive coverage for authorization and credential precedence. Use pairwise combinations for lower-risk deployment/configuration interactions, with explicit extra cases for account + token + HTTPS proxy, upgrade + encrypted secrets + external store, and password reset + two live sessions + ongoing polling. Record the generated pairwise inputs so failures are reproducible. Do not take a Cartesian product of every browser, store, and input shape.

Keep an inventory containing route/method, request model, expected principal access, existing tests, new tests, applicable case IDs and execution status. Enumerate FastAPI routes at test collection, including installed contract hooks. Fail if any API route lacks an explicit classification. Cover documented routes absent from the running inventory as contract defects rather than ignoring them.

### Explicit permission oracle

These rules apply when an account or legacy token makes authentication required. They describe intended permissions independently of how middleware currently implements them.

| Operation family | Anonymous | Session | Read key | Full key / legacy token |
| --- | --- | --- | --- | --- |
| GET health/config/whoami | Public, no private account/credential data | Public; whoami reflects principal | Public; whoami reflects principal | Public; whoami reflects principal |
| POST setup | Only with no account and allowed bind | Cannot create another account | Cannot create another account | Cannot bypass setup gate |
| POST login | Credential validation and limiter apply | Credential validation and limiter apply | Credential validation and limiter apply | Credential validation and limiter apply |
| GET auth/me and auth/csrf | Denied | Allowed | Denied | Denied |
| POST password/logout; create/delete keys | Denied | Allowed with valid CSRF | Denied | Denied |
| GET keys | Denied | Allowed | Denied | Denied |
| Read allowlist resources and POST corpus query | Denied | Allowed; query requires CSRF | Allowed | Allowed |
| Providers/provider-types/secrets, OCR operations, pipeline detail/diff, node runs, evaluation, admin | Denied | Allowed; mutations require CSRF | Denied | Allowed |
| Other resource mutations | Denied | Allowed with CSRF | Denied | Allowed |
| API docs/OpenAPI | Denied | Allowed | Denied | Allowed |
| Static UI GET/HEAD | Public shell/assets; UI auth gate still applies | Public assets | Public assets | Public assets |

For forbidden protected routes, anonymous/invalid credentials should receive 401; read keys receive 403 before resource lookup; session-only endpoints deny full bearer credentials according to their explicit API error contract. Assert exact error code/status from the current documented contract in individual tests. OPTIONS and HEAD must be classified explicitly, not assumed public from their method name. Login/setup are public dispatch routes, not privileged bearer operations; valid bearer credentials do not relax their own rules.

The read allowlist includes GET corpora and corpus detail, corpus documents/pipelines, document detail/chunks/content/parsed/outline, chunk detail, pipeline status, and POST corpus query, plus public health/whoami. Test each concrete route with populated fixtures and inspect returned fields. Pipeline detail, provider catalogs, evaluation and administrative endpoints are not implicitly allowed by being GET routes.

## 14. API validation, protocol, and pagination

Execution: backend API/contract tests by default, plus real HTTP for parsing/proxy behavior that an in-process client normalizes. Priority P1 unless the failure leaks data or permits mutation (P0).

| ID | Inputs and procedure | Expected result |
| --- | --- | --- |
| VAL-01 | For each JSON request model, omit each required field; send null, number, boolean, list and object in place of strings | Consistent documented validation errors; no 500, unintended coercion or partial write |
| VAL-02 | Add unknown fields such as `scope`, `is_platform_admin`, `auth_version`, `team_id` and nested extras where forbidden | RequestModel's extra-forbid policy holds; no mass assignment or hidden privilege change |
| VAL-03 | Empty/malformed JSON, truncated UTF-8, scalar JSON, wrong Content-Type and missing body | Controlled protocol/validation responses; rejected request leaves state unchanged |
| VAL-04 | Duplicate JSON keys, repeated query parameters and duplicate auth/cookie headers through supported server/proxy | Parsing and precedence are documented and consistent; ambiguity cannot broaden permissions; do not require a particular parser policy without a contract |
| VAL-05 | Query `top_k` absent, null, 0, 1, 1000, 1001, negative, fractional and string | Bounds 1–1000 enforced with documented coercion; defaults/null behave consistently |
| VAL-06 | Page size below minimum, minimum, maximum, over maximum; empty/negative/non-numeric/oversized cursor | Controlled validation; bounded result sizes and no exception or unbounded allocation |
| VAL-07 | Traverse corpus/provider/document/pipeline/run/eval pages with more than two pages and equal creation timestamps | Stable tie-break order; no missing/duplicate items on unchanged dataset; correct terminal next_cursor |
| VAL-08 | Insert/delete while traversing offset pagination | Record documented consistency limits; no invalid cursor/server crash; do not claim snapshot semantics for offset pagination |
| VAL-09 | Query/filter values containing quotes, SQL syntax, wildcard characters, Unicode and control characters | Values treated as data; filters/search do not broaden results or break queries |
| VAL-10 | Missing IDs and ID from another corpus in nested endpoints | Correct 404/validation behavior for authorized callers; no cross-corpus substitution |
| VAL-11 | Query/filter invalid operators, incompatible types, empty groups, null values and excessive but bounded nesting | Explicit validation or documented semantics; no unfiltered fallback or recursion exception |
| VAL-12 | Response shapes on success/error, empty lists, optional fields and 204 replies | Match generated OpenAPI and UI/MCP models; 204 has no JSON body; no internal exception paths/secret inputs in errors |

## 15. Documents, metadata, blobs, and ingestion

Execution: backend integration tests with built-ins, selected live browser journeys, and storage assertions in isolated fixtures. P1 by default; data loss, traversal or cross-corpus leakage is P0.

| ID | Procedure | Expected result |
| --- | --- | --- |
| DOC-01 | Upload same bytes twice with no external ID; upload same bytes with distinct filenames/metadata | Identity/deduplication follows current contract; document metadata remains independent; shared blobs are not prematurely deleted |
| DOC-02 | Upload same external ID twice in one corpus with changed content | First returns 201, replacement/upsert returns documented 200; one logical document, updated content and correct reingestion |
| DOC-03 | Same external ID in two corpora; simultaneous upserts in one corpus | Corpora stay isolated; same-corpus upsert is serialized; no duplicate active logical document or orphaned run |
| DOC-04 | Replace document content while pipeline is ready and while ingestion is running | Content version advances correctly; stale workers cannot overwrite newer results; eventual query returns only current content |
| DOC-05 | Patch metadata/attributes before and after index completion | Returned/query metadata agrees with source; only required work reruns; arbitrary attributes cannot mutate reserved state |
| DOC-06 | Metadata schema str/int/float/bool; wrong scalar types, undeclared fields and schema change with existing documents | Validation and compatibility follow schema contract; boolean/numeric ambiguity is explicitly checked; rejected change preserves old schema/data |
| DOC-07 | Multipart metadata/attributes invalid JSON, JSON array/scalar and missing file | Controlled errors; no document/blob/job written for rejected upload |
| DOC-08 | File at/around total request limit, empty file, unsupported MIME and filename/MIME disagreement | Limit includes multipart overhead; predictable validation/parser failure; no truncated successful document |
| DOC-09 | Filename with traversal segments, absolute path, HTML, CR/LF, Unicode and very long name | File cannot escape data directory; download headers valid; UI escapes text; no script execution |
| DOC-10 | Multi-page document with repeated headings, tables, images and blank pages | Parsed structure, page references, chunk provenance and outline match fixture expectations; supported fidelity losses recorded |
| DOC-11 | Document state filter across queued/running/failed/ready pipelines, including archived pipeline | List counts/status match actual live runs; archived pipeline state does not incorrectly dominate active documents |
| DOC-12 | Delete while querying/ingesting; repeat delete; restart before purge | Document immediately follows deletion contract; eventual vector/blob purge completes; no resurrection or deletion of shared live blob |
| DOC-13 | Read content/parsed/chunks/outline before ready, after failure and after delete | Documented pending/404/error behavior; no unrelated version's content or internal filesystem path |
| DOC-14 | Modify one corpus, clear cache/GC, and query a second corpus sharing provider/blob fixtures | Other corpus remains intact; no shared-resource cleanup regression |

## 16. Pipeline, retrieval, and evaluation correctness

Execution: deterministic backend/provider fixtures, existing regression suites, and selected browser journeys. P1; cross-corpus result leakage or destructive reuse is P0.

| ID | Procedure | Expected result |
| --- | --- | --- |
| PIPE-01 | Submit DAG with cycle, missing dependency, incompatible node input, wrong provider kind and duplicate node ID | Validation before scheduling; no partial pipeline/run graph |
| PIPE-02 | Clone unchanged pipeline, then vary parser/chunker/embedder/index/retrieval settings one at a time | Fingerprints and plan reuse match semantic dependencies; changed stage and descendants rerun, unrelated ancestors reuse |
| PIPE-03 | Change provider secret or mutable metadata without changing semantic config | Reuse/invalidation follows documented fingerprint policy; secret material never appears in plans or hashes exposed as plaintext |
| PIPE-04 | Activate unready, failed, archived and foreign-corpus pipelines | Controlled refusal; corpus's previous active pipeline remains intact |
| PIPE-05 | Switch between two ready pipelines while queries run | Each query uses a coherent pipeline/config snapshot; results identify actual pipeline; no mixed dimensions/indices |
| PIPE-06 | Retry failed node once and twice, retry nonfailed node, retry pipeline with no failed jobs | State transitions and counts correct; no duplicate running job or stale retry lease |
| PIPE-07 | Parser succeeds but embed/index fails, then repair and retry | Successful ancestors reused; downstream completes once; no duplicate vectors |
| PIPE-08 | Archive non-active pipeline while workers run; rebuild another pipeline sharing caches | Archive policy respected; workers and shared cache cleanup cannot damage live pipeline |
| RET-01 | Known fixtures for dense only, keyword only and hybrid | Expected relevant documents, unique chunks, stable rank/score schema and correct source provenance |
| RET-02 | Equal scores and repeated identical requests | Deterministic tie-break behavior where promised; no duplicated chunk across fused sources |
| RET-03 | Empty corpus, no active pipeline, building pipeline; toggle allow_partial | Explicit errors or documented partial semantics; no false ready/success or accidental fallback pipeline |
| RET-04 | Filter matches none/all/one; combine metadata and document restriction across corpora | Only intended documents returned; top_k and fusion cannot reintroduce excluded hits |
| RET-05 | Reranker returns fewer/more results, duplicate/unknown IDs, malformed scores or timeout | Explicit provider validation/error semantics; no unrelated hit or silent success with corrupt ranking |
| RET-06 | Embedder dimension mismatch, empty vector, NaN/Infinity and batch-count mismatch | Fail safely before corrupting index; retries cannot mix incompatible vectors |
| RET-07 | Query-transform cache with changed query/model/prompt/settings, transform=false and provider failure | Cache keys distinguish semantic inputs; disabled path bypasses model; failure does not silently become ordinary search |
| RET-08 | Request debug/enrichment/context fields with read key; inspect all response metadata | No provider credentials/system-only configuration leak; response remains compatible with CLI/MCP limits |
| RET-09 | Unicode, whitespace-only, long bounded query, punctuation and repeated terms | Defined validation/tokenization; no 500, unexpected corpus expansion or broken serialization |
| EVAL-01 | JSONL with blank lines, malformed row, missing required fields, duplicate item IDs and invalid references | Actionable row-level validation; atomic or explicitly documented partial import; no phantom dataset |
| EVAL-02 | Dataset belonging to corpus A evaluated against pipeline from B or missing pipeline | Controlled rejection before evaluation starts; no cross-corpus scoring |
| EVAL-03 | Hand-computed tiny ranking fixture with known relevant positions | Metrics and aggregate denominator match expected arithmetic; empty/no-relevance handling specified |
| EVAL-04 | One query fails during evaluation, then server restarts | Run/item failure and aggregate status honest; progress persists; no completed report hiding failed items |
| EVAL-05 | Compare two pipelines over identical items, with pagination and a deleted/unavailable source document | Item alignment correct; useful unavailable-reference behavior; no index-based comparison mismatch |
| EVAL-06 | Delete dataset with dependent runs and inspect surviving views | Current deletion contract enforced; no dangling run that crashes list/detail screens |

## 17. Browser behavior, accessibility, and privacy

Execution: component tests for edge states, real Chromium journeys for auth/storage/network, Firefox/WebKit smoke, and keyboard/manual accessibility checks. Visual/usability issues are P2 unless they block a core operation; auth leakage is P0/P1.

| ID | Procedure | Expected result |
| --- | --- | --- |
| WEB-01 | Submit setup/login/change-password by Enter and button, rapidly double-submit | Form validation identical; pending state prevents duplicate mutation; returned errors remain visible |
| WEB-02 | Password manager/autofill, paste long Unicode password, reveal/hide, confirmation mismatch | Correct autocomplete/labels, no silent truncation/normalization; reveal does not change submitted password |
| WEB-03 | Keyboard-only setup/login/key creation/revocation; focus after errors/dialog close | Logical focus order, visible focus, no trap; associated error messages and usable dialogs |
| WEB-04 | Screen-reader landmarks, inputs, loading/error announcements and icon buttons | Controls have useful names; state changes announced; errors are not conveyed only by color |
| WEB-05 | 320px/375px viewport, tablet, desktop and 200% zoom | Forms, navigation and key secret display remain usable; no hidden primary controls or page-wide horizontal scroll |
| WEB-06 | Logout in tab A, mutate/query in tab B, then navigate browser history | Backend denies revoked session; tab B refreshes auth state; cached private data is cleared/hidden at the defined auth transition |
| WEB-07 | Change/reset password while page polls, while key dialog is open and after key reveal | Consistent sign-in transition; no secret retained in obsolete modal/history or automatic replay after reauthentication |
| WEB-08 | Revoke key in another session, refresh key list, attempt stale revoke | UI converges; 404/revoked state handled without showing success for wrong action |
| WEB-09 | Delay/drop auth/config, login, csrf and me responses independently | Loading bounded by normal timeout/error handling; retry actionable; no false setup screen from a temporary config failure |
| WEB-10 | 401 versus 403 versus 429 versus 5xx during ordinary operation | Only authentication failure prompts sign-in; permission/rate/server errors retain useful context; no redirect storm |
| WEB-11 | Expire CSRF and recover via csrf endpoint; run two simultaneous mutations | Client recovery obeys contract; no duplicate successful mutation, infinite retry or cross-session token mixing |
| WEB-12 | Direct `/login`/`/setup` while signed in; logout then reload protected deep link | Correct route redirects with safe internal return destination; no external/open redirect |
| WEB-13 | Stale built UI after server upgrade; removed asset referenced by old HTML | Recoverable refresh/update behavior; no blank screen misdiagnosed as invalid password; asset caching policy recorded |
| WEB-14 | localStorage/sessionStorage unavailable; clipboard rejected; browser privacy settings | UI renders and auth works; optional convenience failures are handled; no fallback that exposes credentials |
| WEB-15 | HTML/script-like strings in display name, key name, provider name, filenames and error text | React/UI renders text safely; no executable markup or unsafe download URL |
| WEB-16 | Inspect URL, referrer, browser console, network logs and stored client state after auth/key operations | Passwords/keys/tokens absent from URLs and diagnostics; one-time key data not persistently cached beyond intended reveal |

## 18. Storage integrity, configuration, and operational boundaries

Execution: isolated database/filesystem tests plus installed artifact/container smoke. P1 by default; unsafe startup, credential loss or corrupted durable data is P0.

| ID | Procedure | Expected result |
| --- | --- | --- |
| DB-01 | Two processes open same migration-0004 database simultaneously | `BEGIN IMMEDIATE` migration coordination applies 0005 exactly once; both processes observe a valid final schema |
| DB-02 | Set schema version newer than candidate; start server and CLI recovery | Actionable refusal without downgrade or destructive writes |
| DB-03 | Inject failure at each statement of migration 0005 | Original version/schema preserved or fully committed migration; no partial table set marked current |
| DB-04 | Run `PRAGMA integrity_check` and `foreign_key_check` after setup/reset/revoke/delete and restore | Healthy integrity and no foreign-key violations; compare against known clean fixtures |
| DB-05 | Assert uniqueness/check constraints for usernames, token hashes, user status and key scope | Invalid direct fixture writes rejected; public service rejects errors cleanly; do not assume DB enforces the service's single-account rule |
| DB-06 | Interrupted password reset between credential/auth-version/session updates | Atomic commit/rollback; no password accepted with unintended old sessions remaining |
| DB-07 | Session creation fails after successful account creation or login verification | Account remains recoverable via subsequent login; error does not falsely claim setup never committed |
| DB-08 | Inventory DB, WAL, cache files and backups for generated secret values | Raw password, API-key secret and session bearer token absent; distinguish intentionally stored CSRF secret and configured encrypted provider secrets |
| DB-09 | Restore backup made before key revocation/password reset | Document that restore reverts revocation state; operator recovery procedure reissues/reset/revokes credentials before exposure as required |
| CFG-01 | Remote-setup flag unset/empty/1/true/yes/0/false/no, mixed case and whitespace | Parsing matches settings contract; false/unrecognized values do not enable setup |
| CFG-02 | Env token unset/empty/whitespace, replace token on restart, account remains present | Empty matches documented unset behavior; whitespace policy explicit; old replaced env token fails while account/keys remain correct |
| CFG-03 | Invalid port/workers/upload/attempt/recheck values, including zero and negatives | Validated or explicitly documented supported values; no unsafe bind/unbounded work from malformed configuration |
| CFG-04 | Command-line host/port overrides env values | Effective bind drives startup gate/setup policy consistently; no mismatch between actual listener and configured permission |
| CFG-05 | Data path relative/absolute, spaces, Unicode, nonexistent parent and symlink | Documented path behavior, useful permissions errors; no accidental fallback to another directory or write outside storage confinement |
| CFG-06 | Unset/wrong secret env prefix, blocked egress target and approved local fixture host | Secret/egress restrictions survive account setup and migration; denied provider test does not leak referenced env values |
| CFG-07 | Start worker/CLI account command against wrong/empty data directory | Clear fixture/operator distinction; no misleading recovery success for a different installation; record effective data directory |
| CFG-08 | Occupied port, absent built UI, missing sqlite-vec/FTS5 capability | Actionable startup/UI error; no process left holding database locks; installed package requirements match advertised support |
| PKG-01 | Install minimum core dependencies without any store/engine extras | Built-in workflow and auth work; absent optional extras fail clearly only when selected; no eager heavy-model imports |
| PKG-02 | Inspect wheel/sdist contents and Docker build context | Required UI/migrations included; no runtime database, local secrets, ignored planning material or test credential artifacts shipped |
| PKG-03 | Compare package/version constant/UI metadata, target tag expectation and MCP compatibility floor | Intended versions consistent; independently versioned MCP documented; release workflow would reject wrong tag before publishing |
| PKG-04 | Stop/recreate container with specified grace period while jobs run | Durable state recoverable; process exits predictably; restart resumes according to job lease policy |

## 19. Integration contract and client failure coverage

Execution: local deterministic HTTP fixtures first, then real service versions specified in the release matrix. P1 unless crossing a trust/scope boundary (P0).

| ID | Procedure | Expected result |
| --- | --- | --- |
| EXT-01 | Each HTTP provider returns 401/403/404/429/500, timeout and connection reset | Error classified correctly; retries bounded according to provider/job contract; no credential embedded in message |
| EXT-02 | Provider redirects to another host, loop, or disallowed address | No secret forwarding or egress bypass; redirect behavior explicit and bounded |
| EXT-03 | Invalid/partial JSON, incorrect protocol version and content type | Controlled compatibility/schema error rather than corrupt document/vector/cache |
| EXT-04 | Provider responds successfully after client timeout/retry | Idempotent job/index semantics or documented duplicate handling; no duplicated logical output |
| EXT-05 | Every supported store: same corpus fixture, filters and deletion after restart | Adapter-independent semantic invariants hold; numeric score scales need not be identical across engines |
| EXT-06 | Two pipelines/corpora share one external store provider; delete/archive one | Namespace/collection isolation prevents cross-pipeline loss or query leakage |
| EXT-07 | External store unavailable at startup, mid-index, mid-query, then restored | No false ready status; clear failure; recoverable retry/rebuild; no silent switch to local store |
| EXT-08 | OCR discovery repeated, duplicate endpoints, stale service, unsupported `/info` protocol | Stable provider identity/status, no duplicates; service reachable is distinct from engine OCR ready |
| EXT-09 | OCR check completes after provider deletion/config change | Stale completion cannot mark wrong provider/config ready; status converges and no orphaned task damages resources |
| CLI-01 | Core CLI commands with missing token, invalid token, server unreachable, malformed URL and timeout | Clear nonzero failure; structured output/error contract preserved; no full traceback/secret echo for expected errors |
| CLI-02 | CLI output consumed as JSON, empty results, Unicode fields and 204 mutation | Valid stable machine-readable output where advertised; no progress/password prompt mixed into JSON |
| CLI-03 | Piped/noninteractive account prompts, interrupted stdin and password mismatch | Controlled failure without hanging indefinitely or creating partial account; interactive passwords stay hidden |
| MCP-01 | Real backend missing/unsupported whoami/version endpoint and malformed response | Specific startup compatibility error; no assumption that missing scope means read permission |
| MCP-02 | Read credential with configured corpus allowlist; search/fetch valid ID from excluded corpus | Excluded corpus not listed/searched/fetched; guessed document/chunk IDs cannot bypass MCP allowlist |
| MCP-03 | Empty, duplicate, missing and renamed corpus allowlist entries | Documented resolution/refresh behavior; explicit error or exclusion; no fallback to all corpora |
| MCP-04 | Full/legacy token default refusal and explicit allow-full override | Default denies; override works only when explicit and does not expand MCP tool catalog into writes |
| MCP-05 | Paginated catalogs and long document/chunk context near output limits | Complete intended catalog resolution; bounded/truncated outputs clearly indicated; no malformed tool response |
| MCP-06 | MCP transport startup/shutdown, client disconnect and cancelled backend request | Connections released; subsequent requests work; no credential in transport/diagnostic output |
| MCP-07 | Revoke/replace backend credential after startup; dependency timeout/429/5xx during tool request | Useful tool error; no cached privileged result used as proof of current authorization; recovery after correct reconfiguration |
| MCP-08 | Backend text includes instructions and HTML/script-like content | Transport preserves content as retrieval data, not executable UI or server instruction; no injected tool/credential operation |

## 20. Executable test recipes for critical scenarios

These recipes define the minimum evidence for high-risk cases. Implement them as tests with assertions at each step, using fixture-private cookies/keys rather than hardcoded credentials.

### REC-01: password change and local recovery (SES-07/08/09)

1. Initialize empty fixture; create account, session A and session B. Assert both can GET `/v1/auth/me`.
2. Create a read key and a full key from A with its CSRF. Record resource counts and a known query result.
3. Change password from A with correct current password/CSRF. Assert old password fails and both old cookies fail protected reads/mutations.
4. Login with new password; assert GET me, GET csrf, and a CSRF-protected mutation succeed. Check persisted session auth_version against current user version.
5. Repeat change/login to catch hardcoded initial-version assumptions. Verify unrelated API keys retain their intended validity.
6. Run CLI reset on the same fixture data directory; assert every prior session fails, new password login creates a usable session, and known resources/query survive.
7. Restart server and repeat new login/protected mutation. Capture only redacted HTTP summaries and credential-free DB assertions.

### REC-02: permission denial has no side effects (SEC/KEY matrix)

1. Create two corpora and ready pipelines with documents, one provider, a dataset/run, and a populated key list. Snapshot logical resource counts/configs and provider fixture call counters.
2. Parameterize every protected route/method with anonymous, read key, invalid bearer and mixed read bearer + valid cookie. Supply otherwise valid bodies and real resource IDs.
3. Assert expected denial, error envelope and absence of private fields. Assert no newly enqueued jobs, outbound provider call, secret change, resource mutation or new key.
4. Repeat authorized control requests with the intended principal and CSRF to prove that denials were not caused by invalid fixtures.
5. Exempt expected authentication bookkeeping (`last_used_at` and explicit security telemetry) from no-write assertions. A denied read-key request may legitimately touch key usage metadata; it must not mutate business resources.

### REC-03: populated current-release restart and restore (OPS-02–04)

Previous-release upgrade and rollback portions of the original journey are not applicable to this first release.

1. Start the candidate with an empty data directory, create the account and read/full keys, and populate providers/secrets, two corpora, uploads, active pipelines, successful query/evaluation and failed/pending jobs.
2. Stop all writers. Capture schema version, logical row counts, content hashes, query result IDs and a full fixture backup, including external-store snapshots where required.
3. Restart the same candidate with the existing data and encryption configuration. Assert database integrity, valid account/key access and closed setup.
4. Complete the pending workflow and repeat known queries/evaluation.
5. Stop/restart the candidate twice. Assert durable resources and authentication remain valid and setup stays closed.
6. Restore the candidate backup into a different data directory (and isolated external-store namespace); repeat authentication/content/query checks with the same candidate artifact.
7. Record restore duration, external-store handling, missing encryption-key behavior and credential-reset implications.

### REC-04: browser account plus machine client (SET/KEY/APP)

1. Open fresh built UI against installed candidate; create account using browser controls without API pre-seeding.
2. Use UI to create read key; verify one-time reveal/copy and safe list view after refresh.
3. Use browser to configure built-ins, upload known text, build/activate pipeline and query it.
4. Start real MCP using read key; list/search/fetch same content. Attempt forbidden API mutation with that key and verify no side effect.
5. Create full key in browser; run supported CLI write/query commands; verify MCP refuses it by default.
6. Revoke read key in browser; verify next MCP backend call fails. Logout and verify cookie replay cannot reopen account/key routes.
7. Repeat browser login and known query after process/container recreation using persisted volume.

### REC-05: index freshness through replacement and failure (DOC/PIPE/RET)

1. Upload document with unique phrase OLD, build ready pipeline and assert query finds OLD.
2. Replace same external ID with phrase NEW while a controlled worker/provider delay is active. Release an older in-flight response after replacement commits.
3. Complete retries; assert one logical active document, current chunks/vectors reflect NEW, and stale OLD results do not reappear after cache clear/restart.
4. Create second pipeline sharing reusable stages; switch active pipeline and verify correct provenance and no duplicate hits.
5. Delete document while querying; complete purge/GC and restart. Assert document cannot be fetched/queried and other corpus/shared fixture content survives.

## 21. Test harness design and quality checks

Implement fixtures and helpers only as needed to exercise behavior. Do not test helper implementation details or duplicate assertions solely to increase counts.

- Backend fixtures own temporary directories, contexts, clocks, local HTTP services and cleanup. Assert test data directories differ from the repository's existing data directory. Mark filesystem fault injection explicitly and constrain it to disposable mounts/files.
- Concurrency tests use barriers/events at known transaction boundaries, independent connections/processes where needed, and bounded completion time. Avoid hoping a race occurs from repeated arbitrary sleeps.
- Use fake time for login windows, expiry and usage throttling. Real-time deadlines remain appropriate for deployment readiness, shutdown and measured performance.
- Real browser tests must not intercept/mock successful auth, resource APIs or provider responses. Component tests can use MSW to exercise specific error states. Keep these result categories distinct.
- Store integration preflight asserts required clients and service capabilities before running tests. A missing prerequisite fails/blocks that job; it cannot convert an advertised integration suite into a green all-skipped run.
- New permission tests use an independently maintained expected table, not imports of `_READ_ALLOWED` as their oracle. Include successful authorized controls and denied-request side-effect checks.
- For each serious bug fixed during testing, add a minimal regression that fails on the faulty revision and passes with the fix where practical. Record this evidence for password/session and permission defects.
- Shuffle order for deterministic backend tests once; run critical auth/browser tests individually and as a suite. Use a recorded random seed for generated boundary/pairwise inputs. A flaky failure remains a defect even if a retry passes.
- Use branch coverage reporting for changed auth/guard/client modules if coverage tooling is added. Review uncovered security decisions and error paths individually. Do not define success by a global coverage percentage; route classification, boundary behavior and state transitions are the coverage gates.
- Sanitize artifacts before storing/exporting them. Do not run secret scans that print matched secrets into public CI output. Prefer assertions using fixture-owned secret values and redacted failure descriptions.

Suggested additional files, beyond section 4: `tests/test_api_validation.py`, `tests/test_auth_state_matrix.py`, `tests/test_document_lifecycle_release.py`, `tests/test_pipeline_reuse_release.py`, `tests/test_release_artifacts.py`, and browser auth/storage/accessibility specs. Reuse existing suites where they already provide a suitable home; these names do not require fragmenting coherent tests.

## 22. Expanded execution tiers and reporting gates

| Tier | When | Required work | Completion evidence |
| --- | --- | --- | --- |
| T0 Static and inventory | First and after route/model/config changes | Lint/types/build/contract; API inventory classification; dependency/environment manifest | No unclassified route or unexplained schema drift |
| T1 Deterministic core | Each relevant fix, then final candidate | All local backend/UI/MCP tests; auth state/route/boundary matrix; deterministic migration/fault/concurrency tests | Per-case results, no unexplained skips, regression evidence for fixed defects |
| T2 Real HTTP/browser/client | Harness ready and after auth/client changes | Existing live acceptance plus fresh-account/auth/key flows, CLI and MCP integration, cross-browser smoke | Sanitized traces/transcripts; independently runnable journeys |
| T3 Deployment and data | Stable candidate | Installed wheel/sdist, Docker/proxy, current-release backup/restore, store/shim/live-engine matrix | Artifact hashes and per-environment OPS/integration results |
| T4 Bounded resilience | Stable candidate with T1/T2 passing | Fault/restart tests, thirty-minute mixed workload, baseline/candidate performance | Metrics, error/lock inventory, integrity checks and recovery outcome |
| T5 Final release decision | All required tiers complete | Repeat impacted suites on final SHA, verify artifact provenance, close ledger and scope decisions | Explicit GO/NO-GO with no unresolved P0/P1 |

Update W2 to include API validation and DB/config auth boundaries; W3 to include browser accessibility/privacy; W4 to include CLI/MCP error and corpus restriction cases; W5 to include package contents and storage integrity; W6 to include shared-store isolation/provider failures; W7 to include freshness races and side-effect assertions.

Add to each result row: execution layer, priority, input variant/seed, expected state transition, actual resource side effects, associated route/method and automated test identifier. Create one summary per feature, environment and tier listing total/pass/fail/blocked/not-run; counts must reconcile to the ledger. Report the number of scenario families separately from generated test invocations so a large parameter count cannot obscure missing workflows.

Stop and triage immediately on authentication bypass, unintended privilege, durable data corruption, or inability to recover the account. Preserve the disposable failing fixture privately and add a focused regression before continuing dependent runs. Continue independent read-only/static checks where useful. Unexpected skips, unsupported assumptions and flaky cases stay visible in the final report.

Scenarios remain **not run** unless covered by recorded execution evidence. The [initial execution report](single-user-auth-release-results.md) records implemented regressions, suite counts, confirmed failures and outstanding coverage. Passing suite totals do not mean all 186 scenario families have been completed.
