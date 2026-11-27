# Single-user authentication testing results

Date: 2026-11-27\
Target: `feature/single-user-auth` at `00db4731ec871ea83f90521b0b32633466531a7f`\
Testing branch: `test/single-user-auth-release`\
Verdict: **NO-GO — the three confirmed defects are corrected locally; remaining release gates are incomplete.**\
Follow-up: [review and fix plan](single-user-auth-release-fix-plan.md).

This report records executed tests, not completion of the entire [testing plan](single-user-auth-release-testing.md). Sections 1–6 preserve the initial testing evidence before production fixes. Section 7 records the subsequent implementation and validation on 2026-11-27. Source, tests, generated types and documentation remain uncommitted; reproduce against the target SHA plus this branch's working-tree changes. The original failing regressions remain active and now pass.

## 1. Environment and scope

Executed on macOS arm64 (Darwin 27.0.0), Python 3.13.15, Node 26.7.0, uv 0.12.10, and Playwright Chromium. CI's Node 22 and Linux/Python matrix have not been reproduced. Docker 29.8.2 is available, but candidate-image and Compose scenarios were not run. Existing application/OCR containers were left untouched.

Backend fixtures use isolated temporary data directories. The installed-wheel smoke runs from a new temporary directory and virtual environment outside the source checkout. Live browser tests start their own backend and fixture parser. Browser setup and all application requests go through real backend APIs; no MSW or browser request interception is used for these journeys. Engine-independent shim tests do not establish live OCR readiness.

## 2. Executed results

Counts below describe separate suites; the 33 targeted baseline auth/security tests are included in the wider backend run and must not be added again.

| Suite/check | Actual result | Limits |
| --- | --- | --- |
| Backend lint, formatting, mypy | Passed | Latest check includes new tests; mypy checks production package per project configuration |
| Existing targeted auth/security baseline | 33 passed | Existing tests missed the password-reset follow-up request defect |
| Final local backend regression | **1,759 passed, 8 failed, 565 skipped, 137 deselected** | Includes 318 newly added test invocations: 310 passed, 8 failed; excludes integration/cloud-marked tests |
| UI ESLint, typecheck, generated API contract and production build | Passed | Existing Vite configuration/large-chunk warnings remain; Node differs from CI |
| UI component tests | 181 passed | Existing component suite; no claim of complete accessibility coverage |
| Live browser acceptance | **15 passed** | Existing 11 journeys plus 4 new authentication/key journeys; fresh browser account setup is also exercised in global setup |
| MCP lint, formatting, mypy | Passed | Existing package source type configuration |
| MCP existing unit/transport tests | 40 passed | Real-backend contract module skipped in package-only environment, then explicitly executed with backend installed |
| Real HTTP MCP/backend contract | **3 passed, 3 failed** | Read-key startup/catalog, full-token default refusal/explicit override, and key revocation pass; search/fetch fail |
| Shimkit | 160 passed | Isolated engine-independent environment |
| MinerU shim | 127 passed | Live-engine tests excluded |
| PaddleOCR shim | 93 passed | Live-engine tests excluded |
| Surya shim | 70 passed | Live-engine tests excluded |
| EasyOCR shim | 30 passed | Live-engine tests excluded |
| docTR shim | 28 passed | Live-engine tests excluded |
| Docling shim | 39 passed, 6 skipped | `docling-core` installed; tests requiring full `docling` local engine skipped |
| Exact wheel build/install/live smoke | Passed | UI/migration inclusion, clean installed import/CLI, setup/login/logout, upload, FTS pipeline and query; recovery is separately failing |

The final backend run emitted 29 existing approximate-filter contract warnings. Its 565 skips include unavailable optional clients (Chroma, Milvus, Weaviate, Elasticsearch, OpenSearch, LanceDB, MongoDB, Pinecone and Redis) and capability-specific contract skips. They are not evidence that those integrations pass. The baseline run showed the same skip count. Service-dependent/cloud tests remain unexecuted in this batch.

## 3. Confirmed defects

### AUTH-01 — Fresh sessions are invalid after changing or resetting the password

Priority: P0, account recovery blocker. Plan: SES-07, SES-08, SES-09, REC-01.

Reproduction:

1. Create an account and sign in.
2. Change the password through `/v1/auth/password`, or run `ragcentre account reset-password` against that data directory.
3. Sign in with the new password. Login returns 200 and issues a cookie.
4. GET `/v1/auth/me` with that new cookie returns **401** instead of 200.

The reset increments `users.auth_version`, but `Accounts.create_session()` inserts a literal `0` into every new session's auth_version. `session_for()` rejects the mismatch immediately. The local reset command therefore does not restore usable browser access once a password has been changed. Old-session revocation itself works, and independent API keys remain usable in the multi-session regression before the new-login assertion fails.

Source: [account service](../src/ragcentre/accounts/service.py), `reset_password`, `create_session`, `session_for`.

Failing regressions in [test_account_lifecycle_release.py](../tests/test_account_lifecycle_release.py):

- `test_new_sessions_work_after_repeated_password_changes`
- `test_cli_reset_allows_new_browser_session`
- `test_password_change_revokes_both_sessions_but_keeps_keys`

Required correction: issue sessions with the current account auth_version and verify credential-reset/login race semantics. Retest repeated password changes, both old sessions, local recovery, protected mutations with new sessions and restart persistence. Production fix not applied.

### AUTH-02 — Supplemental origin validation accepts mismatched scheme/port and null origin

Priority: P1 security contract gap. Plan: SEC-05.

With a valid session and valid session-bound CSRF header, POST `/v1/corpora` accepts `Origin: https://testserver`, `Origin: http://testserver:9000`, and `Origin: null` when the request origin is `http://testserver`. Each request returns **201 and creates a corpus**; the intended strict-origin test expects 403 and no creation. A foreign hostname is correctly denied.

`csrf_valid()` compares hostname only and ignores a parsed empty hostname. This demonstrates incomplete supplemental origin checking. It is **not proof of a standalone CSRF attack without the session-bound secret**; missing/incorrect-CSRF tests pass. Confirm the deployment's intended origin policy, then enforce and test that policy for scheme, host, effective port, null/malformed origins and trusted proxy headers. Do not resolve the test by silently weakening the expected security policy.

Source: [principal.py](../src/ragcentre/api/principal.py), `csrf_valid`. Three failing parameter variants in `test_session_mutation_rejects_foreign_origin`. Follow-up direct-helper probes also confirmed that a matching bracketed IPv6 origin is rejected and malformed `http://[` origin raises an unhandled ValueError. Those two probes are not yet formal regressions or full HTTP/proxy verification; recorded suite counts above are unchanged. Production fix not applied.

### MCP-01 — Shipped MCP search/fetch contract does not match the backend

Priority: P1 supported-client blocker. Plan: KEY-11, APP-05, MCP/CLI interoperability gate.

A real local backend fixture creates a corpus, uploads a document, builds an FTS pipeline, and proves a normal API query returns results before testing the MCP client. Read-key MCP startup/catalog resolution works, but:

- `RagcentreApi.query()` includes `max_chars`; backend `QueryRequest` rejects that extra field, returning **422 `validation_error`**.
- GET `/v1/documents/{document_id}/outline` returns **404 `not_found`** because no route is registered.
- GET `/v1/chunks/{chunk_id}` returns **404 `not_found`** because no route is registered.

The read-key middleware allowlist mentions the fetch paths, but an allowlist entry does not implement a route. This mismatch is also caught by two backend route-contract assertions. Existing mock-based MCP tests pass because their fake API supplies the contract the real backend lacks. Follow-up review confirmed these mismatches also exist on the current local `main`: its MCP client sends max_chars, its backend request model lacks it, and its document router lacks both fetch endpoints. They are pre-existing integration gaps that still prevent signing off this candidate's advertised client integration.

Sources: [MCP API client](../packages/ragcentre-mcp/ragcentre_mcp/api.py), [backend API models](../src/ragcentre/models/api.py), [document router](../src/ragcentre/api/routers/documents.py).

Failing contract tests: `test_real_backend_accepts_mcp_query_payload`, `test_real_backend_supports_document_outline`, `test_real_backend_supports_chunk_fetch` in [test_backend_contract.py](../packages/ragcentre-mcp/tests/test_backend_contract.py). Required correction: align supported query and fetch contracts, then exercise actual MCP search/fetch tools and corpus restrictions. Production fix not applied.

## 4. Added tests and harness changes

| File | Coverage/result |
| --- | --- |
| `tests/test_account_lifecycle_release.py` | 37 tests: 31 pass, 6 fail; username/password boundaries, forbidden setup fields, password changes/local reset, multi-session logout, CSRF side effects/origins |
| `tests/test_auth_route_matrix_release.py` | 242 tests: 240 pass, 2 fail; explicit classification of all 60 current OpenAPI operations, protected docs GET/HEAD, anonymous/invalid/read/mixed credentials, full/legacy denial on session-only routes, resource/key-state snapshots, missing MCP fetch routes |
| `tests/test_account_migration_release.py` | 12 pass; migration 4→5 with existing corpus preservation, every statement-boundary rollback, independent-connection concurrent migration, newer-schema refusal, SQLite backup/restore of account/session/key integrity |
| `tests/test_auth_populated_release.py` | 27 pass; real parsed/indexed document reads and read-key search, stale/expired/disabled/revoked sessions, HTTP/HTTPS cookie attributes and remote-setup flag parsing |
| `packages/ragcentre-mcp/tests/test_backend_contract.py` | 6 real-HTTP tests: 3 pass, 3 fail; starts isolated Uvicorn backend, builds/query-checks fixture, tests actual MCP client |
| `ui/e2e/live/global-setup.ts` | Creates first account with browser controls, verifies connected shell, saves session state and a fixture-only full key |
| `ui/playwright.config.ts`, `ui/e2e/live/api.ts` | Each browser test gets an authenticated independent context; Node fixture seeding/polling uses dedicated full key; credentials stored in mode-0600 files under temporary fixture directory |
| `ui/e2e/live/acceptance.spec.ts` | Existing direct session mutation supplies CSRF so immutable-config behavior is tested after authentication |
| `ui/e2e/live/auth.spec.ts` | 4 pass: deep-link login return, isolated logout, one-time read-key UI/revocation, setup closure/account refresh; secret-reveal tests disable trace capture |
| `tests/release_artifact_smoke.py` | Reusable exact-wheel smoke runner; installs outside source checkout, starts/stops isolated server, exercises built UI and core flow |

The first live baseline failed because its original tests entered the shell without completing setup. Updating test setup resolved that harness mismatch; the existing application journeys then passed. A transient 422 in the new browser fixture was corrected by submitting only username/password rather than the fixture's extra key field. Neither was counted as a product defect.

## 5. Reproduction and evidence

From repository root:

```sh
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest -m 'not integration and not cloud' -q --tb=short
uv run pytest tests/test_account_lifecycle_release.py tests/test_auth_route_matrix_release.py tests/test_account_migration_release.py tests/test_auth_populated_release.py -q --tb=short
(cd ui && npm run lint && npm run typecheck && npm test && npm run check:contract && npm run build && npm run e2e)
(cd packages/ragcentre-mcp && uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest -m 'not contract')
uv run --directory packages/ragcentre-mcp --with ../.. pytest -m contract -q --tb=short
uv build --out-dir .pytest_cache/release-dist
uv run python tests/release_artifact_smoke.py .pytest_cache/release-dist/ragcentre-0.0.4-py3-none-any.whl
```

For each of shimkit, mineru_shim, paddleocr_shim, surya_shim, easyocr_shim and doctr_shim, run from its `examples/<name>` directory:

```sh
uv run --isolated --no-project --with ../shimkit --with fastapi --with python-multipart --with httpx --with pytest --with pillow --with pypdfium2 --with numpy pytest -q tests --ignore=tests/test_live.py
```

For Docling, run from `examples/docling_shim`:

```sh
uv run --isolated --no-project --with ../shimkit --with docling-core --with fastapi --with python-multipart --with httpx --with pytest pytest -q tests
```

Local ignored evidence: `.pytest_cache/release-results/backend.log`, `.pytest_cache/release-results/backend.xml`, `.pytest_cache/release-dist/`. Playwright's `ui/test-results/` is ignored. Raw records can contain sensitive fixture state and are not included in versioned documentation. Failed product assertions are intentionally not marked xfail or disabled.

Exact tested wheel: `ragcentre-0.0.4-py3-none-any.whl`. SHA-256:

```text
1515620338cd4a529ce32761b6aae5aacc29073b0c0ff0eb19de75c86eaeb862
```

The build generated wheel and sdist; `uv build` built the wheel from the sdist. This does not replace the remaining independent platform/container/complete recovery smoke checks. Rebuilding may produce a different hash; record and test the actual artifact proposed for release.

## 6. Remaining gates and next execution order

1. Correct AUTH-01, confirm/correct AUTH-02's origin policy, and align MCP-01's backend/client contracts. Repeat failing tests, then REC-01 and real MCP tool search/fetch. Product fixes are outside this testing-only change set.
2. Complete session race/expiry boundaries, login-limiter concurrency/resource limits, setup races, remaining API validation, document replacement/deletion freshness races, accessibility and client-cache/privacy cases. The successful route-denial matrix does not prove every lifecycle or proxy scenario.
3. Run Linux/Python 3.11/3.13/default matrix, CI Node 22, Firefox/WebKit, native wheels on required platforms, candidate Docker image/Compose modes and HTTPS proxy.
4. **First-release scope correction (owner decision, 2026-11-27):** no version has been released, so previous-version upgrade, migration-compatibility and rollback rehearsals are not applicable. Current-release backup/restore remains a separate data-recovery check, covering blobs, vector stores, encrypted secrets, jobs and evaluations where supported.
5. Provision isolated optional-store clients/services and run every advertised adapter; execute live OCR for the declared engine/hardware scope. Missing prerequisites remain blocked/unexecuted, never green.
6. After fixes and stable core journeys, run bounded fault injection, thirty-minute mixed workload and measured performance comparison. No load/soak conclusion is available from this batch.
7. Build final artifacts from the final candidate SHA, rerun release gates, reconcile the case ledger and record explicit sign-off before any tag/publication.

At the end of the initial batch, account recovery failure stopped dependent release sign-off. Independent checks in that batch completed and their processes exited. No production source fix had been applied at that checkpoint. The implementation below follows that historical result.

## 7. Fix implementation and local retest — 2026-11-27

The three confirmed defects are corrected on `test/single-user-auth-release`. No schema migration is required. No commit, push, tag, PR or release publication has been performed.

### Implemented behavior

- **AUTH-01 / F1:** login reads credentials and account version in one snapshot, verifies outside a transaction, and requires the verified version and active account inside the session-insertion transaction. Fresh sessions work after repeated password changes and local reset. Browser password replacement also checks the verified version before updating, so a stale password change cannot overwrite a completed operator reset. Existing sessions are revoked atomically; API keys keep their independent lifetime.
- **AUTH-02 / F2:** cookie mutations compare a single valid HTTP(S) Origin with the effective ASGI scheme, Host and normalized port. Matching IPv6 and default ports work; foreign schemes/ports, null/empty/malformed origins and duplicate authority headers fail safely. Missing Origin still requires valid CSRF. Forwarding is handled by trusted Uvicorn proxy middleware; raw forwarding headers do not change the origin check. README now documents Host preservation and `FORWARDED_ALLOW_IPS`.
- **MCP-01 / F3:** queries accept `max_chars >= 200`, return ordinals and truthful truncation, and apply a response-only Unicode passage budget. Chunk windows are bounded to ten neighbours per direction and the current document revision/stage. Outlines use the active or explicitly selected same-corpus pipeline and return at most 200 sections with a full chunk count. The minimum backend version is 0.0.4. Outline `page_count` is null because source-page totals are not reliably stored; chunk-by-ID `enrichment` is null because multiple enrichment stages can share a chunk ID. Retained inactive stages remain readable only for the current revision, until garbage collection.
- **F4:** real MCP tool/stdio contracts now cover search, both fetch paths, corpus restrictions, key failure/revocation, output limits and replacement/deletion. A dedicated Python 3.11/3.13 CI job installs the current backend as editable and rejects zero tests or skipped contracts. The browser recovery journey uses its own backend/account. The exact-wheel smoke exercises password change, installed CLI recovery and the new APIs outside the checkout.

### Executed follow-up results

| Suite/check | Actual result | Limits |
| --- | --- | --- |
| Full local backend, `not integration and not cloud` | **1,854 passed, 0 failed, 565 skipped, 137 deselected; 29 warnings** | Same macOS/Python environment and optional-integration limitations as the initial batch |
| Backend lint, formatting, mypy; patch whitespace | Passed | 269 Python files formatted; mypy checks 137 source files |
| UI ESLint, TypeScript, component tests, production build | Passed; **181 component tests** | Node 26.7.0; existing Vite config and bundle-size warnings remain |
| Generated API schema freshness | Passed by regeneration and byte-for-byte comparison; TypeScript contract checks compile | `npm run check:contract` reaches its expected git-diff failure against the old committed schema because the generated API change is intentionally uncommitted; it is not recorded as a passing command |
| Live browser acceptance | **16 passed** | Chromium; includes the existing journeys plus isolated password-change/CLI recovery with protected reads and writes |
| MCP with editable backend installed | **57 passed, zero skips: 43 unit/transport + 14 real-backend contracts** | Local Python 3.13; configured CI matrix has not been run remotely |
| MCP lint, formatting, mypy | Passed | 13 package/test files formatted; 7 typed source files |
| New wheel/sdist build and exact installed-wheel smoke | Passed | Clean external virtualenv, built UI/migration inclusion, CLI/import, auth/recovery, upload/query and agent APIs |

The backend JUnit contains 2,419 cases: 1,854 passes and 565 skips, with zero errors/failures. Focused reruns overlap this full suite and are not added to the total. The same 565 optional/client/capability skips remain; no external-store or live-OCR readiness is inferred. Historical parser-shim results were not rerun because these fixes do not modify shim code.

The new browser journey initially exposed fixture timing and duplicate-name errors: it filled password fields before account loading settled, then reused a corpus name on the second protected-write check. The fixture now waits for loaded account identity and uses distinct names. The final full 16-test run passes. A timed-out fixture process was stopped, and no test backend was left running.

### Reproduction and artifact identity

```sh
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest -m 'not integration and not cloud' -q --tb=short --junitxml=.pytest_cache/release-results/backend-fixed.xml
uv run --directory packages/ragcentre-mcp --with-editable ../.. pytest -q --tb=short --junitxml=../../.pytest_cache/release-results/mcp-fixed.xml
(cd ui && npm run lint && npm run typecheck && npm test && npm run build && npm run e2e)
uv build --out-dir .pytest_cache/release-dist-fixed
uv run python tests/release_artifact_smoke.py .pytest_cache/release-dist-fixed/ragcentre-0.0.4-py3-none-any.whl
```

Generated schema stability was checked by saving `ui/src/api/generated/openapi.ts` bytes, running `node scripts/gen-api-types.mjs` from `ui`, and asserting identical bytes afterward. The normal git-diff freshness gate will compare correctly once this intended generated change is included in a commit; no index staging or gate weakening was used.

Follow-up evidence: `.pytest_cache/release-results/backend-fixed.log`, `backend-fixed.xml`, `mcp-fixed.xml`, `.pytest_cache/release-dist-fixed/`, and ignored Playwright results. The final wheel smoke returned this tested wheel SHA-256:

```text
6041e9fa9bf62680fc2b7c9bfe97125235e16461e8fdb9444972c0972f0b60f5
```

This hash supersedes the earlier wheel only for this local working-tree candidate. The source remains uncommitted, so a final release SHA/artifact still needs to be fixed and retested before publication.

### Release decision after fixes

**NO-GO for publication.** The initial 11 failing invocations now pass, but the wider plan is not complete. Trusted/untrusted proxy behavior is verified through real ASGI proxy middleware in-process; an actual TLS-terminating deployment has not been rehearsed. Linux/Python/Node 22, Firefox/WebKit, candidate Docker/Compose, current-release backup/restore, supported external stores, live OCR and bounded fault/soak/performance gates remain pending. The configured CI jobs also need to execute on the final committed candidate.

**First-release scope decision (2026-11-27):** the owner confirmed that no version has been released. Previous-release upgrade, migration-compatibility and rollback rehearsals are therefore **not applicable**, rather than pending or passed. Fresh database initialization, persistence, password recovery and current-release backup/restore remain applicable. Existing synthetic migration test results are retained as engineering evidence, not as a requirement to support an unreleased predecessor.
