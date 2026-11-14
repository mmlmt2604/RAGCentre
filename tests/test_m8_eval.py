from __future__ import annotations

import json
import math
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

import ragcentre.retrieval.engine as engine
from ragcentre.context import AppContext
from ragcentre.eval.compare import paired_bootstrap
from ragcentre.eval.matching import matches_target, normalise
from ragcentre.eval.metrics import item_metrics, percentile
from ragcentre.eval.runner import run_eval
from ragcentre.pipeline.planner import create_pipeline
from test_m6_ingestion import _add_providers, _pipeline
from test_m7_retrieval import drain

DOCS = {
    "warranty": b"# Warranty\n\nThe warranty period is 24 months from purchase.\n\n## Claims\n\nClaims need a receipt.",
    "battery": b"# Battery\n\nCharge the battery for four hours before first use.",
    "filters": b"# Filters\n\nReplace the filter every three months.",
}
ITEMS = [
    {"question": "How long is the warranty?", "targets": [{"document": "warranty", "text_contains": "24 MONTHS"}]},
    {"question": "How long should I charge the battery?", "targets": [{"document": "battery"}]},
    {"question": "When do I replace the filter?", "targets": [{"document": "filters"}], "filter": {"lang": "en"}},
]


# ---- metrics, matching, comparison (hand-computed) ---------------------------------------------


def test_metrics_two_chunks_hit_one_target() -> None:
    # Two targets; ranks 1 and 2 both match target 0, rank 3 matches target 1.
    m = item_metrics([[0], [0], [1]], n_targets=2, k_values=[1, 3])
    assert m["hit_rate@1"] == 1.0 and m["recall@1"] == 0.5 and m["precision@1"] == 1.0
    assert m["ndcg@1"] == pytest.approx(1.0)
    assert m["hit_rate@3"] == 1.0 and m["recall@3"] == 1.0 and m["precision@3"] == 1.0
    # DCG = 1/log2(2) + 0 (target 0 already counted) + 1/log2(4) = 1.5; IDCG = 1 + 1/log2(3)
    assert m["ndcg@3"] == pytest.approx(1.5 / (1 + 1 / math.log2(3)))
    assert m["mrr"] == 1.0


def test_metrics_late_hit_and_short_list() -> None:
    m = item_metrics([[], [1], []], n_targets=2, k_values=[1, 3, 5])
    assert m["hit_rate@1"] == 0.0 and m["recall@1"] == 0.0 and m["ndcg@1"] == 0.0
    assert m["recall@3"] == 0.5 and m["precision@3"] == pytest.approx(1 / 3)
    assert m["ndcg@3"] == pytest.approx((1 / math.log2(3)) / (1 + 1 / math.log2(3)))
    assert m["precision@5"] == pytest.approx(1 / 5)  # divided by k, not by the number of results
    assert m["mrr"] == 0.5
    assert item_metrics([], 1, [1])["mrr"] == 0.0
    # mrr only looks within max(k_values)
    assert item_metrics([[], [], [0]], 1, [1, 2])["mrr"] == 0.0


def test_matching_rules() -> None:
    chunk = {"document_id": "d", "text": "The  ﬁlter\nlasts 24 MONTHS", "page_start": 3, "page_end": 4}
    assert normalise("The  ﬁlter\nlasts") == "the filter lasts"
    assert matches_target(chunk, {"document_id": "d", "text_contains": "filter lasts 24 months"})
    assert not matches_target(chunk, {"document_id": "e"})
    assert matches_target(chunk, {"document_id": "d", "pages": [1, 4]})
    assert not matches_target(chunk, {"document_id": "d", "pages": [5]})
    no_pages = {**chunk, "page_start": None, "page_end": None}
    assert not matches_target(no_pages, {"document_id": "d", "pages": [3]})
    assert matches_target(no_pages, {"document_id": "d"})


def test_paired_bootstrap() -> None:
    same = [0.0, 1.0, 0.5, 1.0, 0.0]
    c = paired_bootstrap(same, same)
    assert (c.diff, c.ci_low, c.ci_high, c.significant) == (0.0, 0.0, 0.0, False)
    better = paired_bootstrap([0.0] * 20, [1.0] * 20)
    assert better.diff == 1.0 and better.significant and better.ci_low == 1.0
    noisy = paired_bootstrap([0, 1, 0, 1, 0, 1], [1, 0, 1, 0, 1, 0])
    assert not noisy.significant and noisy.ci_low < 0 < noisy.ci_high
    assert paired_bootstrap([0.2, 0.4], [0.3, 0.9]) == paired_bootstrap([0.2, 0.4], [0.3, 0.9])  # fixed seed
    assert percentile([5, 1, 3, 2, 4], 50) == 3 and percentile([5, 1, 3, 2, 4], 95) == 5 and percentile([], 50) == 0


# ---- API -----------------------------------------------------------------------------------------


@pytest.fixture
def corpus(ctx: AppContext, client: TestClient) -> dict[str, Any]:
    _add_providers(ctx)
    schema = {"fields": {"lang": {"type": "str", "default": "en"}}}
    cid = client.post("/v1/corpora", json={"name": "manuals", "metadata_schema": schema}).json()["id"]
    for ext, content in DOCS.items():
        r = client.post(f"/v1/corpora/{cid}/documents", files={"file": (f"{ext}.md", content, "text/markdown")},
                        data={"external_id": ext})  # fmt: skip
        assert r.status_code == 201
    p1 = create_pipeline(ctx, cid, _pipeline())
    same = _pipeline()
    same["name"] = "same"
    p2 = create_pipeline(ctx, cid, same)
    drain(ctx)
    return {"corpus_id": cid, "p1": p1, "p2": p2}


def upload(client: TestClient, cid: str, lines: list[Any], name: str = "qa") -> Any:
    body = "\n".join(line if isinstance(line, str) else json.dumps(line) for line in lines)
    return client.post(f"/v1/corpora/{cid}/eval/datasets", data={"name": name},
                       files={"file": ("qa.jsonl", body.encode(), "application/jsonl")})  # fmt: skip


def test_import_and_errors_with_line_numbers(client: TestClient, corpus: dict[str, Any]) -> None:
    cid = corpus["corpus_id"]
    r = upload(client, cid, [ITEMS[0], "", {"question": "x", "targets": [{"document": "nope"}]}, "{bad"])
    assert r.status_code == 422
    errors = r.json()["error"]["details"]["errors"]
    assert errors == [
        {"line": 3, "message": "targets[0]: unknown document 'nope'"},
        {"line": 4, "message": errors[1]["message"]},
    ]
    assert errors[1]["message"].startswith("invalid JSON")
    bad_filter = upload(client, cid, [{"question": "q", "targets": [{"document": "battery"}], "filter": {"x": 1}}])
    assert bad_filter.json()["error"]["details"]["errors"][0]["line"] == 1
    r = upload(client, cid, ITEMS)
    assert r.status_code == 201, r.text
    ds = r.json()
    assert set(ds) == {"id", "corpus_id", "name", "item_count", "created_at"} and ds["item_count"] == 3
    assert upload(client, cid, ITEMS).status_code == 409
    items = client.get(f"/v1/eval/datasets/{ds['id']}/items").json()["items"]
    assert items[0]["targets"][0]["document_label"] == "warranty"
    assert items[0]["targets"][0]["pages"] is None and items[2]["filter"] == {"lang": "en"}
    assert client.get(f"/v1/corpora/{cid}/eval/datasets").json()["items"][0]["id"] == ds["id"]


def test_run_against_itself_and_item_diff(ctx: AppContext, client: TestClient, corpus: dict[str, Any]) -> None:
    cid, p1, p2 = corpus["corpus_id"], corpus["p1"], corpus["p2"]
    ds = upload(client, cid, ITEMS).json()
    r = client.post("/v1/eval/runs", json={"dataset_id": ds["id"], "pipeline_ids": [p1, p2], "k_values": [1, 3]})
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["status"] == "pending" and run["progress"] == {"done": 0, "total": 6}
    assert client.get(f"/v1/eval/runs/{run['id']}").json()["summaries"] == []
    ctx.start_workers(2)
    try:
        deadline = time.monotonic() + 15
        while client.get(f"/v1/eval/runs/{run['id']}").json()["status"] != "finished":
            assert time.monotonic() < deadline, "run did not finish"
            time.sleep(0.1)
    finally:
        ctx.close_workers()
    detail = client.get(f"/v1/eval/runs/{run['id']}").json()
    assert detail["progress"] == {"done": 6, "total": 6} and detail["finished_at"]
    assert [s["pipeline_id"] for s in detail["summaries"]] == [p1, p2]
    s = detail["summaries"][0]
    assert set(s) == {"pipeline_id", "pipeline_name", "pipeline_fingerprint", "pipeline_archived", "metrics",
                      "latency_ms", "error_count", "chunk_count", "mean_chunk_chars"}  # fmt: skip
    assert s["metrics"]["hit_rate@3"] == 1.0 and s["error_count"] == 0 and s["chunk_count"] >= 3
    assert set(s["metrics"]) == {"hit_rate@1", "recall@1", "precision@1", "ndcg@1", "hit_rate@3", "recall@3",
                                 "precision@3", "ndcg@3", "mrr"}  # fmt: skip
    assert detail["summaries"][0]["metrics"] == detail["summaries"][1]["metrics"]
    assert len(detail["comparisons"]) == 9
    for c in detail["comparisons"]:
        assert c["a"] == p1 and c["b"] == p2
        assert c["diff"] == 0 and c["ci_low"] == 0 and c["ci_high"] == 0 and c["significant"] is False
    diff = client.get(f"/v1/eval/runs/{run['id']}/items", params={"a": p1, "b": p2, "metric": "recall@3"}).json()
    assert len(diff["items"]) == 3 and {x["outcome"] for x in diff["items"]} == {"unchanged"}
    first = diff["items"][0]
    assert first["a"]["retrieved"][0]["matched_targets"] == [0]
    assert first["item"]["question"] == ITEMS[0]["question"]
    only = client.get(f"/v1/eval/runs/{run['id']}/items",
                      params={"a": p1, "b": p2, "metric": "recall@3", "only": "differs"}).json()  # fmt: skip
    assert only["items"] == []
    assert client.get(f"/v1/eval/runs/{run['id']}/items", params={"a": p1, "b": p2, "metric": "x"}).status_code == 422
    runs = client.get(f"/v1/corpora/{cid}/eval/runs").json()["items"]
    assert runs[0]["id"] == run["id"] and runs[0]["dataset_name"] == "qa"


def test_non_ready_pipeline_refused_unless_partial(ctx: AppContext, client: TestClient, corpus: dict[str, Any]) -> None:
    cid = corpus["corpus_id"]
    ds = upload(client, cid, ITEMS).json()
    building = _pipeline()
    building["name"] = "building"
    building["nodes"][1]["options"]["max_chars"] = 400
    pid = create_pipeline(ctx, cid, building)
    r = client.post("/v1/eval/runs", json={"dataset_id": ds["id"], "pipeline_ids": [corpus["p1"], pid]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "index_incomplete"
    assert r.json()["error"]["details"]["pipeline_id"] == pid
    r = client.post("/v1/eval/runs", json={"dataset_id": ds["id"], "pipeline_ids": [pid], "allow_partial": True})
    assert r.status_code == 201
    run_eval(ctx, r.json()["id"])
    detail = client.get(f"/v1/eval/runs/{r.json()['id']}").json()
    assert detail["status"] == "finished"
    dup = client.post("/v1/eval/runs", json={"dataset_id": ds["id"], "pipeline_ids": [pid, pid]})
    assert dup.status_code == 422


def test_interrupted_run_resumes(
    ctx: AppContext, client: TestClient, corpus: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = corpus["corpus_id"]
    ds = upload(client, cid, ITEMS).json()
    run_id = client.post(
        "/v1/eval/runs", json={"dataset_id": ds["id"], "pipeline_ids": [corpus["p1"], corpus["p2"]]}
    ).json()["id"]
    real = engine.query
    calls: list[tuple[str, str]] = []

    class Crash(BaseException):
        pass

    def crashing(*args: Any, **kwargs: Any) -> Any:
        if len(calls) == 4:
            raise Crash
        calls.append((args[2], args[3]))
        return real(*args, **kwargs)

    monkeypatch.setattr(engine, "query", crashing)
    with pytest.raises(Crash):
        run_eval(ctx, run_id)
    assert ctx.db.scalar("SELECT COUNT(*) FROM eval_results WHERE run_id = ?", (run_id,)) == 4
    assert client.get(f"/v1/eval/runs/{run_id}").json()["status"] == "running"
    first_pass = list(calls)
    calls.clear()

    def counting(*args: Any, **kwargs: Any) -> Any:
        calls.append((args[2], args[3]))
        return real(*args, **kwargs)

    monkeypatch.setattr(engine, "query", counting)
    run_eval(ctx, run_id)
    assert len(calls) == 2  # only the two unfinished rows were computed
    assert not set(calls) & set(first_pass)
    assert client.get(f"/v1/eval/runs/{run_id}").json()["status"] == "finished"


def test_failed_query_counts_as_error(ctx: AppContext, client: TestClient, corpus: dict[str, Any]) -> None:
    cid = corpus["corpus_id"]
    ds = upload(client, cid, ITEMS).json()
    run_id = client.post("/v1/eval/runs", json={"dataset_id": ds["id"], "pipeline_ids": [corpus["p1"]]}).json()["id"]
    ctx.db.execute("UPDATE pipelines SET archived = 1 WHERE id = ?", (corpus["p1"],))
    run_eval(ctx, run_id)
    detail = client.get(f"/v1/eval/runs/{run_id}").json()
    summary = detail["summaries"][0]
    assert summary["error_count"] == 3 and summary["pipeline_archived"] is True
    assert all(v == 0 for v in summary["metrics"].values())
    item = client.get(f"/v1/eval/runs/{run_id}/items", params={"a": corpus["p1"], "b": corpus["p1"]}).json()
    assert "pipeline_archived" in item["items"][0]["a"]["error"]


def test_delete_dataset(client: TestClient, corpus: dict[str, Any]) -> None:
    ds = upload(client, corpus["corpus_id"], ITEMS).json()
    assert client.delete(f"/v1/eval/datasets/{ds['id']}").status_code == 204
    assert client.get(f"/v1/eval/datasets/{ds['id']}").status_code == 404
