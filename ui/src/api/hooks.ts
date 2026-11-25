import { useMutation, useQuery, useQueryClient, type Query } from "@tanstack/react-query";
import { ApiError, request, upload } from "./client";
import type {
  Chunk,
  GcReport,
  Corpus,
  Document,
  EvalDataset,
  EvalItem,
  EvalItemComparison,
  EvalRun,
  EvalRunCreate,
  EvalRunDetail,
  CorpusCreate,
  Health,
  MetadataSchema,
  NodeRun,
  OcrStatus,
  Page,
  Pipeline,
  PipelineConfig,
  PipelineDiff,
  PipelinePlan,
  PipelineStatus,
  Provider,
  ProviderCreate,
  ProviderTestResult,
  ProviderType,
  QueryRequest,
  QueryResponse,
  SecretSet,
  Source,
  SourceCreate,
  SourceItem,
  SourcePatch,
} from "./types";

const POLL_MS = 2000;
const OCR_POLL_MS = 3000;
const OCR_BUSY_POLL_MS = 1000;

/** Poll every 2 s while `busy(data)` is true; TanStack pauses polling when the tab is hidden. */
function pollWhile<T>(busy: (data: T) => boolean) {
  return (query: Query<T, Error, T, readonly unknown[]>) => (query.state.data && busy(query.state.data) ? POLL_MS : false);
}

const pipelineBusy = (p: { status: string }) => p.status === "building";

export const keys = {
  health: ["health"] as const,
  corpora: ["corpora"] as const,
  corpus: (id: string) => ["corpora", id] as const,
  pipelines: (corpusId: string, archived: boolean) => ["corpora", corpusId, "pipelines", { archived }] as const,
  pipeline: (id: string) => ["pipelines", id] as const,
  pipelineStatus: (id: string) => ["pipelines", id, "status"] as const,
  pipelineDiff: (a: string, b: string) => ["pipelines", a, "diff", b] as const,
  plan: (corpusId: string, config: string) => ["corpora", corpusId, "plan", config] as const,
  providers: ["providers"] as const,
  provider: (id: string) => ["providers", id] as const,
  providerTypes: ["provider-types"] as const,
  ocr: ["ocr-services"] as const,
  nodeRuns: (params: Record<string, string | undefined>) => ["node-runs", params] as const,
  documents: (corpusId: string, params: Record<string, string | undefined>) => ["corpora", corpusId, "documents", params] as const,
  document: (id: string) => ["documents", id] as const,
  parsed: (id: string, pipelineId: string) => ["documents", id, "parsed", pipelineId] as const,
  chunks: (id: string, pipelineId: string, nodeId: string) => ["documents", id, "chunks", pipelineId, nodeId] as const,
  sources: (corpusId: string) => ["corpora", corpusId, "sources"] as const,
  source: (id: string) => ["sources", id] as const,
  sourceItems: (id: string) => ["sources", id, "items"] as const,
};

export function useHealth() {
  return useQuery({ queryKey: keys.health, queryFn: () => request<Health>("/health"), refetchInterval: 15_000, retry: false });
}

export function useCorpora() {
  return useQuery({
    queryKey: keys.corpora,
    queryFn: () => request<Page<Corpus>>("/corpora", { query: { limit: 500 } }),
    refetchInterval: pollWhile<Page<Corpus>>((d) => d.items.some((c) => c.pipelines.some(pipelineBusy))),
  });
}

export function useCorpus(id: string | undefined) {
  return useQuery({
    queryKey: keys.corpus(id ?? ""),
    queryFn: () => request<Corpus>(`/corpora/${id}`),
    enabled: Boolean(id),
    refetchInterval: pollWhile<Corpus>((c) => c.pipelines.some(pipelineBusy)),
  });
}

export function usePipelines(corpusId: string | undefined, archived = false) {
  return useQuery({
    queryKey: keys.pipelines(corpusId ?? "", archived),
    queryFn: () => request<Page<Pipeline>>(`/corpora/${corpusId}/pipelines`, { query: { archived, limit: 500 } }),
    enabled: Boolean(corpusId),
    refetchInterval: pollWhile<Page<Pipeline>>((d) => d.items.some(pipelineBusy)),
  });
}

export function usePipeline(id: string | undefined) {
  return useQuery({
    queryKey: keys.pipeline(id ?? ""),
    queryFn: () => request<Pipeline>(`/pipelines/${id}`),
    enabled: Boolean(id),
    refetchInterval: pollWhile<Pipeline>(pipelineBusy),
  });
}

export function usePipelineStatus(id: string | undefined) {
  return useQuery({
    queryKey: keys.pipelineStatus(id ?? ""),
    queryFn: () => request<PipelineStatus>(`/pipelines/${id}/status`),
    enabled: Boolean(id),
    refetchInterval: pollWhile<PipelineStatus>((s) => s.nodes.some((n) => n.counts.pending + n.counts.running > 0 && s.status === "building")),
  });
}

export function useProviders() {
  return useQuery({ queryKey: keys.providers, queryFn: () => request<Page<Provider>>("/providers", { query: { limit: 500 } }) });
}

/** OCR shims and their readiness. Polls while the tab is visible, faster while a test runs. */
export function useOcr() {
  return useQuery({
    queryKey: keys.ocr,
    queryFn: () => request<OcrStatus>("/ocr-services"),
    retry: false,
    refetchInterval: ({ state }) => {
      if (state.error instanceof ApiError && state.error.status === 401) return false; // the token prompt takes over
      const busy = state.data?.scanning || state.data?.services.some((s) => s.state === "checking" || s.state === "looking" || s.rechecking);
      return busy ? OCR_BUSY_POLL_MS : OCR_POLL_MS;
    },
  });
}

export function useOcrScan() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => request<OcrStatus>("/ocr-services/scan", { method: "POST" }),
    onSuccess: (status) => qc.setQueryData(keys.ocr, status),
  });
}

export function useOcrCheck(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => request<OcrStatus>(`/providers/${id}/ocr-check`, { method: "POST" }),
    onSuccess: (status) => qc.setQueryData(keys.ocr, status),
  });
}

export function useProvider(id: string | undefined) {
  return useQuery({ queryKey: keys.provider(id ?? ""), queryFn: () => request<Provider>(`/providers/${id}`), enabled: Boolean(id) });
}

export function useProviderTypes() {
  return useQuery({ queryKey: keys.providerTypes, queryFn: () => request<Page<ProviderType>>("/provider-types"), staleTime: Infinity });
}

export function useNodeRuns(params: Record<string, string | undefined>, opts: { enabled?: boolean } = {}) {
  return useQuery({
    queryKey: keys.nodeRuns(params),
    queryFn: () => request<Page<NodeRun>>("/node-runs", { query: { limit: 500, ...params } }),
    enabled: opts.enabled ?? true,
    refetchInterval: pollWhile<Page<NodeRun>>((d) => d.items.some((r) => r.status === "pending" || r.status === "running")),
  });
}

// ---------- Documents ----------

const docBusy = (d: Document) => d.status === "deleting" || d.ingestion.some((i) => i.state === "pending" || i.state === "running");

export function useDocuments(corpusId: string, params: Record<string, string | undefined>) {
  return useQuery({
    queryKey: keys.documents(corpusId, params),
    queryFn: () => request<Page<Document>>(`/corpora/${corpusId}/documents`, { query: { limit: 50, ...params } }),
    enabled: Boolean(corpusId),
    placeholderData: (prev) => prev,
    refetchInterval: pollWhile<Page<Document>>((d) => d.items.some(docBusy)),
  });
}

export function useDocument(id: string | undefined) {
  return useQuery({
    queryKey: keys.document(id ?? ""),
    queryFn: () => request<Document>(`/documents/${id}`),
    enabled: Boolean(id),
    refetchInterval: pollWhile<Document>(docBusy),
  });
}

export function useParsed(id: string, pipelineId: string | null) {
  return useQuery({
    queryKey: keys.parsed(id, pipelineId ?? ""),
    queryFn: () => request<Record<string, unknown>>(`/documents/${id}/parsed`, { query: { pipeline_id: pipelineId } }),
    enabled: Boolean(pipelineId),
    retry: false,
  });
}

export function useChunks(id: string, pipelineId: string | null, nodeId: string | null) {
  return useQuery({
    queryKey: keys.chunks(id, pipelineId ?? "", nodeId ?? ""),
    queryFn: () => request<Chunk[]>(`/documents/${id}/chunks`, { query: { pipeline_id: pipelineId, node_id: nodeId } }),
    enabled: Boolean(pipelineId),
  });
}

/** Invalidate document lists, the document, its runs and the corpus counts after a change. */
export function useInvalidateDocuments() {
  const qc = useQueryClient();
  return (doc?: Document) => {
    if (doc) qc.setQueryData(keys.document(doc.id), doc);
    void qc.invalidateQueries({ queryKey: ["corpora"] });
    void qc.invalidateQueries({ queryKey: ["documents"] });
    void qc.invalidateQueries({ queryKey: ["node-runs"] });
    void qc.invalidateQueries({ queryKey: ["pipelines"] });
  };
}

export function usePatchDocument(id: string) {
  const invalidate = useInvalidateDocuments();
  return useMutation({
    mutationFn: (body: { metadata?: Record<string, unknown>; attributes?: Record<string, unknown> }) =>
      request<Document>(`/documents/${id}`, { method: "PATCH", body }),
    onSuccess: invalidate,
  });
}

export function useDeleteDocument() {
  const invalidate = useInvalidateDocuments();
  return useMutation({
    mutationFn: (id: string) => request<Document>(`/documents/${id}`, { method: "DELETE" }),
    onSuccess: invalidate,
  });
}

export function useRetryRun() {
  const invalidate = useInvalidateDocuments();
  return useMutation({
    mutationFn: (runId: number) => request<NodeRun>(`/node-runs/${runId}/retry`, { method: "POST" }),
    onSuccess: () => invalidate(),
  });
}

/** Invalidate everything that a change to a corpus's pipelines can affect. */
function useInvalidateCorpus() {
  const qc = useQueryClient();
  return () => {
    void qc.invalidateQueries({ queryKey: ["corpora"] });
    void qc.invalidateQueries({ queryKey: ["pipelines"] });
    void qc.invalidateQueries({ queryKey: ["node-runs"] });
    void qc.invalidateQueries({ queryKey: keys.providers });
  };
}

export function useRetryFailed() {
  const invalidate = useInvalidateCorpus();
  return useMutation({
    mutationFn: (pipelineId: string) => request<{ retried: number }>(`/pipelines/${pipelineId}/retry-failed`, { method: "POST" }),
    onSuccess: invalidate,
  });
}

export function useArchivePipeline() {
  const invalidate = useInvalidateCorpus();
  return useMutation({
    mutationFn: (pipelineId: string) => request<Pipeline>(`/pipelines/${pipelineId}/archive`, { method: "POST" }),
    onSuccess: invalidate,
  });
}

export function useSetActivePipeline() {
  const invalidate = useInvalidateCorpus();
  return useMutation({
    mutationFn: ({ corpusId, pipelineId, force }: { corpusId: string; pipelineId: string; force?: boolean }) =>
      request<Corpus>(`/corpora/${corpusId}/active-pipeline`, { method: "PUT", body: { pipeline_id: pipelineId, force } }),
    onSuccess: invalidate,
  });
}

// ---------- Providers ----------

function useInvalidateProviders() {
  const qc = useQueryClient();
  return (p?: Provider) => {
    if (p) qc.setQueryData(keys.provider(p.id), p);
    void qc.invalidateQueries({ queryKey: keys.providers });
    void qc.invalidateQueries({ queryKey: keys.ocr });
  };
}

export function useCreateProvider() {
  const invalidate = useInvalidateProviders();
  return useMutation({
    mutationFn: (body: ProviderCreate) => request<Provider>("/providers", { method: "POST", body }),
    onSuccess: invalidate,
  });
}

export function usePatchProvider(id: string) {
  const invalidate = useInvalidateProviders();
  return useMutation({
    mutationFn: (config: Record<string, unknown>) => request<Provider>(`/providers/${id}`, { method: "PATCH", body: { config } }),
    onSuccess: invalidate,
  });
}

export function useDeleteProvider() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => request<void>(`/providers/${id}`, { method: "DELETE" }),
    onSuccess: (_, id) => {
      qc.removeQueries({ queryKey: keys.provider(id) });
      void qc.invalidateQueries({ queryKey: keys.providers });
      void qc.invalidateQueries({ queryKey: keys.ocr });
    },
  });
}

export function useSetSecret(id: string) {
  const invalidate = useInvalidateProviders();
  return useMutation({
    mutationFn: ({ key, body }: { key: string; body: SecretSet }) =>
      request<Provider>(`/providers/${id}/secrets/${encodeURIComponent(key)}`, { method: "PUT", body }),
    onSuccess: invalidate,
  });
}

export function useDeleteSecret(id: string) {
  const invalidate = useInvalidateProviders();
  return useMutation({
    mutationFn: (key: string) => request<Provider>(`/providers/${id}/secrets/${encodeURIComponent(key)}`, { method: "DELETE" }),
    onSuccess: invalidate,
  });
}

export function useTestProvider(id: string) {
  const qc = useQueryClient();
  return useMutation({
    // A deep test checks a sample document or a vector store's live behavior.
    mutationFn: (deep?: boolean) => request<ProviderTestResult>(`/providers/${id}/test`, { method: "POST", query: deep ? { deep: "true" } : undefined }),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.provider(id) }),
  });
}

// ---------- Sources ----------

const sourceBusy = (s: Source) => s.status === "syncing";

export function useSources(corpusId: string) {
  return useQuery({
    queryKey: keys.sources(corpusId),
    queryFn: () => request<Page<Source>>(`/corpora/${corpusId}/sources`, { query: { limit: 500 } }),
    enabled: Boolean(corpusId),
    refetchInterval: pollWhile<Page<Source>>((d) => d.items.some(sourceBusy)),
  });
}

export function useSource(id: string | undefined) {
  return useQuery({
    queryKey: keys.source(id ?? ""),
    queryFn: () => request<Source>(`/sources/${id}`),
    enabled: Boolean(id),
    refetchInterval: pollWhile<Source>(sourceBusy),
  });
}

export function useSourceItems(id: string | undefined) {
  return useQuery({
    queryKey: keys.sourceItems(id ?? ""),
    queryFn: () => request<Page<SourceItem>>(`/sources/${id}/items`, { query: { limit: 500 } }),
    enabled: Boolean(id),
  });
}

function useInvalidateSources(corpusId: string) {
  const qc = useQueryClient();
  return (source?: Source) => {
    if (source) qc.setQueryData(keys.source(source.id), source);
    void qc.invalidateQueries({ queryKey: keys.sources(corpusId) });
    void qc.invalidateQueries({ queryKey: keys.providers });
  };
}

export function useCreateSource(corpusId: string) {
  const invalidate = useInvalidateSources(corpusId);
  return useMutation({
    mutationFn: (body: SourceCreate) => request<Source>(`/corpora/${corpusId}/sources`, { method: "POST", body }),
    onSuccess: (source) => invalidate(source),
  });
}

export function usePatchSource(id: string, corpusId: string) {
  const invalidate = useInvalidateSources(corpusId);
  return useMutation({
    mutationFn: (body: SourcePatch) => request<Source>(`/sources/${id}`, { method: "PATCH", body }),
    onSuccess: (source) => invalidate(source),
  });
}

export function useSyncSource(corpusId: string) {
  const invalidate = useInvalidateSources(corpusId);
  return useMutation({
    mutationFn: (id: string) => request<Source>(`/sources/${id}/sync`, { method: "POST" }),
    onSuccess: (source) => invalidate(source),
  });
}

export function useDeleteSource(corpusId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => request<void>(`/sources/${id}`, { method: "DELETE" }),
    onSuccess: (_, id) => {
      qc.removeQueries({ queryKey: keys.source(id) });
      qc.removeQueries({ queryKey: keys.sourceItems(id) });
      void qc.invalidateQueries({ queryKey: keys.sources(corpusId) });
      void qc.invalidateQueries({ queryKey: keys.providers });
    },
  });
}

// ---------- Corpora ----------

function useInvalidateCorpora() {
  const qc = useQueryClient();
  return (c: Corpus) => {
    qc.setQueryData(keys.corpus(c.id), c);
    void qc.invalidateQueries({ queryKey: keys.corpora });
  };
}

export function useCreateCorpus() {
  const invalidate = useInvalidateCorpora();
  return useMutation({
    mutationFn: (body: CorpusCreate) => request<Corpus>("/corpora", { method: "POST", body }),
    onSuccess: invalidate,
  });
}

export function useRenameCorpus(id: string) {
  const invalidate = useInvalidateCorpora();
  return useMutation({
    mutationFn: (name: string) => request<Corpus>(`/corpora/${id}`, { method: "PATCH", body: { name } }),
    onSuccess: invalidate,
  });
}

export function useUpdateSchema(id: string) {
  const invalidate = useInvalidateCorpora();
  return useMutation({
    mutationFn: (schema: MetadataSchema) => request<Corpus>(`/corpora/${id}/metadata-schema`, { method: "PATCH", body: schema }),
    onSuccess: invalidate,
  });
}

// ---------- Pipelines: plan, create, diff ----------

/** The dry-run plan for a configuration (always 200, problems included). Keyed by its JSON. */
export function usePlan(corpusId: string, config: PipelineConfig | null) {
  const text = config ? JSON.stringify(config) : "";
  return useQuery({
    queryKey: keys.plan(corpusId, text),
    queryFn: ({ signal }) => request<PipelinePlan>(`/corpora/${corpusId}/pipelines`, { method: "POST", body: config, query: { dry_run: true }, signal }),
    enabled: Boolean(corpusId && config),
    placeholderData: (prev) => prev,
    staleTime: 5000,
  });
}

export function useCreatePipeline(corpusId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (config: PipelineConfig) => request<Pipeline>(`/corpora/${corpusId}/pipelines`, { method: "POST", body: config }),
    onSuccess: (p) => {
      qc.setQueryData(keys.pipeline(p.id), p);
      void qc.invalidateQueries({ queryKey: ["corpora"] });
      void qc.invalidateQueries({ queryKey: ["pipelines"] });
      void qc.invalidateQueries({ queryKey: keys.providers });
    },
  });
}

export function usePipelineDiff(a: string | null, b: string | null) {
  return useQuery({
    queryKey: keys.pipelineDiff(a ?? "", b ?? ""),
    queryFn: () => request<PipelineDiff>(`/pipelines/${a}/diff`, { query: { against: b } }),
    enabled: Boolean(a && b && a !== b),
  });
}

// ---------- Query ----------

/**
 * Results for a submitted query. The submitted query lives in the URL, so it is keyed on the
 * request and re-runs on reload; it never refetches on its own (a query may call paid providers).
 */
export function useQueryResults(corpusId: string, body: QueryRequest | null) {
  return useQuery({
    queryKey: ["query", corpusId, body],
    queryFn: ({ signal }) => request<QueryResponse>(`/corpora/${corpusId}/query`, { method: "POST", body: body!, signal }),
    enabled: Boolean(corpusId && body?.query.trim()),
    staleTime: Infinity,
    gcTime: 5 * 60_000,
    retry: false,
    refetchOnWindowFocus: false,
  });
}

// ---------- Evaluation ----------

const runBusy = (r: EvalRun) => r.status === "pending" || r.status === "running";

export function useDatasets(corpusId: string) {
  return useQuery({ queryKey: ["corpora", corpusId, "eval", "datasets"], queryFn: () => request<Page<EvalDataset>>(`/corpora/${corpusId}/eval/datasets`, { query: { limit: 500 } }), enabled: Boolean(corpusId) });
}

export function useDataset(id: string | undefined) {
  return useQuery({ queryKey: ["eval", "datasets", id], queryFn: () => request<EvalDataset>(`/eval/datasets/${id}`), enabled: Boolean(id) });
}

export function useDatasetItems(id: string | undefined) {
  return useQuery({ queryKey: ["eval", "datasets", id, "items"], queryFn: () => request<Page<EvalItem>>(`/eval/datasets/${id}/items`, { query: { limit: 500 } }), enabled: Boolean(id) });
}

export function useEvalRuns(corpusId: string) {
  return useQuery({
    queryKey: ["corpora", corpusId, "eval", "runs"],
    queryFn: () => request<Page<EvalRun>>(`/corpora/${corpusId}/eval/runs`, { query: { limit: 500 } }),
    enabled: Boolean(corpusId),
    refetchInterval: pollWhile<Page<EvalRun>>((d) => d.items.some(runBusy)),
  });
}

export function useEvalRun(id: string | undefined) {
  return useQuery({
    queryKey: ["eval", "runs", id],
    queryFn: () => request<EvalRunDetail>(`/eval/runs/${id}`),
    enabled: Boolean(id),
    refetchInterval: pollWhile<EvalRunDetail>(runBusy),
  });
}

export function useRunItems(runId: string | undefined, a: string | null, b: string | null, metric: string, only: string | null) {
  return useQuery({
    queryKey: ["eval", "runs", runId, "items", a, b, metric, only],
    queryFn: () => request<Page<EvalItemComparison>>(`/eval/runs/${runId}/items`, { query: { a, b, metric, only, limit: 500 } }),
    enabled: Boolean(runId && a && b && a !== b),
  });
}

function useInvalidateEval() {
  const qc = useQueryClient();
  return () => {
    void qc.invalidateQueries({ queryKey: ["corpora"] });
    void qc.invalidateQueries({ queryKey: ["eval"] });
  };
}

export function useImportDataset(corpusId: string) {
  const invalidate = useInvalidateEval();
  return useMutation({
    mutationFn: ({ name, file }: { name: string; file: File }) => {
      const form = new FormData();
      form.set("name", name);
      form.set("file", file, file.name);
      return upload<EvalDataset>(`/corpora/${corpusId}/eval/datasets`, form).then((r) => r.body);
    },
    onSuccess: invalidate,
  });
}

export function useDeleteDataset() {
  const invalidate = useInvalidateEval();
  return useMutation({ mutationFn: (id: string) => request<void>(`/eval/datasets/${id}`, { method: "DELETE" }), onSuccess: invalidate });
}

export function useCreateRun() {
  const invalidate = useInvalidateEval();
  return useMutation({ mutationFn: (body: EvalRunCreate) => request<EvalRun>("/eval/runs", { method: "POST", body }), onSuccess: invalidate });
}

// ---------- Admin ----------

function useInvalidateAdmin() {
  const qc = useQueryClient();
  return () => {
    void qc.invalidateQueries({ queryKey: ["corpora"] });
    void qc.invalidateQueries({ queryKey: ["pipelines"] });
    void qc.invalidateQueries({ queryKey: ["node-runs"] });
    void qc.invalidateQueries({ queryKey: keys.providers });
  };
}

/** Preview what garbage collection would remove (changes nothing). */
export function useGcPreview() {
  const invalidate = useInvalidateAdmin();
  return useMutation({
    mutationFn: () => request<GcReport>("/admin/gc", { method: "POST", body: { dry_run: true } }),
    onSuccess: invalidate,
  });
}

/** Run garbage collection for real. */
export function useGcRun() {
  const invalidate = useInvalidateAdmin();
  return useMutation({
    mutationFn: () => request<GcReport>("/admin/gc", { method: "POST", body: { dry_run: false } }),
    onSuccess: invalidate,
  });
}

