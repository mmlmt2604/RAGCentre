# Integration fragments (vector-store compatibility, v0.1.13)

Workers do not commit edits to shared files. Each worker writes `integration/<task-id>.md`
in its own branch; the lead applies it at merge time and deletes it.

## Fragment format

```
## pyproject extras
<extra name> = ["<package>>=<tested lower bound>"]

## registry BUILTIN
"<type>": "ragcentre.stores.<module>:STORE_TYPE"

## docker-compose service (host port from the table below)
<yaml block>

## ci stores matrix entry
<service, extra, test module>

## README store-table row
<markdown row>

## DECISIONS.md
<numbered decisions; say what was run against a live server versus only read>
```

## Host ports (one compose project per task: `rc-<task-id>`)

| Task | Service | Host port |
|---|---|---|
| t-pgvector | pgvector | 54329 |
| t-chroma | chroma | 58000 |
| t-weaviate | weaviate http / grpc | 58080 / 50051 |
| t-milvus | milvus standalone | 59530 |
| t-search | elasticsearch / opensearch | 59200 / 59201 |
| t-redis | redis 8 | 56379 |
| t-mongodb | atlas-local | 57017 |

Plan: `docs/plans/vector-store-compatibility.md` (git-ignored; the lead copies it into each worktree).
