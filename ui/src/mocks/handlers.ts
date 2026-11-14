// MSW request handlers implementing the API contract (§16, §22) over the in-memory store.
import { delay, http, HttpResponse, type HttpResponseResolver } from "msw";
import type { Page } from "../api/types";
import { db } from "./db";
import { refreshProviderUsage, retryRuns } from "./engine";
import * as out from "./serialize";
import * as admin from "./admin";
import * as docs from "./documents";
import * as ev from "./eval";
import { readForm } from "./multipart";
import * as pls from "./pipelines";
import * as ops from "./providers";
import { query } from "./query";
import { pipelineHealth, type MockState } from "./store";

let latency: [number, number] = [150, 400];

/** Tests set this to [0, 0]. */
export function setLatency(min: number, max: number): void {
  latency = [min, max];
}

function err(status: number, code: string, message: string, details?: Record<string, unknown>) {
  return HttpResponse.json({ error: { code, message, ...(details ? { details } : {}) } }, { status });
}

const notFound = (what: string) => err(404, "not_found", `${what} was not found.`);

function page<T>(items: T[], request: Request): Page<T> {
  const url = new URL(request.url);
  const limit = Math.min(500, Math.max(1, Number(url.searchParams.get("limit") ?? 50)));
  const start = Number(url.searchParams.get("cursor") ?? 0) || 0;
  const slice = items.slice(start, start + limit);
  return { items: slice, next_cursor: start + limit < items.length ? String(start + limit) : null };
}

type Ctx = { state: MockState; request: Request; params: Record<string, string | readonly string[] | undefined> };
type Handler = (ctx: Ctx) => Response | Promise<Response>;

/** Turn a thrown MockError into the API's error response. */
function guarded(handler: Handler): Handler {
  return async (ctx) => {
    try {
      return await handler(ctx);
    } catch (e) {
      if (e instanceof ops.MockError) return err(e.status, e.code, e.message, e.details);
      throw e;
    }
  };
}

async function body<T>(ctx: Ctx): Promise<T> {
  try {
    return (await ctx.request.json()) as T;
  } catch {
    throw new ops.MockError(422, "validation_error", "The request body must be JSON.");
  }
}

/** Wrap a handler with latency, the simulated network failure, and the auth check. */
function route(handler: Handler, opts: { public?: boolean } = {}): HttpResponseResolver {
  return async ({ request, params }) => {
    const [min, max] = latency;
    if (max > 0) await delay(min + Math.random() * (max - min));
    const state = db();
    if (state.failNetwork) return HttpResponse.error();
    if (!opts.public && state.authToken) {
      const auth = request.headers.get("Authorization");
      if (auth !== `Bearer ${state.authToken}`)
        return err(401, "unauthorized", "This server requires an access token.");
    }
    return guarded(handler)({ state, request, params: params as Ctx["params"] });
  };
}

const p = (ctx: Ctx, name: string) => String(ctx.params[name] ?? "");

export const handlers = [
  http.get("/v1/health", route(({ state }) => HttpResponse.json(out.health(state)), { public: true })),

  // Providers
  http.get("/v1/provider-types", route(({ state }) => HttpResponse.json({ items: state.providerTypes, next_cursor: null }))),
  http.get("/v1/providers", route(({ state, request }) => {
    refreshProviderUsage(state);
    return HttpResponse.json(page(state.providers, request));
  })),
  http.get("/v1/providers/:id", route((ctx) => {
    refreshProviderUsage(ctx.state);
    return HttpResponse.json(ops.findProvider(ctx.state, p(ctx, "id")));
  })),
  http.post("/v1/providers", route(async (ctx) => HttpResponse.json(ops.createProvider(ctx.state, await body(ctx)), { status: 201 }))),
  http.patch("/v1/providers/:id", route(async (ctx) => {
    const b = await body<{ config?: Record<string, unknown> }>(ctx);
    return HttpResponse.json(ops.patchProvider(ctx.state, p(ctx, "id"), b.config ?? {}));
  })),
  http.delete("/v1/providers/:id", route((ctx) => {
    ops.deleteProvider(ctx.state, p(ctx, "id"));
    return new HttpResponse(null, { status: 204 });
  })),
  http.post("/v1/providers/:id/test", route((ctx) => HttpResponse.json(ops.testProvider(ctx.state, p(ctx, "id"))))),
  http.put("/v1/providers/:id/secrets/:key", route(async (ctx) =>
    HttpResponse.json(ops.setSecret(ctx.state, p(ctx, "id"), p(ctx, "key"), await body(ctx))),
  )),
  http.delete("/v1/providers/:id/secrets/:key", route((ctx) => HttpResponse.json(ops.deleteSecret(ctx.state, p(ctx, "id"), p(ctx, "key"))))),

  // Corpora
  http.get("/v1/corpora", route(({ state, request }) => {
    const index = out.indexRuns(state);
    return HttpResponse.json(page(state.corpora.map((c) => out.corpus(state, c, index)), request));
  })),
  http.post("/v1/corpora", route(async (ctx) => {
    const c = ops.createCorpus(ctx.state, await body(ctx));
    return HttpResponse.json(out.corpus(ctx.state, c, out.indexRuns(ctx.state)), { status: 201 });
  })),
  http.patch("/v1/corpora/:id", route(async (ctx) => {
    const c = ops.renameCorpus(ctx.state, p(ctx, "id"), String((await body<{ name?: string }>(ctx)).name ?? ""));
    return HttpResponse.json(out.corpus(ctx.state, c, out.indexRuns(ctx.state)));
  })),
  http.patch("/v1/corpora/:id/metadata-schema", route(async (ctx) => {
    const c = ops.updateSchema(ctx.state, p(ctx, "id"), await body(ctx));
    return HttpResponse.json(out.corpus(ctx.state, c, out.indexRuns(ctx.state)));
  })),
  http.get("/v1/corpora/:id", route((ctx) => {
    const c = ctx.state.corpora.find((x) => x.id === p(ctx, "id"));
    return c ? HttpResponse.json(out.corpus(ctx.state, c, out.indexRuns(ctx.state))) : notFound("Corpus");
  })),
  http.put("/v1/corpora/:id/active-pipeline", route(async (ctx) => {
    const c = ctx.state.corpora.find((x) => x.id === p(ctx, "id"));
    if (!c) return notFound("Corpus");
    const body = (await ctx.request.json()) as { pipeline_id: string; force?: boolean };
    const pl = ctx.state.pipelines.find((x) => x.id === body.pipeline_id && x.corpus_id === c.id);
    if (!pl) return notFound("Pipeline");
    if (pl.archived) return err(409, "pipeline_archived", `${pl.name} is archived and cannot be made active.`);
    const h = pipelineHealth(ctx.state, pl);
    if (h.status !== "ready" && !body.force)
      return err(409, "index_incomplete", `${pl.name} is ${h.status} (${Math.round(h.coverage * 100)}% coverage). Confirm to make it active anyway.`, { status: h.status, coverage: h.coverage });
    c.active_pipeline_id = pl.id;
    return HttpResponse.json(out.corpus(ctx.state, c, out.indexRuns(ctx.state)));
  })),

  // Documents
  http.get("/v1/corpora/:id/documents", route((ctx) => {
    ops.findCorpus(ctx.state, p(ctx, "id"));
    const list = docs.listDocuments(ctx.state, p(ctx, "id"), new URL(ctx.request.url).searchParams);
    const pg = page(list, ctx.request);
    const index = out.indexRuns(ctx.state);
    return HttpResponse.json({ ...pg, items: pg.items.map((d) => out.document(ctx.state, d, index)) });
  })),
  http.post("/v1/corpora/:id/documents", route(async (ctx) => {
    const { doc, created } = docs.upload(ctx.state, p(ctx, "id"), await readForm(ctx.request));
    return HttpResponse.json(out.document(ctx.state, doc, out.indexRuns(ctx.state)), { status: created ? 201 : 200 });
  })),
  http.get("/v1/documents/:id", route((ctx) =>
    HttpResponse.json(out.document(ctx.state, docs.findDocument(ctx.state, p(ctx, "id")), out.indexRuns(ctx.state))),
  )),
  http.patch("/v1/documents/:id", route(async (ctx) => {
    const d = docs.patchDocument(ctx.state, p(ctx, "id"), await body(ctx));
    return HttpResponse.json(out.document(ctx.state, d, out.indexRuns(ctx.state)));
  })),
  http.put("/v1/documents/:id/content", route(async (ctx) => {
    const d = docs.replaceContent(ctx.state, p(ctx, "id"), await readForm(ctx.request));
    return HttpResponse.json(out.document(ctx.state, d, out.indexRuns(ctx.state)));
  })),
  http.delete("/v1/documents/:id", route((ctx) => {
    const d = docs.deleteDocument(ctx.state, p(ctx, "id"));
    return HttpResponse.json(out.document(ctx.state, d, out.indexRuns(ctx.state)), { status: 202 });
  })),
  http.get("/v1/documents/:id/content", route((ctx) => {
    const d = docs.findDocument(ctx.state, p(ctx, "id"));
    return new HttpResponse(docs.content(d), {
      headers: { "Content-Type": d.mime_type, "Content-Disposition": `attachment; filename="${d.filename.split("/").pop()}"` },
    });
  })),
  http.get("/v1/documents/:id/parsed", route((ctx) => {
    const d = docs.findDocument(ctx.state, p(ctx, "id"));
    return HttpResponse.json(docs.parsed(ctx.state, d, new URL(ctx.request.url).searchParams.get("pipeline_id") ?? ""));
  })),
  http.get("/v1/documents/:id/chunks", route((ctx) => {
    const d = docs.findDocument(ctx.state, p(ctx, "id"));
    const q = new URL(ctx.request.url).searchParams;
    return HttpResponse.json(docs.chunks(ctx.state, d, q.get("pipeline_id") ?? "", q.get("node_id")));
  })),

  // Pipelines
  http.get("/v1/corpora/:id/pipelines", route((ctx) => {
    const archived = new URL(ctx.request.url).searchParams.get("archived") === "true";
    const index = out.indexRuns(ctx.state);
    const items = ctx.state.pipelines
      .filter((x) => x.corpus_id === p(ctx, "id") && x.archived === archived)
      .sort((a, b) => b.created_at.localeCompare(a.created_at))
      .map((x) => out.pipeline(ctx.state, x, index));
    return HttpResponse.json(page(items, ctx.request));
  })),
  http.post("/v1/corpora/:id/pipelines", route(async (ctx) => {
    ops.findCorpus(ctx.state, p(ctx, "id"));
    const raw = await body<unknown>(ctx);
    if (new URL(ctx.request.url).searchParams.get("dry_run") === "true") return HttpResponse.json(pls.plan(ctx.state, p(ctx, "id"), raw));
    const rec = pls.create(ctx.state, p(ctx, "id"), raw);
    return HttpResponse.json(out.pipeline(ctx.state, rec, out.indexRuns(ctx.state)), { status: 201 });
  })),
  http.get("/v1/pipelines/:id/diff", route((ctx) =>
    HttpResponse.json(pls.diff(ctx.state, p(ctx, "id"), new URL(ctx.request.url).searchParams.get("against") ?? "")),
  )),
  http.get("/v1/pipelines/:id", route((ctx) => {
    const pl = ctx.state.pipelines.find((x) => x.id === p(ctx, "id"));
    return pl ? HttpResponse.json(out.pipeline(ctx.state, pl, out.indexRuns(ctx.state))) : notFound("Pipeline");
  })),
  http.get("/v1/pipelines/:id/status", route((ctx) => {
    const pl = ctx.state.pipelines.find((x) => x.id === p(ctx, "id"));
    return pl ? HttpResponse.json(out.pipelineStatus(ctx.state, pl, out.indexRuns(ctx.state))) : notFound("Pipeline");
  })),
  http.post("/v1/pipelines/:id/retry-failed", route((ctx) => {
    const pl = ctx.state.pipelines.find((x) => x.id === p(ctx, "id"));
    if (!pl) return notFound("Pipeline");
    const fps = new Set(pl.nodes.map((n) => n.node_fp));
    const retried = retryRuns(ctx.state.runs.filter((r) => r.corpus_id === pl.corpus_id && fps.has(r.node_fp)));
    return HttpResponse.json({ retried });
  })),
  http.post("/v1/pipelines/:id/archive", route((ctx) => {
    const pl = ctx.state.pipelines.find((x) => x.id === p(ctx, "id"));
    if (!pl) return notFound("Pipeline");
    const c = ctx.state.corpora.find((x) => x.id === pl.corpus_id);
    if (c?.active_pipeline_id === pl.id)
      return err(409, "pipeline_is_active", `${pl.name} is the active pipeline. Make another pipeline active before archiving it.`);
    pl.archived = true;
    refreshProviderUsage(ctx.state);
    return HttpResponse.json(out.pipeline(ctx.state, pl, out.indexRuns(ctx.state)));
  })),

  // Evaluation
  http.get("/v1/corpora/:id/eval/datasets", route((ctx) => {
    ops.findCorpus(ctx.state, p(ctx, "id"));
    const items = ctx.state.datasets.filter((d) => d.corpus_id === p(ctx, "id")).sort((a, b) => b.created_at.localeCompare(a.created_at));
    return HttpResponse.json(page(items, ctx.request));
  })),
  http.post("/v1/corpora/:id/eval/datasets", route(async (ctx) =>
    HttpResponse.json(ev.importForm(ctx.state, p(ctx, "id"), await readForm(ctx.request)), { status: 201 }),
  )),
  http.get("/v1/eval/datasets/:id", route((ctx) => HttpResponse.json(ev.findDataset(ctx.state, p(ctx, "id"))))),
  http.get("/v1/eval/datasets/:id/items", route((ctx) => {
    ev.findDataset(ctx.state, p(ctx, "id"));
    return HttpResponse.json(page(ctx.state.datasetItems[p(ctx, "id")] ?? [], ctx.request));
  })),
  http.delete("/v1/eval/datasets/:id", route((ctx) => {
    ev.deleteDataset(ctx.state, p(ctx, "id"));
    return new HttpResponse(null, { status: 204 });
  })),
  http.get("/v1/corpora/:id/eval/runs", route((ctx) => {
    ops.findCorpus(ctx.state, p(ctx, "id"));
    const items = ctx.state.evalRuns.filter((r) => r.corpus_id === p(ctx, "id")).sort((a, b) => b.created_at.localeCompare(a.created_at)).map(ev.runOut);
    return HttpResponse.json(page(items, ctx.request));
  })),
  http.post("/v1/eval/runs", route(async (ctx) => HttpResponse.json(ev.runOut(ev.createRun(ctx.state, await body(ctx))), { status: 201 }))),
  http.get("/v1/eval/runs/:id", route((ctx) => HttpResponse.json(ev.runDetail(ctx.state, ev.findRun(ctx.state, p(ctx, "id")))))),
  http.get("/v1/eval/runs/:id/items", route((ctx) => {
    const run = ev.findRun(ctx.state, p(ctx, "id"));
    const q = new URL(ctx.request.url).searchParams;
    const list = ev.itemComparisons(ctx.state, run, q.get("a") ?? "", q.get("b") ?? "", q.get("metric") ?? "mrr", q.get("only"));
    return HttpResponse.json(page(list, ctx.request));
  })),

  // Query
  http.post("/v1/corpora/:id/query", route(async (ctx) => HttpResponse.json(query(ctx.state, p(ctx, "id"), await body(ctx))))),

  // Node runs
  http.get("/v1/node-runs", route((ctx) => {
    const q = new URL(ctx.request.url).searchParams;
    const index = out.indexRuns(ctx.state);
    let runs = ctx.state.runs;
    const corpusId = q.get("corpus_id");
    const docId = q.get("document_id");
    const pipelineId = q.get("pipeline_id");
    if (corpusId) runs = runs.filter((r) => r.corpus_id === corpusId);
    if (docId) runs = runs.filter((r) => r.document_id === docId);
    if (pipelineId) {
      const pl = ctx.state.pipelines.find((x) => x.id === pipelineId);
      const fps = new Set(pl?.nodes.map((n) => n.node_fp) ?? []);
      runs = runs.filter((r) => r.corpus_id === pl?.corpus_id && fps.has(r.node_fp));
    }
    let items = runs.map((r) => out.nodeRun(ctx.state, r, index));
    const status = q.get("status");
    if (status) items = items.filter((r) => r.status === status);
    items.sort((a, b) => (b.finished_at ?? b.run_after).localeCompare(a.finished_at ?? a.run_after));
    return HttpResponse.json(page(items, ctx.request));
  })),
  // Admin
  http.post("/v1/admin/gc", route(async (ctx) => {
    const dryRun = !((await body<{ dry_run?: boolean }>(ctx)).dry_run === false);
    return HttpResponse.json(admin.collect(ctx.state, dryRun));
  })),

  http.post("/v1/node-runs/:id/retry", route((ctx) => {
    const run = ctx.state.runs.find((r) => r.id === Number(p(ctx, "id")));
    if (!run) return notFound("Node run");
    retryRuns([run]);
    return HttpResponse.json(out.nodeRun(ctx.state, run, out.indexRuns(ctx.state)));
  })),

  // Anything else under /v1 is not implemented by the mock yet.
  http.all("/v1/*", route(({ request }) => err(404, "not_found", `The mock API has no route for ${request.method} ${new URL(request.url).pathname}.`))),
];
