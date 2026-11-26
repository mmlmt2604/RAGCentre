# ragcentre-mcp

A read-only [MCP](https://modelcontextprotocol.io) server that lets agents search a RAGCentre knowledge base:
Claude Code, Claude Desktop, Cursor, the OpenAI Responses API, or any other MCP client.

It is a client of RAGCentre's HTTP API and does not depend on the `ragcentre` package, so the machine running
the agent does not need the server's dependencies. It needs RAGCentre 0.0.3 or later and refuses to start
against an older server.

## Tools

| Tool | What it does |
|---|---|
| `list_corpora` | The corpora you can search, their document counts and their metadata filter fields. |
| `search` | Ranked passages for a query, each with `chunk_id`, `document_id`, filename, pages and section headings to cite. Optional `corpus`, `top_k` (1–50) and `filter`. |
| `fetch` | With a `chunk_id`: that passage plus up to 10 neighbouring passages on each side. With a `document_id`: the document's metadata and an outline of its sections, each with a `first_chunk_id` to fetch. It never returns a whole document. |

Every tool returns structured content (with an output schema) and a text form in which each passage is wrapped
in a `<result>` or `<chunk>` tag carrying its ids. Tool results are capped at `--max-chars` characters of
document text (default 12,000); when results are cut, `truncated` is true.

Retrieved text is untrusted: a document can contain text written to steer an agent. The server only reads, so
the worst a document can do through it is mislead the agent, not change anything. Don't give the same agent
tools that write or send data without thinking about that.

## Install and connect

```sh
uv tool install ragcentre-mcp      # or: pipx install ragcentre-mcp
```

It reads two settings from the environment:

| Variable | |
|---|---|
| `RAGCENTRE_URL` | Where RAGCentre listens. Default `http://127.0.0.1:8700`. |
| `RAGCENTRE_API_TOKEN` | A RAGCentre **read** API key, minted under **Account → API keys** in the web UI. |

At startup it asks RAGCentre what the token can do. If it is the full-access token, it refuses to start unless
you pass `--allow-full-token`. The tools only read either way, but a full token in an agent's config is one leak
away from someone changing your data.

**Claude Code**

```sh
claude mcp add ragcentre --env RAGCENTRE_API_TOKEN=... -- ragcentre-mcp --corpus manuals
```

**Claude Desktop, Cursor and other clients that take JSON**

```json
{
  "mcpServers": {
    "ragcentre": {
      "command": "uvx",
      "args": ["ragcentre-mcp", "--corpus", "manuals"],
      "env": { "RAGCENTRE_URL": "http://127.0.0.1:8700", "RAGCENTRE_API_TOKEN": "..." }
    }
  }
}
```

**Over HTTP** (remote clients, the OpenAI Responses API)

```sh
RAGCENTRE_MCP_TOKEN=$(openssl rand -hex 32) ragcentre-mcp --transport http --host 0.0.0.0 --port 8710
```

Clients send `Authorization: Bearer $RAGCENTRE_MCP_TOKEN` to `http://HOST:8710/mcp`. The token is required
whenever the server listens on anything but a loopback address. Put it behind HTTPS for anything beyond a
private network. Hosted clients that require OAuth (such as claude.ai connectors) need an OAuth proxy in front;
see `docs/plans/agent-access.md`, phase 5.

```python
client.responses.create(
    model="...",
    tools=[{
        "type": "mcp",
        "server_label": "ragcentre",
        "server_url": "https://rag.example.com/mcp",
        "headers": {"Authorization": "Bearer ..."},
        "require_approval": "never",
    }],
    input="What torque do the stem bolts need?",
)
```

## Options

```
ragcentre-mcp [--transport stdio|http] [--host 127.0.0.1] [--port 8710] [--corpus NAME ...]
              [--default-top-k 8] [--max-chars 12000] [--allow-full-token] [--log-level INFO]
```

- `--corpus` exposes only the named corpora (by name or id; repeatable). Without it, every corpus is exposed.
  Corpora are fixed by id at startup, so renaming one later doesn't widen what the agent can see. Other corpora,
  and their documents and chunks, behave as if they don't exist.
- Searches always use each corpus's active pipeline.
- The `search` tool's description lists each corpus's filter fields as they were at startup. Restart the server
  after changing a corpus's metadata schema; `list_corpora` always shows the current fields.

## Development

```sh
uv sync
uv run pytest           # no network, no RAGCentre needed: tests use a fake API
uv run ruff check . && uv run mypy
```
