# Quickstart

This walk-through needs nothing outside this repository: no model downloads, no API keys and no
other services. It uses the built-in text parser (`builtin_text`), the built-in hashing embedder
(`builtin_hash`) and the default SQLite vector store (`sqlite_vec`). It builds two pipelines over
a small set of documents from a fictional bike shop, queries them, and ends with an evaluation
that compares the two chunkers.

`builtin_hash` matches words, not meaning, so expect keyword-style search quality. Swap in a real
embedding model later (see `examples/providers/openai-embedder.yaml`) without changing anything
else.

You need [uv](https://docs.astral.sh/uv/) and a terminal open at the repository root. Commands
are run with `uv run`, which uses the project's own environment.

## 1. Start the server

```sh
uv sync
uv run ragcentre serve
```

The API listens on `http://127.0.0.1:8700` and keeps its data in `./data`. Leave it running and
open a second terminal at the repository root for the remaining steps.

## 2. Add an embedder and a vector store

A parser named `builtin-text` already exists. Add the hashing embedder and the SQLite vector
store:

```sh
uv run ragcentre provider add -f examples/providers/hash-embedder.yaml
uv run ragcentre provider add -f examples/providers/local-vectors.yaml
uv run ragcentre provider list
```

## 3. Create a corpus and upload documents

The corpus has one filterable metadata field, `audience`, which defaults to `customer`
(`examples/quickstart/schema.yaml`). The staff document is tagged `audience=staff`.

```sh
uv run ragcentre corpus create kestrel --schema examples/quickstart/schema.yaml
uv run ragcentre docs add kestrel examples/quickstart/docs/customer/*.md
uv run ragcentre docs add kestrel examples/quickstart/docs/staff/workshop-procedures.md --metadata audience=staff
```

Each document's external id is its file name. Running `docs add` again with an unchanged file does
nothing; with a changed file it replaces the content and re-indexes only that document.

## 4. Build two pipelines

`examples/pipelines/fixed.yaml` cuts documents into 300-character windows.
`examples/pipelines/structure.yaml` follows the headings instead. Both use the same parser,
embedder, indexes and retrieval settings: only the chunker differs.

Preview the first pipeline's work, then create it:

```sh
uv run ragcentre pipeline create kestrel -f examples/pipelines/fixed.yaml --dry-run
uv run ragcentre pipeline create kestrel -f examples/pipelines/fixed.yaml
```

The second pipeline's preview shows its `parse` node as `reused`: RAGCentre keys work by what it
computes, not by pipeline, so the documents are not parsed again.

```sh
uv run ragcentre pipeline create kestrel -f examples/pipelines/structure.yaml --dry-run
uv run ragcentre pipeline create kestrel -f examples/pipelines/structure.yaml
```

Wait for both to finish (a few seconds):

```sh
uv run ragcentre pipeline status fixed --watch
uv run ragcentre pipeline status structure --watch
uv run ragcentre pipeline diff fixed structure
```

## 5. Query

Make `structure` the corpus's default pipeline and ask a question. The second query only searches
staff documents, through the metadata filter:

```sh
uv run ragcentre corpus activate kestrel structure
uv run ragcentre query kestrel "How long is the warranty on a carbon frame?" --top-k 3
uv run ragcentre query kestrel "torque for the stem bolts" --filter '{"audience": "staff"}' --top-k 2
uv run ragcentre query kestrel "How long is the warranty on a carbon frame?" --pipeline fixed --top-k 3
```

## 6. Compare the chunkers

`examples/quickstart/questions.jsonl` has 24 questions. Each names the document and a snippet of
text that answers it, so the ground truth does not depend on how either pipeline chunks.

```sh
uv run ragcentre eval import kestrel questions examples/quickstart/questions.jsonl
uv run ragcentre eval run questions fixed structure --wait
```

The last command prints both pipelines' metrics side by side: `hit_rate`, `recall`, `precision`
and `ndcg` at k = 1, 3, 5 and 10, plus `mrr`. `DIFF (b-a)` is `structure` minus `fixed`, with a
95 % confidence interval from a paired bootstrap over the questions. Rows marked `*` in the `SIG`
column are differences whose interval excludes zero. With these documents the heading-aware
chunker should come out ahead at k = 3 and k = 5. Show the run again at any time with
`uv run ragcentre eval show RUN_ID`.

## Start again

Stop the server with Ctrl-C and delete `./data`.
