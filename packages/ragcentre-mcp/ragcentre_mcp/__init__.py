"""A read-only MCP server for RAGCentre."""

__version__ = "0.1.0.dev0"

# The first RAGCentre release with the endpoints this server calls (chunk windows, document
# outlines, `max_chars`, the read-only token and `/v1/auth/whoami`). See docs/plans/agent-access.md.
MIN_SERVER_VERSION = "0.0.3"
