// Type-level check that the backend matches the hand-written API contract (IMPLEMENTATION_PLAN.md M11).
//
// `generated/openapi.ts` is produced from the backend's OpenAPI document by `npm run types:api`.
// Every response the backend declares must be assignable to the type the UI expects, and every
// request body the UI sends must be assignable to what the backend accepts. A mismatch is a
// compile error in this file, so `npm run typecheck` (and CI) fails on drift.
//
// This file has no runtime content and is never imported.
import type { components } from "./generated/openapi";
import type * as T from "./types";

type S = components["schemas"];

/** Compiles only when `From` is assignable to `To`. */
type Assignable<From extends To, To> = [From, To];

// ---- responses: backend -> UI ----------------------------------------------------------------
export type ResponseChecks = [
  Assignable<S["Health"], T.Health>,
  Assignable<S["ProviderType"], T.ProviderType>,
  Assignable<S["Page_ProviderType_"], T.Page<T.ProviderType>>,
  Assignable<S["Provider"], T.Provider>,
  Assignable<S["Page_Provider_"], T.Page<T.Provider>>,
  Assignable<S["ProviderTestResult"], T.ProviderTestResult>,
  Assignable<S["MetadataSchema-Output"], T.MetadataSchema>,
  Assignable<S["PipelineSummary"], T.PipelineSummary>,
  Assignable<S["Corpus"], T.Corpus>,
  Assignable<S["Page_Corpus_"], T.Page<T.Corpus>>,
  Assignable<S["DocumentIngestion"], T.DocumentIngestion>,
  Assignable<S["Document"], T.Document>,
  Assignable<S["Page_Document_"], T.Page<T.Document>>,
  Assignable<S["Chunk"], T.Chunk>,
  Assignable<S["PipelinePlan"], T.PipelinePlan>,
  Assignable<S["Pipeline"], T.Pipeline>,
  Assignable<S["Page_Pipeline_"], T.Page<T.Pipeline>>,
  Assignable<S["PipelineStatus"], T.PipelineStatus>,
  Assignable<S["PipelineDiff"], T.PipelineDiff>,
  Assignable<S["NodeRun"], T.NodeRun>,
  Assignable<S["Page_NodeRun_"], T.Page<T.NodeRun>>,
  Assignable<S["QueryResponse"], T.QueryResponse>,
  Assignable<S["EvalItem"], T.EvalItem>,
  Assignable<S["Page_EvalItem_"], T.Page<T.EvalItem>>,
  Assignable<S["EvalDataset"], T.EvalDataset>,
  Assignable<S["Page_EvalDataset_"], T.Page<T.EvalDataset>>,
  Assignable<S["EvalRun"], T.EvalRun>,
  Assignable<S["Page_EvalRun_"], T.Page<T.EvalRun>>,
  Assignable<S["EvalRunDetail"], T.EvalRunDetail>,
  Assignable<S["EvalItemComparison"], T.EvalItemComparison>,
  Assignable<S["Page_EvalItemComparison_"], T.Page<T.EvalItemComparison>>,
  Assignable<S["GcReport"], T.GcReport>,
];

// ---- request bodies: UI -> backend -----------------------------------------------------------
export type RequestChecks = [
  Assignable<T.ProviderCreate, S["ProviderCreate"]>,
  Assignable<T.ProviderPatch, S["ProviderPatch"]>,
  Assignable<T.CorpusCreate, S["CorpusCreate"]>,
  Assignable<T.CorpusPatch, S["CorpusPatch"]>,
  Assignable<T.MetadataSchema, S["MetadataSchema-Input"]>,
  Assignable<T.SecretSet, S["SecretSet"]>,
  Assignable<T.QueryRequest, S["QueryRequest"]>,
  Assignable<T.EvalRunCreate, S["EvalRunCreate"]>,
];
