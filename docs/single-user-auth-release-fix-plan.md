# Single-user authentication release fix plan

Date: 2026-11-27\
Target: `feature/single-user-auth` at `00db4731ec871ea83f90521b0b32633466531a7f`\
Working branch: `test/single-user-auth-release`\
Status: F1–F4 implemented and locally validated on 2026-11-27; wider release gates remain pending. See [follow-up results](single-user-auth-release-results.md#7-fix-implementation-and-local-retest--2026-11-27).\
Release decision: **NO-GO** until the defects and remaining release gates are resolved.

References: [test results](single-user-auth-release-results.md), [testing plan](single-user-auth-release-testing.md).

## 1. Review conclusions

The recorded backend JUnit agrees with the report: 1,759 passed, 8 failed and 565 skipped; the original command also deselected 137 integration/cloud cases. The six real-backend MCP tests recorded three passes and three failures. The 11 failing invocations represent overlapping checks of three workstreams, not 11 unrelated defects.

| Workstream | Evidence and assessment | Priority |
| --- | --- | --- |
| F1 Session issuance and recovery | Three backend failures prove new sessions cannot be used after password change/reset. Literal session auth_version `0` conflicts with the incremented account version. | P0; fix first |
| F2 Origin validation | Three backend failures prove hostname-only checks admit different schemes/ports and null origin when CSRF is otherwise valid. Exact-Origin checking was specified in the auth implementation plan. Additional direct-helper probes reject matching IPv6 and raise ValueError for malformed bracketed origin. | P1; fix before security/deployment sign-off |
| F3 Agent-facing API completion | Three real MCP failures prove search 422 and both fetch endpoints 404; two backend route assertions duplicate the missing-fetch evidence. The original agent-access plan specifies the missing API features. | P1; required supported-client functionality |
| F4 Regression integration and evidence quality | Browser setup harness now works, but password recovery is not covered in browser tests, actual MCP tools are not exercised against the real backend, and current CI skips the opt-in backend contract without installing the backend. | P1; prevents recurrence and false green CI |

The MCP mismatch also exists on the current local `main`: its client sends `max_chars`, its query request has no such field, and its document router lacks both fetch routes. Treat this as a pre-existing integration gap that still blocks the release scope. Do not attribute it to the account change.

The origin results establish a supplemental validation defect. They do not establish unauthorized mutation without possession of the session CSRF secret; missing/wrong-token tests pass. Keep that distinction in release notes and severity descriptions.

At review time, IPv6 and malformed-origin findings were direct-helper probes only. The subsequent implementation added formal helper and HTTP regressions plus trusted/untrusted ASGI proxy-middleware tests. Actual TLS proxy deployment remains a separate pending release gate.

## 2. F1 — Make session issuance consistent with verified credentials

Primary files: [account service](../src/ragcentre/accounts/service.py), [auth routes](../src/ragcentre/api/routers/auth.py), [local recovery CLI](../src/ragcentre/cli.py).

### Intended behavior

A successful login must issue a usable session for the credential version that was actually verified. Password changes and local resets invalidate all earlier sessions. A login already verifying an old password cannot acquire the new credential version after a reset commits. API keys and the legacy operator token retain their existing independent lifetime.

### Implementation

1. Read account status/version and password credential from one consistent database snapshot during login. Return a typed verified-account value containing the user identity and verified auth_version. Do Argon2 verification outside the transaction; do not hold a database read/write transaction while hashing.
2. Replace the hardcoded `0` in session creation with the verified account version. Require the expected version at the session-issuance boundary; use a short write transaction to re-read active status/current version and reject if it no longer matches. This can be an explicit `expected_auth_version` argument or an internal verified-account object; do not leave the login route using an unchecked user ID-only path.
3. Reuse this checked issuance path for initial setup. Include the initial version in the account-creation return value or obtain the setup account snapshot explicitly. Update direct fixture/service callers in the migration, route-matrix and populated-data tests.
4. Only set a session cookie after successful version/status revalidation and committed session insertion. Convert version/status races into the existing generic credential error; do not return a successful AuthSession for a session already known invalid at issuance.
5. Preserve `session_for`'s per-request status/version checks. A reset that commits after session insertion must still delete/invalidate that session. A request authenticated before the reset may be in flight; do not claim cancellation of already executing work.
6. Review password-change verification and write as the same boundary: verify a snapshot outside the transaction, then condition the password replacement on its expected credential/account version and active status. Otherwise a request using an old verified current password can overwrite a concurrent operator reset. Share the transactional update helper with CLI reset, with explicit operator-reset semantics rather than requiring the operator to know the old password.
7. Keep password update, auth-version increment and session deletion atomic. Do not reset user versions to zero or remove the version check to make the regressions pass. The existing schema supports the fix; no migration or manual DB repair should be required.

Reading the current account version only at insertion would fix the simple lockout but introduce a stale-verification race: a login verified before reset could receive a session tagged with the post-reset version. The expected-version comparison is part of the fix, not optional hardening.

### Validation gate

- All three existing F1 regressions pass; repeat two password changes and CLI recovery.
- After each reset, old password and both old cookies fail, while new login can GET me/csrf and perform a protected mutation.
- Add barrier-controlled races: login verifies → reset commits → login attempts issuance; session inserts → reset commits; account disabled between verification and issuance. Assert fail-closed behavior without timing sleeps.
- Race user password change against CLI reset; the stale user change cannot overwrite the operator reset after it commits.
- Reopen the context/server after recovery and repeat new login; existing API keys remain valid unless separately revoked.
- Add a real browser password-change/relogin journey using an isolated account/server so it cannot revoke the shared acceptance suite's stored session.

## 3. F2 — Validate exact origins without breaking IPv6 or proxies

Primary files: [principal resolution](../src/ragcentre/api/principal.py), [RequestGuard](../src/ragcentre/api/app.py), origin/session tests, deployment documentation.

### Policy to implement

For cookie-authenticated state-changing API requests, require the session-bound CSRF header. If Origin is present, it must be a valid HTTP(S) origin equal to the externally visible request origin after trusted proxy processing. Compare normalized scheme, hostname and effective port. Missing Origin remains acceptable with a valid CSRF token, preserving non-browser/session test clients; a present empty, null, opaque or malformed origin is rejected. Bearer clients retain their existing CSRF exemption and bearer precedence.

This applies to the existing guarded session mutations. Public login/setup Origin policy is a separate test-plan case; add focused checks for their intended behavior rather than assuming this helper change protects public routes.

### Implementation

1. Replace `_hostname` and manual `Host.rsplit` with a small strict origin parser. Normalize scheme/hostname and default ports (HTTP 80, HTTPS 443); correctly parse bracketed IPv6. Catch parsing/port errors and return invalid rather than raising.
2. Reject unexpected userinfo, paths, query/fragment, invalid/multiple origin values, null/opaque origins and invalid ports. Compare `http://example:80` with `http://example` as equal, but do not equate localhost, IPv4 and IPv6 host aliases.
3. Pass the effective request scheme from ASGI scope into origin validation. Use the actual request Host/authority, not the configured listener address, so a public HTTPS domain can proxy to an internal loopback/container bind.
4. Honor forwarding only through the server's trusted proxy processing. Do not independently trust arbitrary `X-Forwarded-Proto`, `X-Forwarded-Host` or `Forwarded` headers in the CSRF helper. Verify the proxy preserves the external Host and conveys HTTPS scheme from a trusted peer.
5. Preserve constant-time CSRF-secret comparison and existing 403 `csrf_invalid` behavior. Invalid origins must not reach resource mutations or cause 500. Avoid introducing a permissive wildcard trusted-origin setting to make proxy tests pass.
6. Document the supported single-origin reverse-proxy setup and trusted-peer configuration. If an actual supported deployment cannot preserve its external origin through these mechanisms, introduce an explicit exact trusted-origin configuration with validation/tests; do not infer it from untrusted headers.

### Validation gate

Add passing same-origin controls as well as rejection tests:

- Matching HTTP/HTTPS, custom ports, normalized default ports and case-normalized domain.
- Matching IPv4, localhost and bracketed IPv6, with/without explicit ports.
- Foreign host, different scheme, different non-default port, null, empty, malformed brackets, invalid port, userinfo and multiple origin values.
- Valid CSRF with missing Origin succeeds; missing/wrong/other-session CSRF fails even for matching Origin.
- Full/read bearer behavior and invalid-bearer-plus-cookie precedence remain unchanged.
- Exercise the helper and real HTTP path; bad Origin returns a controlled error, not an unhandled parser exception.
- Run HTTPS reverse-proxy setup/login/query/password/key flows and confirm Secure cookies and strict Origin comparison together. Test spoofed forwarding from an untrusted peer.

## 4. F3 — Complete the backend contract consumed by MCP

Primary files: [API models](../src/ragcentre/models/api.py), [query route](../src/ragcentre/api/routers/query.py), [document routes](../src/ragcentre/api/routers/documents.py), [retrieval assembly](../src/ragcentre/retrieval/engine.py), [MCP client/models](../packages/ragcentre-mcp/ragcentre_mcp/api.py), and UI generated API types.

Implement the agreed agent-facing API rather than merely removing the rejected query field or replacing document fetch with an unbounded full-document download. The original [agent-access specification](plans/agent-access.md) defines chunk windows, bounded document outlines, ordinals and shared server-side passage budgets. Keep MCP independent of the backend package at runtime.

### F3a. Query budget and ordinal

1. Add optional `QueryRequest.max_chars` with the specified minimum of 200. Absence keeps existing retrieval result selection/text unchanged. Add `QueryResult.ordinal` from the existing chunk ordinal and `QueryResponse.truncated` (false when no content was cut).
2. Define the budget precisely in API documentation and tests: count Unicode characters in returned passage content (`text`, `context`, `enrichment`, and before/after passage strings, counting repeated transmitted text as transmitted). It is a passage-text budget, not a bound on total JSON bytes, metadata or query-transform probe text.
3. Apply one shared deterministic response-budget helper after retrieval/reranking, before API serialization. Remove neighbour/context/enrichment content before main result text; then drop lower-ranked hits. If the first hit alone exceeds the budget, clip its text within budget and indicate truncation. Count any truncation marker within the limit. Preserve result ordering, IDs, scores and provenance.
4. Keep budget shaping out of the retrieval ranking/index/cache computation and evaluation metrics. An MCP output preference must not change which candidates the engine ranks or poison caches with truncated stored content. Preserve the existing transform/debug semantics.
5. Update strict response-shape expectations, UI contract checks/generated types and MCP fake responses where appropriate. Keep MCP's own `_fit_hits`/`_fit_window` as output protection; server budgeting provides consistent API behavior for all clients.

Tests: omitted budget, minimum 200, invalid 199/negative, exact-fit, no results, first oversized chunk, multiple hits, large neighbours/context/enrichment, multibyte text, truthful truncated flag, top-k/provenance preservation, ordinal parity with get_chunks, and transformed/plain query parity. Assert the declared content sum never exceeds the requested limit. Ensure existing evaluation results do not change when max_chars is absent.

### F3b. Chunk fetch with bounded neighbours

Add `GET /v1/chunks/{chunk_id}?before=0&after=0`, returning `{chunk, before, after, document}` in the shape already consumed by MCP. Bound before/after to 0–10 each; default zero for API compatibility with the design, while MCP can request its existing default two.

- Chunk IDs are globally primary-keyed and generated from chunk fingerprint + document ID + ordinal, so a separate pipeline parameter is unnecessary for lookup. IDs are stable across replacement at the same stage/ordinal; do not promise revision snapshots from the ID alone.
- Resolve chunk/document in a consistent read with document locking where existing lifecycle helpers require it. Require active document status and current `doc_revision`. Return 404 for missing, deleting or stale-revision chunks.
- Restrict neighbours to the same document, chunk_node_fp and current revision; sort in ordinal order. Never join neighbours by ordinal alone or mix cloned pipeline stages/revisions.
- Return document ID and corpus ID alongside safe filename/external ID/metadata. MCP needs corpus ID to enforce its configured corpus visibility. Keep credentials/provider configuration out of the response.
- Use bounded SQL neighbour queries; do not materialize every document chunk to return a window. Clarify retained non-active-pipeline chunk behavior and keep it consistent with explicit pipeline search and retention policy.

Tests: first/middle/last chunk, before/after 0 and 10, invalid limits, unknown IDs, repeated ordinals in another document/stage, cloned pipeline sharing, current versus stale revision, deletion/replacement races, and unchanged permission boundaries.

### F3c. Bounded document outline

Add `GET /v1/documents/{document_id}/outline?pipeline_id=...`. Default to the document corpus's active pipeline; an explicit pipeline must belong to that corpus. Reuse current chunk/enrichment stage selection rules from `get_chunks` rather than introducing a second interpretation.

- Add typed response models compatible with MCP: document identity/corpus metadata, full chunk count, page count where available, sections and truncated flag.
- Group consecutive identical heading paths, preserving document order. For headingless content, provide bounded page/roughly-ten-chunk groups rather than one entry per chunk or a whole-text response.
- Each section has heading_path, first_chunk_id, page_start/page_end and chunk_count. Return at most 200 sections; query enough to detect a 201st and set truncated truthfully. Total chunk count remains the full selected-stage count.
- Compute section metadata from current chunk rows, with bounded SQL/window aggregation. Do not load chunk bodies or reparsed whole documents just to produce an outline. Page count may be null when source data cannot determine it reliably.
- Active but not-yet-chunked documents return the documented empty outline (zero chunks/sections); no active pipeline returns a controlled not-found error naming the missing selection. Missing/deleting documents and foreign-corpus pipeline selection are rejected. Establish these semantics in tests and docs before coding the route.

Tests: multiple heading levels, nonconsecutive repeated heading paths, no headings/pages, empty/pending result, cap boundaries 199/200/201, explicit/active pipeline selection, ambiguous chunk/enrich stage, changed revision/deleted document, and parity with get_chunks.

### F3d. Expose and verify the contract

- Add both endpoints to the explicit test permission oracle as read-accessible operations. Middleware already lists their paths, but the new operations must have positive populated-data controls plus anonymous/read/full/session denial/allowance coverage.
- Update generated UI OpenAPI types and contract assertions. Additive response fields still require updating tests that assert exact dictionaries.
- Extend real-backend MCP coverage from RagcentreApi methods to actual `list_corpora`, `search` and both fetch tool paths using the MCP client, with two corpora and an allowlist. Out-of-allowlist chunk/document IDs must remain not found at the tool boundary.
- Cover hidden-corpus direct IDs, invalid/revoked keys, full-key default refusal/explicit override, output truncation and stale IDs after replacement/deletion. Assert the MCP tool catalog stays read-only.
- Set `MIN_SERVER_VERSION` to the first released server version that actually contains this API contract. The current 0.0.3 claim is insufficient. Use the actual release version selected by the project; do not arbitrarily bump unrelated package versions. Test old-version refusal and supported-version acceptance.
- Correct stale diagnostics suggesting `RAGCENTRE_READ_TOKEN`; this release creates named read API keys through Account and supplies them to MCP as `RAGCENTRE_API_TOKEN`. Keep operator credential and read-key instructions distinct.

## 5. F4 — Make the new coverage a required regression gate

Primary files: [.github/workflows/ci.yml](../.github/workflows/ci.yml), [MCP contract tests](../packages/ragcentre-mcp/tests/test_backend_contract.py), [route matrix](../tests/test_auth_route_matrix_release.py), [browser tests](../ui/e2e/live/auth.spec.ts), release smoke runner and test fixtures.

1. Add an explicit CI real-backend contract job, or a separate step with equivalent dependency isolation. Install current checkout's backend as editable test-only dependency in the MCP environment and run the contract marker. Ordinary MCP package isolation/unit tests remain separate.
2. Preflight `import ragcentre` before contract collection and require the contract tests to be collected/executed. A skipped module because backend is absent is not a green contract job. Keep backend as a test-only dependency, not a runtime dependency of ragcentre-mcp.
3. Prefer `--with-editable ../..` for source-contract tests, or explicitly refresh the local backend package after edits. A previously cached local wheel from `--with ../..` must not make tests exercise stale source. Installed-artifact tests separately verify the exact built wheel.
4. Strengthen denial fixtures: populate real corpora/documents/providers/jobs and send valid target IDs/request bodies for representative mutations. Current broad route matrix correctly checks middleware status precedence, but mostly empty state/missing IDs/invalid bodies cannot establish all advertised side-effect guarantees. Include task/evaluation/index/secret state and outbound-call counters where relevant, exempting only intended auth usage bookkeeping.
5. Add separate password-change/reset browser fixtures with their own server/data directory/session lifecycle. The normal Playwright contexts copy one setup session cookie; they are independent contexts, not independently issued backend sessions. Do not revoke that shared cookie while other acceptance tests depend on it.
6. Ensure one failing global browser setup records a distinct setup failure; do not classify all acceptance cases as having run. Disable tracing around password/key reveal where secrets would be captured; retain sanitized diagnostics for lifecycle failures.
7. Pin/document CI Node 22 and supported Python/browser environments in the execution record. After API additions, require UI contract regeneration/typecheck/build and full browser acceptance on the same candidate.
8. Extend installed-wheel smoke with post-password-change and CLI-recovery login plus query/fetch contracts where supported. Build after the UI and test from outside the checkout, recording the new wheel hash. Do not reuse the prior tested wheel hash after product changes.

### Coverage limitations to keep visible

The owner confirmed on 2026-11-27 that no version has been released. Previous-release upgrade, migration-compatibility and rollback rehearsals are not applicable to this first release and do not block publication. Existing synthetic migration tests remain useful atomicity/coordination evidence. Fresh database initialization, persistence and account recovery remain applicable; current-release backup/restore is a separate data-recovery check, and the SQLite account restore test does not establish complete deployment backup recovery.

The 565 backend skips combine missing dependencies with valid capability-specific skips. Inventory them by reason; provision supported clients/services and fail service-required jobs on missing prerequisites. Parser-shim unit results similarly do not replace live OCR-engine/hardware checks.

## 6. Implementation sequence and validation checkpoints

These are proposed cohesive changes, not commits already made. Keep the existing failing tests active; fix production behavior and expand assertions rather than marking them xfail or relaxing the contract.

| Order | Change set | Dependency | Exit condition |
| --- | --- | --- | --- |
| 1 | F1 session-version/race-safe recovery plus focused regressions | None | New login works after repeated reset/change; old verified credentials cannot cross reset boundary |
| 2 | F2 strict Origin parsing, effective request origin and IPv6/proxy tests | None; full password-change proxy journey follows F1 | Existing three origin failures pass; IPv6 valid origin passes; malformed inputs fail safely |
| 3 | F3a query budgeting/ordinals and schema/type updates | None | Real MCP query test passes; budget/legacy retrieval/evaluation invariants pass |
| 4 | F3b/F3c bounded chunk/outline APIs plus permission/lifecycle tests | Agreed response contracts; can share stage-selection helper | Both endpoint-registration and real fetch tests pass; revision/corpus isolation proven |
| 5 | F3d/F4 full MCP tool contract, required CI job, browser recovery and artifact smoke | 1–4 | Real MCP tools/recovery browser flow pass and cannot silently skip in CI |
| 6 | Final regression and remaining release matrix | 1–5 and stable candidate | All P0/P1 resolved; environmental/deployment gates completed or explicit supported-scope decisions recorded |

F1 and F2 are small focused product fixes with security-sensitive validation. F3 is a larger API-completion change, not a three-line client workaround. Keep those scopes independently reviewable, then validate their integration on the same final candidate.

### Focused commands during fixes

From the repository root:

```sh
uv run pytest tests/test_account_lifecycle_release.py tests/test_auth_populated_release.py tests/test_account_migration_release.py -q --tb=short
uv run pytest tests/test_auth_route_matrix_release.py -q --tb=short
uv run --directory packages/ragcentre-mcp --with-editable ../.. python -c 'import ragcentre'
uv run --directory packages/ragcentre-mcp --with-editable ../.. pytest -m contract -q --tb=short
```

Add focused tests for new agent API behavior in an appropriate backend module (for example `tests/test_agent_api.py`) and a browser recovery spec with isolated setup. Run them with their related suites at each checkpoint.

### Final candidate commands and deployment work

```sh
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest -m 'not integration and not cloud'
(cd ui && npm run lint && npm test && npm run check:contract && npm run build && npm run e2e)
(cd packages/ragcentre-mcp && uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest -m 'not contract')
uv run --directory packages/ragcentre-mcp --with-editable ../.. pytest -m contract
uv build --out-dir .pytest_cache/release-dist
uv run python tests/release_artifact_smoke.py .pytest_cache/release-dist/ragcentre-0.0.4-py3-none-any.whl
```

Use the actual package version's filename if it changes. Then complete the testing plan's Linux/Python/Node 22 and cross-browser matrix, Docker setup modes, trusted HTTPS proxy, current-release backup/restore, supported stores/live OCR, and bounded resilience/performance checks. Previous-release upgrade, migration-compatibility and rollback rehearsals are not applicable to this first release. Record actual environments and skip/scope decisions. Passing the previously failing 11 invocations alone does not constitute release sign-off.

## 7. Definition of done

- [x] F1 repeated password change, CLI recovery and credential-version race tests pass, including independently reopened storage.
- [ ] F2 deployment gate: helper, HTTP, IPv4/IPv6/default-port and trusted/untrusted ASGI proxy-middleware tests pass; actual TLS proxy deployment remains pending.
- [x] F3 query budget, ordinal, chunk-window and bounded-outline APIs match generated schema and MCP models.
- [x] Actual MCP list/search/fetch tools work against the real backend, respect corpus restrictions and fail after key revocation.
- [ ] CI execution gate: dedicated Python 3.11/3.13 job is configured to require current editable backend and reject skipped/zero contracts; local 14-contract run passes, remote CI execution pending.
- [x] Browser recovery and installed-wheel recovery work alongside the existing acceptance journeys: 16 browser journeys and exact-wheel smoke pass.
- [ ] Local full regression passes (1,854 backend tests; no failures), but 565 skips and wider environment/deployment gates still need reconciliation.
- [ ] Working-tree artifacts rebuilt/tested and wheel hash recorded; final committed candidate SHA, complete release gates and release-owner sign-off remain pending. Verdict stays NO-GO.

No schema migration was needed. Production code, active regressions, generated types and CI configuration are now implemented, with results in the follow-up report. Changes remain uncommitted; no push, PR, tag or publication has been performed.
