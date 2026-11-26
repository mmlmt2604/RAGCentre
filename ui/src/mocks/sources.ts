// Mock sync-source operations: the behaviour of the backend's sources router and sync engine,
// close enough that the UI meets the same errors (IMPLEMENTATION_PLAN.md §11).
import type { Source, SourceCreate, SourceItem, SourcePatch } from "../api/types";
import { scopeProblem, scopeSchemaFor } from "../features/sources/scopeModel";
import { uuidFrom } from "./hash";
import { findCorpus, findProvider, MockError } from "./providers";
import { now, type MockState } from "./store";

export function findSource(state: MockState, id: string): Source {
  const s = state.sources.find((x) => x.id === id);
  if (!s) throw new MockError(404, "not_found", "Source was not found.");
  return s;
}

export function listSources(state: MockState, corpusId: string): Source[] {
  findCorpus(state, corpusId);
  return state.sources.filter((s) => s.corpus_id === corpusId).sort((a, b) => a.created_at.localeCompare(b.created_at));
}

export function createSource(state: MockState, corpusId: string, body: SourceCreate): Source {
  findCorpus(state, corpusId);
  const provider = findProvider(state, body.provider_id);
  if (provider.kind !== "connector")
    throw new MockError(422, "validation_error", `Provider ${provider.name} is a ${provider.kind}, not a connector.`);
  const name = body.name.trim();
  if (!name) throw new MockError(422, "validation_error", "Give the source a name.");
  if (state.sources.some((s) => s.corpus_id === corpusId && s.name === name))
    throw new MockError(409, "conflict", `A source named '${name}' already exists in this corpus.`);
  const problem = scopeProblem(JSON.stringify(body.scope ?? {}), scopeSchemaFor(state.providerTypes, provider.type));
  if (problem) throw new MockError(422, "validation_error", problem);
  const schedule_seconds = body.schedule_seconds ?? 3600;
  const source: Source = {
    id: uuidFrom(`source:${corpusId}:${name}:${Date.now()}`),
    corpus_id: corpusId,
    provider_id: provider.id,
    name,
    scope: body.scope ?? {},
    schedule_seconds,
    cursor: null,
    status: "idle",
    next_run_at: schedule_seconds > 0 ? now() : null,
    last_run_at: null,
    last_error: null,
    metadata_template: body.metadata_template ?? {},
    attributes_template: body.attributes_template ?? {},
    created_at: now(),
    updated_at: now(),
  };
  state.sources.push(source);
  return source;
}

export function patchSource(state: MockState, id: string, body: SourcePatch): Source {
  const s = findSource(state, id);
  if (body.scope !== null && body.scope !== undefined) {
    const provider = findProvider(state, s.provider_id);
    const problem = scopeProblem(JSON.stringify(body.scope), scopeSchemaFor(state.providerTypes, provider.type));
    if (problem) throw new MockError(422, "validation_error", problem);
    s.scope = body.scope;
  }
  if (body.metadata_template !== null && body.metadata_template !== undefined) s.metadata_template = body.metadata_template;
  if (body.attributes_template !== null && body.attributes_template !== undefined) s.attributes_template = body.attributes_template;
  if (body.schedule_seconds !== null && body.schedule_seconds !== undefined) {
    s.schedule_seconds = body.schedule_seconds;
    if (s.status === "idle") s.next_run_at = body.schedule_seconds > 0 ? now() : null;
  }
  s.updated_at = now();
  return s;
}

export function deleteSource(state: MockState, id: string): void {
  findSource(state, id);
  state.sources = state.sources.filter((s) => s.id !== id);
  state.sourceItems = state.sourceItems.filter((i) => i.source_id !== id);
}

/** In the mock, a manual sync is instant: the run is recorded and the source returns to idle. */
export function syncSource(state: MockState, id: string): Source {
  const s = findSource(state, id);
  s.last_run_at = now();
  s.last_error = null;
  s.updated_at = now();
  if (s.schedule_seconds > 0) s.next_run_at = now();
  return s;
}

export function listItems(state: MockState, sourceId: string): SourceItem[] {
  findSource(state, sourceId);
  return state.sourceItems.filter((i) => i.source_id === sourceId).sort((a, b) => a.remote_id.localeCompare(b.remote_id));
}
