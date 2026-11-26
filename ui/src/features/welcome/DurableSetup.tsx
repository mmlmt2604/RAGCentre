import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, CircleX } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useMatch, useSearchParams } from "react-router";
import { useCorpus, useHealth, usePipeline, useProviders } from "../../api/hooks";
import type { Document, OcrStatus, Provider, QueryResponse } from "../../api/types";
import { Button } from "../../components/Button";
import { Field, TextInput } from "../../components/Field";
import { ErrorState } from "../../components/States";
import { useAuth, useCan, useTransport } from "../auth/context";
import { initialSecretDrafts, ProviderCredentials } from "../providers/ProviderCredentials";
import type { SecretDraft } from "../providers/SecretInput";
import { choicesFromDraft, defaultChoices, guideDetails, setupDraft, validateStep, type Choices } from "./model";
import type { SetupDraft, SetupOperation, SetupState, SetupTask } from "./setup";
import { EmbedStep, Head, NameStep, ParseStep, StoreStep, TaskError } from "./WelcomeSetup";

const LABELS: Record<SetupTask, string> = { parser: "Set up document reading", embedder: "Set up embeddings", store: "Set up vector storage", corpus: "Create the corpus", pipeline: "Create the pipeline", active: "Select the active pipeline", check_services: "Verify saved services" };
const ORDER: SetupTask[] = ["parser", "embedder", "store", "corpus", "pipeline", "active", "check_services"];

/** The modal and the permanent team settings page share this exact flow. */
export function DurableSetup({ embedded = false }: { embedded?: boolean }) {
  const auth = useAuth();
  const manage = useCan("manage");
  const { request } = useTransport();
  const atHome = useMatch("/");
  const [params, setParams] = useSearchParams();
  const [closed, setClosed] = useState(false);
  const query = useQuery({ queryKey: ["team-setup", auth.teamId], queryFn: () => request<SetupState>(`/teams/${auth.teamId}/setup`), enabled: auth.mode === "local" && manage && Boolean(auth.teamId) });
  const asked = params.has("setup");
  const initial = query.data;
  const auto = atHome && initial && !initial.draft.draft.dismissed && !initial.readiness.ready && (!initial.readiness.corpus_id || initial.operation);
  if (!manage || auth.mode !== "local" || !auth.teamId || closed || !(embedded || asked || auto)) return null;
  const close = () => {
    setClosed(true);
    if (asked) { const next = new URLSearchParams(params); next.delete("setup"); setParams(next, { replace: true }); }
  };
  if (query.isError) return <div className="page-pad"><ErrorState error={query.error} onRetry={() => void query.refetch()} /></div>;
  if (!initial) return <div className="page-pad" role="status">Loading team setup…</div>;
  return <DurableFrame embedded={embedded} initial={initial} teamId={auth.teamId} onClose={close} />;
}

function DurableFrame({ embedded, initial, teamId, onClose }: { embedded: boolean; initial: SetupState; teamId: string; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { if (!embedded && dialog.current && !dialog.current.open) { if (dialog.current.showModal) dialog.current.showModal(); else dialog.current.setAttribute("open", ""); } }, [embedded]);
  const content = <DurableFlow initial={initial} teamId={teamId} onClose={onClose} />;
  return embedded ? <section className="wl wl-durable wl-embedded" aria-label="Team setup">{content}</section> : <dialog ref={dialog} className="rc-dialog wl wl-durable" aria-label="Team setup" onCancel={(e) => { e.preventDefault(); dialog.current?.querySelector<HTMLButtonElement>("[data-setup-dismiss]")?.click(); }}>{content}</dialog>;
}

function DurableFlow({ initial, teamId, onClose }: { initial: SetupState; teamId: string; onClose: () => void }) {
  const { request, isCurrent } = useTransport();
  const qc = useQueryClient();
  const platform = useCan("platform");
  const providers = useProviders();
  const saved = useRef(initial);
  const [op, setOp] = useState(initial.operation);
  const live = useQuery({ queryKey: ["team-setup", teamId], queryFn: () => request<SetupState>(`/teams/${teamId}/setup`), initialData: initial, refetchInterval: op ? 2000 : false });
  const [mode, setMode] = useState<"trial" | "custom">(initial.operation?.draft.mode ?? initial.draft.draft.mode);
  const [c, setC] = useState<Choices>(() => choicesFromDraft(initial.operation?.draft ?? initial.draft.draft));
  const [step, setStep] = useState(initial.operation ? 5 : initial.draft.draft.choice_step);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [working, setWorking] = useState<SetupTask>();
  const pane = useRef<HTMLDivElement>(null);
  const ocr = useQuery({ queryKey: ["ocr-services"], queryFn: () => request<OcrStatus>("/ocr-services"), enabled: platform && mode === "custom" && step === 1, retry: false });
  const available = providers.data?.items ?? [];
  const guide = c.parser.mode === "guide" && ocr.data ? guideDetails(c.parser, ocr.data, available) : undefined;
  const set = (patch: Partial<Choices>) => { setC((old) => ({ ...old, ...patch })); setErrors({}); setError(undefined); };
  useEffect(() => { pane.current?.querySelector<HTMLElement>("[data-autofocus], input:checked, input:not([type=file]), button")?.focus({ preventScroll: true }); }, [step]);

  const save = async (draft: SetupDraft) => {
    const state = await request<SetupState>(`/teams/${teamId}/setup-draft`, { method: "PUT", body: { expected_version: saved.current.draft.version, draft } });
    if (!isCurrent()) throw new DOMException("The team changed.", "AbortError");
    saved.current = state;
    qc.setQueryData(["team-setup", teamId], state);
    return state;
  };
  const dismiss = async () => {
    setBusy(true);
    try {
      const draft = op ? { ...saved.current.draft.draft, dismissed: true } : setupDraft(c, mode, available, Math.min(step, 4), true);
      await save(draft);
      if (isCurrent()) onClose();
    } catch (e) { if (isCurrent()) setError(e); }
    finally { if (isCurrent()) setBusy(false); }
  };

  const run = async (operation: SetupOperation) => {
    let current = operation;
    setBusy(true); setError(undefined);
    try {
      for (const task of ORDER) {
        if (current.steps.find((s) => s.step === task)?.state === "succeeded" && task !== "check_services") continue;
        setWorking(task);
        current = await request<SetupOperation>(`/teams/${teamId}/setup-operations/${current.id}/steps/${task}`, { method: "POST" });
        if (!isCurrent()) return;
        setOp(current);
        if (current.steps.some((s) => s.state === "failed")) break;
      }
      void qc.invalidateQueries({ queryKey: ["team-setup", teamId] });
      void qc.invalidateQueries({ queryKey: ["corpora"] });
      void qc.invalidateQueries({ queryKey: ["providers"] });
    } catch (e) {
      if (!isCurrent()) return;
      setError(e);
      // An interrupted response can follow a committed creation. Read its durable result before retry.
      try { const resumed = await request<SetupOperation>(`/teams/${teamId}/setup-operations/${current.id}`); if (isCurrent()) setOp(resumed); } catch { /* The retry control also re-reads server state. */ }
    } finally { if (isCurrent()) { setWorking(undefined); setBusy(false); } }
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    if (step === 5 && op) { await run(op); return; }
    const choices = step === 1 && guide?.manualUrl !== undefined ? { ...c, parser: { mode: "url" as const, url: guide.manualUrl.trim() } } : c;
    const errs = validateStep(step, choices, new Set());
    setErrors(errs);
    if (Object.keys(errs).length) return;
    setBusy(true); setError(undefined);
    try {
      const next = step === 0 ? (mode === "trial" ? 4 : 1) : Math.min(step + 1, 4);
      const state = await save(setupDraft(choices, mode, available, next));
      if (step < 4) { setC(choices); setStep(next); }
      else {
        const operation = await request<SetupOperation>(`/teams/${teamId}/setup-operations`, { method: "POST", body: { draft_version: state.draft.version, input_hash: state.draft_hash, idempotency_key: state.draft_hash } });
        if (!isCurrent()) return;
        setOp(operation); setStep(5);
        await run(operation);
      }
    } catch (e) { if (isCurrent()) setError(e); }
    finally { if (isCurrent()) setBusy(false); }
  };

  const created = Boolean(op?.steps.find((s) => s.step === "active" && s.state === "succeeded"));
  const checked = Boolean(op?.steps.find((s) => s.step === "check_services" && s.state === "succeeded") && live.data.readiness.services);
  const corpusId = op?.steps.find((s) => s.step === "corpus")?.resource_id;
  const pipelineId = op?.steps.find((s) => s.step === "pipeline")?.resource_id;
  const currentTitle = step === 0 ? "Choose a setup path" : step < 5 ? `Setup choice ${step} of 4` : "Setup progress";
  return (
    <div className="wl-grid">
      <aside className="wl-chain" aria-label="Setup tasks">
        <h2 className="wl-corpus__name">Team setup</h2>
        <ol className="wl-nodes">{ORDER.map((task) => { const state = op?.steps.find((s) => s.step === task)?.state; return <li key={task} className="wl-node" data-state={working === task ? "busy" : state === "succeeded" ? "done" : state === "failed" ? "failed" : "todo"}><div className="wl-node__box"><span className="wl-node__head"><span className="wl-node__label">{LABELS[task]}</span>{state === "succeeded" ? <Check size={14} aria-label="Done" /> : state === "failed" ? <CircleX size={14} aria-label="Needs attention" /> : working === task ? <span className="rc-spinner" aria-label="Working" /> : null}</span></div></li>; })}</ol>
        <p className="wl-note">Your choices and resource references are saved for this account and team. Credentials stay in the write-only service settings.</p>
      </aside>
      <div className="wl-main">
        <form onSubmit={(event) => void submit(event)} noValidate>
          <div ref={pane} className="wl-pane" aria-label={currentTitle}>
            {step === 0 ? <><Head title="Reach your first useful search">Choose built-in components for a local trial, or connect services you already run.</Head><div className="wl-opts" role="radiogroup" aria-label="Setup path"><label className="wl-opt wl-opt__row" data-on={mode === "trial" || undefined}><input type="radio" name="setup-mode" checked={mode === "trial"} onChange={() => { setMode("trial"); setC({ ...defaultChoices([], []), corpus: c.corpus }); }} /><span className="wl-opt__text"><strong>Try with built-in components</strong><span className="wl-opt__desc">Text, Markdown and HTML · hash embeddings · SQLite. Demonstrates retrieval mechanics; hash embeddings do not provide semantic-model quality.</span></span></label><label className="wl-opt wl-opt__row" data-on={mode === "custom" || undefined}><input type="radio" name="setup-mode" checked={mode === "custom"} onChange={() => { setMode("custom"); setC({ ...defaultChoices(available, []), corpus: c.corpus }); }} /><span className="wl-opt__text"><strong>Connect my services</strong><span className="wl-opt__desc">Select existing providers or configure your running parser, embedding model and vector store. Save credentials and verify connections before adding documents.</span></span></label></div></> : null}
            {step === 1 ? <ParseStep c={c} set={set} errors={errors} providers={available} ocrStatus={ocr.data} textParser="builtin-text" animateArrival={false} onAnimationEnd={() => {}} onFound={(service) => set({ parser: { mode: "shim", service } })} /> : null}
            {step === 2 ? <EmbedStep c={c} set={set} errors={errors} providers={available} /> : null}
            {step === 3 ? <StoreStep c={c} set={set} errors={errors} providers={available} /> : null}
            {step === 4 ? <NameStep c={c} set={set} errors={errors} /> : null}
            {step === 5 ? <><Head title={checked ? "Verify your first search" : created ? "Verify saved services" : "Build your pipeline"}>{checked ? "Setup is complete when an indexed document returns a real retrieved chunk." : "Completed tasks are saved on the server and reused after a refresh or retry."}</Head><ol className="wl-log" aria-live="polite">{op?.steps.map((task) => <li className="wl-log__line" key={task.step} data-state={task.state}><span>{LABELS[task.step]}</span><span>{working === task.step ? "Working…" : task.state === "succeeded" ? "Done" : task.state === "failed" ? "Needs attention" : task.state === "running" ? "Running; refresh to check progress" : "Waiting"}</span>{task.error_code ? <span className="mono">{task.error_code}</span> : null}</li>)}</ol></> : null}
            {step === 5 && created ? <ul className="wl-files" aria-label="Saved service readiness">{live.data.readiness.providers.map((provider) => <li key={provider.id}><Link to={`/providers/${provider.id}`}>{provider.name}</Link><span>{provider.verified ? "Connection verified" : Object.values(provider.credentials).some((set) => !set) ? "Credentials may be needed; verify the saved connection" : "Connection check needed"}</span></li>)}</ul> : null}
            {error ? <TaskError error={error} /> : null}
          </div>
          <footer className="wl-foot"><Button variant="ghost" data-setup-dismiss onClick={() => void dismiss()} disabled={busy}>{op ? "Save and close" : "Set up later"}</Button><span className="wl-foot__spacer" />{step > 0 && step < 5 ? <Button variant="ghost" disabled={busy} onClick={() => setStep(mode === "trial" ? 0 : step - 1)}>Back</Button> : null}{step === 5 ? <Button variant="ghost" disabled={busy} onClick={() => { setOp(null); setC(defaultChoices(available, [])); setMode("custom"); setStep(0); setError(undefined); }}>Start another setup</Button> : null}{step < 5 || !checked ? <Button type="submit" variant="primary" loading={busy}>{step === 4 ? "Create pipeline" : step === 5 ? "Resume setup" : "Continue"}</Button> : null}</footer>
        </form>
        {created && op && !busy ? <><SetupCredentials operation={op} onSaved={() => { void qc.invalidateQueries({ queryKey: ["providers"] }); void qc.invalidateQueries({ queryKey: ["team-setup", teamId] }); }} />{checked && corpusId && pipelineId ? <FirstSearch teamId={teamId} corpusId={corpusId} pipelineId={pipelineId} trial={mode === "trial"} /> : null}</> : null}
      </div>
    </div>
  );
}

function SetupCredentials({ operation, onSaved }: { operation: SetupOperation; onSaved: () => void }) {
  const providers = useProviders();
  const ids = new Set(operation.steps.filter((s) => ["parser", "embedder", "store"].includes(s.step)).map((s) => s.resource_id));
  const selected = (providers.data?.items ?? []).filter((p) => ids.has(p.id) && Object.keys(p.secrets).length > 0);
  return <>{selected.map((provider) => <CredentialForm key={`${provider.id}-${provider.updated_at}`} provider={provider} onSaved={onSaved} />)}</>;
}

function CredentialForm({ provider, onSaved }: { provider: Provider; onSaved: () => void }) {
  const health = useHealth();
  const { request, isCurrent } = useTransport();
  const storage = health.data?.secret_storage_enabled ?? false;
  const keys = Object.keys(provider.secrets);
  const [drafts, setDrafts] = useState<Record<string, SecretDraft>>(() => initialSecretDrafts(keys, storage));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const [message, setMessage] = useState("");
  return <form className="wl-pane" onSubmit={(event) => {
    event.preventDefault(); setBusy(true); setError(undefined);
    void (async () => {
      try {
        for (const key of keys) { const draft = drafts[key]!; if (draft.mode !== "skip" && draft.value) await request(`/providers/${provider.id}/secrets/${encodeURIComponent(key)}`, { method: "PUT", body: draft.mode === "env" ? { env: draft.value.trim() } : { value: draft.value } }); }
        if (!isCurrent()) return;
        setDrafts(initialSecretDrafts(keys, storage)); setMessage("Credentials saved. Resume setup to verify the saved services."); onSaved();
      } catch (e) { if (isCurrent()) setError(e); }
      finally { if (isCurrent()) setBusy(false); }
    })();
  }}><h3>{provider.name} credentials</h3><p className="wl-note">{keys.every((key) => provider.secrets[key]?.set) ? "Credentials saved. Enter a value only to replace one." : "Save credentials if this service requires them, then resume setup to verify the connection."}</p><ProviderCredentials secretKeys={keys} drafts={drafts} onChange={(key, draft) => setDrafts((old) => ({ ...old, [key]: draft }))} storageEnabled={storage} errors={{}} disabled={busy} onRecheck={() => void health.refetch()} /><Button type="submit" loading={busy}>Save credentials</Button>{message ? <p role="status">{message}</p> : null}{error ? <TaskError error={error} /> : null}</form>;
}

function FirstSearch({ teamId, corpusId, pipelineId, trial }: { teamId: string; corpusId: string; pipelineId: string; trial: boolean }) {
  const { request, upload, isCurrent } = useTransport();
  const qc = useQueryClient();
  const corpus = useCorpus(corpusId);
  const pipeline = usePipeline(pipelineId);
  const state = useQuery({ queryKey: ["team-setup", teamId], queryFn: () => request<SetupState>(`/teams/${teamId}/setup`), refetchInterval: 2000 });
  const [query, setQuery] = useState("How long does the sample warranty last?");
  const [results, setResults] = useState<QueryResponse>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const [uploadMessage, setUploadMessage] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const add = async (files: File[]) => {
    setBusy(true); setError(undefined);
    try { for (const file of files) { const form = new FormData(); form.set("file", file); form.set("external_id", file.name); await upload<Document>(`/corpora/${corpusId}/documents`, form); if (!isCurrent()) return; } setUploadMessage("Documents queued. Waiting for indexing…"); void qc.invalidateQueries({ queryKey: ["corpora"] }); void state.refetch(); }
    catch (e) { if (isCurrent()) setError(e); } finally { if (isCurrent()) setBusy(false); }
  };
  const ready = state.data?.readiness;
  return <section className="wl-pane" aria-label="First useful search"><h3>{corpus.data?.name ?? "Corpus"} · {pipeline.data?.name ?? "Pipeline"}</h3><p role="status">{ready?.ready ? "Setup complete: an indexed document returned a retrieved chunk." : `${ready?.indexed_documents ?? 0} indexed documents. ${uploadMessage || "Add a document to test retrieval."}`}</p>{trial ? <Button disabled={busy} onClick={() => void add([new File(["# Sample warranty\n\nThe RAGCentre sample warranty lasts twenty four months. A warranty claim requires a receipt. This document demonstrates local retrieval mechanics.\n"], "ragcentre-trial.txt", { type: "text/plain" })])}>Add sample text document</Button> : null} <Button disabled={busy} onClick={() => input.current?.click()}>Choose documents</Button><input ref={input} type="file" hidden multiple accept={trial ? ".txt,.md,.html,text/*" : undefined} onChange={(event) => { if (event.target.files) void add(Array.from(event.target.files)); event.target.value = ""; }} /><p className="wl-note">Indexing runs in the background. <Link to={`/corpora/${corpusId}/documents`}>View ingestion progress</Link>.</p><form onSubmit={(event) => { event.preventDefault(); setBusy(true); setError(undefined); void request<QueryResponse>(`/corpora/${corpusId}/query`, { method: "POST", body: { pipeline_id: pipelineId, query } }).then((response) => { if (isCurrent()) { setResults(response); void state.refetch(); } }).catch((e: unknown) => { if (isCurrent()) setError(e); }).finally(() => { if (isCurrent()) setBusy(false); }); }}><Field label="Test search" hint="The search text is not saved in setup history.">{(a11y) => <TextInput {...a11y} value={query} onChange={(event) => setQuery(event.target.value)} />}</Field><Button type="submit" variant="primary" loading={busy} disabled={!ready?.indexed_documents || !query.trim()}>Search indexed documents</Button></form>{results ? <div aria-live="polite"><p>{results.results.length ? `${results.results.length} retrieved chunks` : "No chunks matched. Try terms from your document."}</p>{results.results.slice(0, 3).map((hit) => <blockquote key={hit.chunk_id}>{hit.text}</blockquote>)}</div> : null}{error ? <TaskError error={error} /> : null}</section>;
}
