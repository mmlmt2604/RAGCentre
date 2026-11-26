# Local accounts and team membership

RAGCentre stores users, password hashes, sessions and team memberships in its existing SQLite database. No external identity provider, organization account, email service or PostgreSQL server is required. SSO and organization management are deferred.

Each corpus belongs to one team. Its documents, pipelines, query results, evaluations and sources share that team's access boundary. Members of a team can read its documents; there are no private documents within a team. A person can hold different roles in several teams.

## First administrator

Use the same `RAGCENTRE_DATA_DIR` as the server. Run:

```sh
uv run ragcentre user init-admin
```

Enter a username and a password when prompted. Passwords must contain 15–128 characters, with at most 512 UTF-8 bytes. They are stored using Argon2id. The command creates the first platform administrator, assigns ownership of the existing Default team and permanently activates local authentication for this database. It refuses to replace an existing active administrator.

To finish setup in the browser instead, run `uv run ragcentre user setup-link`. It prints a one-time installation link, valid for 15 minutes, that opens the account-creation page. The page strips the token fragment from the address bar after reading it, and the claim is consumed on success. A subsequent visit with an expired or reused claim explains how to obtain a fresh link; no anonymous bootstrap form is served.

For development on this computer:

```sh
export RAGCENTRE_INSECURE_LOCAL_COOKIE=true
uv run ragcentre serve
```

Open `http://127.0.0.1:8700` and log in. The development-cookie setting is allowed only on a loopback server and, if configured, with a loopback browser origin. Build the UI first when running from source (`npm --prefix ui ci` and `npm --prefix ui run build`).

For remote use, configure an HTTPS reverse proxy and the exact browser origin:

```sh
export RAGCENTRE_AUTH_MODE=local
export RAGCENTRE_BROWSER_ORIGIN=https://rag.example.net
uv run ragcentre serve --host 0.0.0.0
```

The origin has no path or trailing slash. To mount the UI under a sub-path, set `RAGCENTRE_BROWSER_BASE_PATH` to that prefix (for example `/ragcentre`) and have the proxy route that prefix to the backend; one-time links and asset paths honor the prefix. Do not set the development-cookie flag for remote serving. Preserve the browser's Origin header at the proxy and restrict direct access to the backend. Session cookies are host-only, Secure, HttpOnly and SameSite=Lax. If the browser connects through a proxy, avoid granting arbitrary clients control of trusted forwarded headers.

For the Vite development proxy, configure `RAGCENTRE_BROWSER_ORIGIN=http://localhost:5173` and explicit loopback development cookies on the backend. Start the UI with `npm --prefix ui run dev:live`.

An explicit local-mode server without an initialized administrator serves the login/setup information but grants no anonymous administrative access. Initialize the administrator using the local command. There is no public first-admin registration endpoint.

## Accounts, teams and roles

Platform administrators create accounts through the Users screen. A new account is pending until its recipient sets a password through a one-time setup link. The administrator can copy/share the link manually; SMTP is unnecessary. Links expire after 24 hours, are shown once and can be replaced while the account is pending. The setup page removes the token fragment from the address bar. Treat a setup link as a credential. Re-enabling a disabled account that never set a password returns it to pending, so a fresh setup link can be issued.

An account does not automatically join Default or any other team. Assign memberships by exact username. A person without memberships can log in and change their password but sees an access-pending screen.

| Role | Permissions within the selected team |
|---|---|
| Viewer | Read corpora, documents, pipelines, evaluation results and activity; run queries |
| Editor | Viewer access plus manage corpora/documents/pipelines, run evaluations and retry work; inspect/use existing providers |
| Owner | Editor access plus manage providers, connector sources, memberships and team service tokens |
| Platform administrator | Manage users/teams/global infrastructure and operate in any selected team |

Only platform administrators create teams and global accounts. A new team created through the account UI needs an active initial owner. Owners add existing users to their own team and cannot grant platform administration. The final active owner cannot be removed or demoted, and the final active platform administrator cannot be disabled or demoted. Disabling an owner requires another active owner for each affected team.

Select the active team in the UI. The backend validates that selection; changing an ID in a URL or request cannot grant another team's access. Other-team resources return 404. A permitted team's action that exceeds the user's role returns 403. Missing/expired credentials return 401.

Removing or demoting a membership applies on subsequent requests. It does not cancel already authorized ingestion, sync or evaluation jobs, and it does not delete the team's documents. Disabling an account or changing/recovering its password revokes its existing browser sessions.

## Invitations

Team owners invite people through the team's Members screen. An invitation names a username, a role and an optional display name, and produces a one-time link. Sharing the link manually is the delivery mechanism; email is not required.

- If no account exists under that username, the invitation creates a pending account and the link walks the recipient through choosing a password (activation). The membership and a fresh session are created in the same transaction, so a half-completed activation leaves no membership behind.
- If the account already exists, the link asks the recipient to sign in as that account and accept the invitation. It refuses to accept on behalf of a different signed-in account.
- Invitations expire, are shown once, and can be reissued or revoked while still pending. Issuer and target lifecycle changes invalidate a token; activation re-checks every precondition inside the transaction.

Accepting an invitation grants only team membership at the invited role, never platform administration.

## Existing features

- Pipeline resolution, document IDs, provider references, fingerprints, index collections and cached computation stay intact. Adding accounts requires no re-ingestion or re-indexing.
- Connector schedules and queued work run under the owning corpus/team independently of user login. Source administration is owner-only. Imported documents become readable to the team's members; source-system per-document permissions are not copied.
- A connector provider must belong to the same team as the source's corpus. Existing inconsistent sources fail closed rather than use a different team's credentials.
- Connector provider types expose a JSON schema for their source scope. Creating or editing a source validates the supplied scope (for example an S3 bucket, a SharePoint drive id or a Confluence space key); unknown fields are preserved rather than dropped.
- Editors can build pipelines using existing providers. Provider configuration/secrets and source scope changes require owner permission.
- Global OCR discovery, OCR checks and maintenance require a platform administrator. The full first-run OCR wizard remains available to administrators; ordinary members do not poll global OCR services.
- CLI and MCP continue to use existing bearer tokens. Human login does not change their `whoami` response or expand the narrow read-token allowlist. Existing full tokens retain their existing token-management and resource permissions, but receive no access to the new account/membership administration APIs.
- Removing a person does not revoke independent service tokens they may have copied. Owners should review and rotate those tokens separately when needed.

## Team setup

A team owner can run a resumable setup wizard to create a corpus and its first pipeline. The wizard drafts, validates and runs the steps in order — document parser, embeddings, vector store, corpus, pipeline, active pipeline, and saved-service checks — and saves each choice per account and team. An interrupted run resumes where it stopped instead of starting over, and retries reuse work that already succeeded.

- **Trial** uses built-in components (in-process text parsing, hash embeddings, SQLite) and indexes a sample document, so the owner can see a real result without configuring services. Trial never runs global OCR discovery.
- **Custom** connects services the owner already runs or registers new parser/embedding/vector-store providers. Credentials are entered through the provider settings, never saved into the setup draft.

Setup drafts store only the wizard's known non-secret fields; URLs with credentials, query or fragment parts and secret-shaped values are rejected before they are persisted. The final "ready" state is derived server-side from the saved resources, not taken from the client.

## Sessions and recovery

Sessions expire after eight hours of inactivity or seven days total by default. Activity timestamps are coarsened to avoid a SQLite write on every request, so idle expiry can occur up to one minute early. Password changes sign out all sessions. Login and account setup are rate limited; the limiter supports one API process plus separate ingestion worker processes.

If a password is lost, an operator with filesystem access can recover the account locally:

```sh
uv run ragcentre user reset-password USERNAME
```

This sets a new password, reactivates the account and revokes old sessions/setup links. Passwords are entered through hidden terminal prompts, never command-line arguments.

A platform administrator can also issue a recovery link from the Users screen for an active non-admin account. The server verifies the administrator's own password first, then shows a one-time link; the recipient sets a new password and all of that account's sessions are revoked. Recovery completes without signing in, so the account holder must sign in again with the new password.

If team ownership needs local repair:

```sh
uv run ragcentre team set-owner TEAM USERNAME
```

TEAM accepts an ID, slug or unambiguous name. The user must be active. These local operator commands do not start workers or the source scheduler.

## Configuration and upgrades

| Variable | Default | Purpose |
|---|---|---|
| `RAGCENTRE_AUTH_MODE` | Unset | `legacy` or `local`; unset honors the persisted local-account activation marker |
| `RAGCENTRE_BROWSER_ORIGIN` | Unset | Exact allowed browser origin; required for non-loopback local-auth serving |
| `RAGCENTRE_BROWSER_BASE_PATH` | Unset | Leading path prefix when the UI is mounted under a sub-path, e.g. `/ragcentre` |
| `RAGCENTRE_INSECURE_LOCAL_COOKIE` | `false` | Explicit loopback-only HTTP development cookie |
| `RAGCENTRE_SESSION_IDLE_SECONDS` | `28800` | Session inactivity lifetime |
| `RAGCENTRE_SESSION_LIFETIME_SECONDS` | `604800` | Absolute session lifetime |
| `RAGCENTRE_AUDIT_RETENTION_DAYS` | `90` | Retention for local account audit events |

Unset auth mode preserves existing installations until local accounts are initialized. After initialization, the database remembers local mode: clearing environment variables, deleting sessions or deleting all team tokens cannot restore anonymous administration. Explicit `legacy` and `--allow-no-token` are rejected after activation. Existing configured bearer tokens remain accepted in local mode.

Before upgrading, stop writers and back up the data directory and provider encryption-key material consistently. Schema migrations 7, 8 and 9 add identity tables, invitation/recovery/claim tables and team-setup state without rewriting existing documents, team tokens, pipelines or source state. Assign initial owners to existing teams explicitly; no users are bulk-enrolled.

An older binary cannot open a newer schema. To roll back, restore a matching code/data backup; do not drop the account tables to bypass authentication. A pre-activation backup also restores its previous authentication policy, so configure or initialize authentication before exposing a restored server.
