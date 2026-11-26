import { useAuth, useCan, useTransport } from "../auth/context";
import { DurableSetup } from "./DurableSetup";
import { useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ArrowRight, Check, CircleCheck, CircleX, FileUp, RotateCw, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
import { useMatch, useNavigate, useSearchParams } from "react-router";
import { ApiError } from "../../api/client";
import { useCorpora, useOcr, useProviders } from "../../api/hooks";
import type { Corpus, Document, OcrService, OcrStatus, Pipeline, Provider } from "../../api/types";
import { Button } from "../../components/Button";
import { Field, TextInput } from "../../components/Field";
import { Fingerprint } from "../../components/Fingerprint";
import { nextStep } from "../../lib/errors";
import { bytes } from "../../lib/format";
import { ocrLabel } from "../ocr/model";
import {
  acceptFor,
  defaultChoices,
  defaultGuide,
  embedderPick,
  guideDetails,
  markSetupSeen,
  parserPick,
  pipelineConfig,
  setupSeen,
  storePick,
  validateStep,
  type Choices,
  type Errors,
  type Pick,
} from "./model";
import { ShimGuide } from "./ShimGuide";

/**
 * First-run setup. Opens once on an empty server (no corpora) and on demand with ?setup.
 * Four choices fill in a live pipeline chain; nothing is created until the last step.
 */
export function WelcomeSetup() {
  const auth = useAuth();
  const manage = useCan("manage");
  if (!manage) return null;
  return auth.mode === "local" ? <DurableSetup /> : <LegacyWelcomeSetup />;
}

function LegacyWelcomeSetup() {
  const [params, setParams] = useSearchParams();
  const corpora = useCorpora();
  const atHome = useMatch("/");
  const [closed, setClosed] = useState(false);
  const asked = params.has("setup");
  const firstRun = Boolean(atHome && corpora.data && corpora.data.items.length === 0 && !setupSeen());
  // Latch open: the flow itself creates the first corpus, which ends "first run" mid-flow.
  const [latched, setLatched] = useState(false);
  if (firstRun && !closed && !latched) setLatched(true);
  if (!(asked || (latched && !closed))) return null;

  const close = (how: "done" | "skipped") => {
    markSetupSeen(how);
    setClosed(true);
    if (asked) {
      const next = new URLSearchParams(params);
      next.delete("setup");
      setParams(next, { replace: true });
    }
  };
  return <SetupDialog onClose={close} />;
}

const STEPS = ["Welcome", "Parse", "Embed", "Store", "Name"] as const;
const LAST_CHOICE = 4;
const CREATE = 5;
const DONE = 6;

type TaskId = "parser" | "embedder" | "store" | "corpus" | "pipeline" | "active";
interface Task {
  id: TaskId;
  label: string;
  detail: string;
  state: "wait" | "busy" | "done" | "failed";
  ms?: number;
  error?: unknown;
}
/** Which choice step to return to when a task fails. */
const TASK_STEP: Record<TaskId, number> = { parser: 1, embedder: 2, store: 3, corpus: 4, pipeline: 4, active: 4 };
/** Keeps each line of the log on screen long enough to read, even when the server answers instantly. */
const MIN_TASK_MS = 260;

/** Runs one request and returns how long it took, holding the line for at least MIN_TASK_MS. */
async function timed(fn: () => Promise<unknown>): Promise<number> {
  const t0 = performance.now();
  await fn();
  const ms = performance.now() - t0;
  if (ms < MIN_TASK_MS) await new Promise((r) => window.setTimeout(r, MIN_TASK_MS - ms));
  return ms;
}

interface Made {
  providers: Record<string, string>; // request JSON -> created provider name
  corpus?: Corpus;
  pipeline?: Pipeline;
  active?: boolean;
}

const providerName = (m: Made, p: Pick | null) => (p ? ("reuse" in p ? p.reuse : (m.providers[JSON.stringify(p.create)] ?? p.create.name)) : null);

function SetupDialog({ onClose }: { onClose: (how: "done" | "skipped") => void }) {
  const discover = useCan("platform");
  const ref = useRef<HTMLDialogElement>(null);
  const providers = useProviders();
  const ocr = useOcr();
  const corpora = useCorpora();
  const [step, setStep] = useState(0);
  const [dir, setDir] = useState(1);
  const [running, setRunning] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (el && !el.open) {
      if (typeof el.showModal === "function") el.showModal();
      else el.setAttribute("open", "");
    }
  }, []);

  const go = (to: number) => {
    setDir(to >= step ? 1 : -1);
    setStep(to);
  };
  const dismiss = () => {
    if (running) return;
    onClose(step >= DONE ? "done" : "skipped");
  };
  const ready = providers.data && (!discover || ocr.data || ocr.isError) && corpora.data;

  return (
    <dialog
      ref={ref}
      className="rc-dialog wl"
      aria-labelledby="wl-title"
      onCancel={(e) => {
        e.preventDefault();
        dismiss();
      }}
    >
      {ready ? (
        <Flow
          providers={providers.data.items}
          ocrStatus={ocr.data}
          corpora={corpora.data.items}
          step={step}
          dir={dir}
          go={go}
          running={running}
          setRunning={setRunning}
          onSkip={() => onClose("skipped")}
          onFinish={() => onClose("done")}
        />
      ) : (
        <div className="wl-loading" role="status">
          <span className="rc-spinner" aria-hidden /> Checking what this server already has…
        </div>
      )}
      <button type="button" className="rc-iconbtn wl-close" aria-label="Close setup" onClick={dismiss} disabled={running}>
        <X size={16} strokeWidth={1.75} aria-hidden />
      </button>
    </dialog>
  );
}

function Flow({
  providers,
  ocrStatus,
  corpora,
  step,
  dir,
  go,
  running,
  setRunning,
  onSkip,
  onFinish,
}: {
  providers: Provider[];
  ocrStatus?: OcrStatus;
  corpora: Corpus[];
  step: number;
  dir: number;
  go: (step: number) => void;
  running: boolean;
  setRunning: (b: boolean) => void;
  onSkip: () => void;
  onFinish: () => void;
}) {
  const { request, isCurrent } = useTransport();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [c, setC] = useState<Choices>(() => defaultChoices(providers, ocrStatus?.services ?? [], ocrStatus));
  const [arrival, setArrival] = useState<{ id: string; message: string }>();
  const [animateArrival, setAnimateArrival] = useState(false);
  const continueRef = useRef<HTMLButtonElement>(null);
  const guide = c.parser.mode === "guide" && ocrStatus ? guideDetails(c.parser, ocrStatus, providers) : undefined;
  const [errors, setErrors] = useState<Errors>({});
  const [tasks, setTasks] = useState<Task[]>([]);
  // What the run has created so far, so a retry or an edited choice reuses it. The ref is the
  // runner's copy (read between awaits); the state is what renders.
  const [made, setMadeState] = useState<Made>({ providers: {} });
  const madeRef = useRef(made);
  const setMade = (patch: Partial<Made>) => {
    madeRef.current = { ...madeRef.current, ...patch };
    setMadeState(madeRef.current);
  };
  const paneRef = useRef<HTMLDivElement>(null);

  const textParser = providers.find((p) => p.kind === "parser" && p.builtin)?.name ?? "builtin-text";
  const takenCorpora = useMemo(() => new Set(corpora.filter((x) => x.id !== made.corpus?.id).map((x) => x.name)), [corpora, made.corpus]);

  // The provider names the choices resolve to, computed the same way the requests are, so the
  // chain shows exactly what will be created.
  const picks = useMemo(() => {
    // Names this flow registered itself stay free, so a retry resolves to the same requests.
    const ours = new Set(Object.values(made.providers));
    const taken = new Set(providers.map((p) => p.name).filter((n) => !ours.has(n)));
    const claim = <T extends Pick | null>(p: T): T => {
      if (p && "create" in p) taken.add(p.create.name);
      return p;
    };
    const parser = c.parser.mode === "guide" ? null : claim(parserPick(c.parser, taken));
    const embedder = claim(embedderPick(c.embedder, taken));
    const store = claim(storePick(c.store, taken));
    return { parser, embedder, store };
  }, [c.parser, c.embedder, c.store, providers, made.providers]);
  const nameOf = (p: Pick | null) => providerName(made, p);

  const set = (patch: Partial<Choices>) => {
    setC((prev) => ({ ...prev, ...patch }));
    setErrors({});
    if (patch.parser) {
      setArrival(undefined);
      setAnimateArrival(false);
    }
  };

  const foundShim = (service: OcrService, label: string) => {
    set({ parser: { mode: "shim", service } });
    setArrival({ id: service.id, message: `${label} found at ${service.base_url}. Selected as the parser.` });
    setAnimateArrival(true);
  };

  useEffect(() => {
    if (arrival) continueRef.current?.focus();
  }, [arrival]);

  // Move focus into each new step: the selected option, the first field, or the primary action.
  useEffect(() => {
    const pane = paneRef.current;
    if (!pane) return;
    const target =
      pane.querySelector<HTMLElement>("[data-autofocus]") ??
      pane.querySelector<HTMLElement>("input:checked") ??
      pane.querySelector<HTMLElement>("input:not([type=file]), button") ??
      pane.closest("form")?.querySelector<HTMLElement>(".wl-foot [data-autofocus]");
    target?.focus({ preventScroll: true });
  }, [step]);

  useEffect(() => {
    if (Object.keys(errors).length) {
      paneRef.current?.querySelector<HTMLElement>("[aria-invalid='true'], [data-setup-error]")?.focus();
    }
  }, [errors]);

  const run = async () => {
    setRunning(true);
    const plan: Task[] = [];
    if (picks.parser && "create" in picks.parser) plan.push({ id: "parser", label: `Register parser ${nameOf(picks.parser)}`, detail: "POST /v1/providers", state: "wait" });
    if ("create" in picks.embedder) plan.push({ id: "embedder", label: `Register embedder ${nameOf(picks.embedder)}`, detail: "POST /v1/providers", state: "wait" });
    if ("create" in picks.store) plan.push({ id: "store", label: `Register vector store ${nameOf(picks.store)}`, detail: "POST /v1/providers", state: "wait" });
    plan.push({ id: "corpus", label: `Create corpus ${c.corpus.trim()}`, detail: "POST /v1/corpora", state: "wait" });
    plan.push({ id: "pipeline", label: `Create pipeline ${c.pipeline.trim()}`, detail: "POST /v1/corpora/{id}/pipelines", state: "wait" });
    plan.push({ id: "active", label: "Make it the corpus's active pipeline", detail: "PUT /v1/corpora/{id}/active-pipeline", state: "wait" });
    setTasks(plan);

    const patch = (id: TaskId, p: Partial<Task>) => setTasks((ts) => ts.map((t) => (t.id === id ? { ...t, ...p } : t)));
    const provider = async (pick: Pick | null) => {
      if (!pick || "reuse" in pick) return;
      const key = JSON.stringify(pick.create);
      if (madeRef.current.providers[key]) return;
      const p = await request<Provider>("/providers", { method: "POST", body: pick.create });
      setMade({ providers: { ...madeRef.current.providers, [key]: p.name } });
    };
    const steps: Record<TaskId, () => Promise<unknown>> = {
      parser: () => provider(picks.parser),
      embedder: () => provider(picks.embedder),
      store: () => provider(picks.store),
      corpus: async () => {
        const want = c.corpus.trim();
        const had = madeRef.current.corpus;
        if (had?.name === want) return;
        const corpus = had
          ? await request<Corpus>(`/corpora/${had.id}`, { method: "PATCH", body: { name: want } })
          : await request<Corpus>("/corpora", { method: "POST", body: { name: want } });
        setMade({ corpus });
      },
      pipeline: async () => {
        const m = madeRef.current;
        const config = pipelineConfig(c, { text: textParser, parser: providerName(m, picks.parser), embedder: providerName(m, picks.embedder)!, store: providerName(m, picks.store)! });
        if (m.pipeline && JSON.stringify(m.pipeline.config) === JSON.stringify(config)) return;
        const pipeline = await request<Pipeline>(`/corpora/${m.corpus!.id}/pipelines`, { method: "POST", body: config });
        setMade({ pipeline, active: false });
      },
      active: async () => {
        const m = madeRef.current;
        if (m.active) return;
        await request<Corpus>(`/corpora/${m.corpus!.id}/active-pipeline`, { method: "PUT", body: { pipeline_id: m.pipeline!.id, force: true } });
        setMade({ active: true });
      },
    };

    for (const t of plan) {
      patch(t.id, { state: "busy" });
      try {
        const ms = await timed(steps[t.id]);
        if (!isCurrent()) return;
        patch(t.id, { state: "done", ms });
      } catch (error) {
        if (!isCurrent()) return;
        patch(t.id, { state: "failed", error });
        setRunning(false);
        void qc.invalidateQueries();
        return;
      }
    }
    void qc.invalidateQueries();
    setRunning(false);
    window.setTimeout(() => { if (isCurrent()) go(DONE); }, 520);
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (step === 0) return go(1);
    if (step <= LAST_CHOICE) {
      const nextChoices: Choices = step === 1 && guide?.manualUrl !== undefined
        ? { ...c, parser: { mode: "url", url: guide.manualUrl.trim() } }
        : c;
      const errs = validateStep(step, nextChoices, takenCorpora);
      setErrors(errs);
      if (Object.keys(errs).length) return;
      if (nextChoices !== c) setC(nextChoices);
      if (step < LAST_CHOICE) return go(step + 1);
      go(CREATE);
      void run();
      return;
    }
    if (step === CREATE && !running) void run();
  };

  const failed = tasks.find((t) => t.state === "failed");
  const nodeState = useNodeStates(step, tasks, Boolean(made.pipeline) && step >= DONE);

  const waiting = step === 1 && Boolean(guide?.watchUrl);

  return (
    <form className="wl-grid" onSubmit={submit} noValidate>
      <Chain
        choices={c}
        step={step}
        states={waiting ? { ...nodeState, parse: "busy" } : nodeState}
        names={{ text: textParser, parser: guide ? `${guide.engine.id}${waiting ? " (waiting)" : ""}` : nameOf(picks.parser), embedder: nameOf(picks.embedder), store: nameOf(picks.store) }}
        pipeline={step >= DONE ? made.pipeline : undefined}
        onJump={(s) => (s < step && step <= LAST_CHOICE ? go(s) : undefined)}
      />
      <div className="wl-main">
        <div key={step} ref={paneRef} className="wl-pane" style={{ "--dir": dir } as React.CSSProperties}>
          {step === 0 ? <Welcome /> : null}
          {step === 1 ? (
            <ParseStep
              c={c}
              set={set}
              errors={errors}
              providers={providers}
              ocrStatus={ocrStatus}
              textParser={textParser}
              arrival={arrival}
              animateArrival={animateArrival}
              onAnimationEnd={() => setAnimateArrival(false)}
              onFound={foundShim}
            />
          ) : null}
          {step === 2 ? <EmbedStep c={c} set={set} errors={errors} providers={providers} /> : null}
          {step === 3 ? <StoreStep c={c} set={set} errors={errors} providers={providers} /> : null}
          {step === 4 ? <NameStep c={c} set={set} errors={errors} /> : null}
          {step === CREATE ? <CreateStep tasks={tasks} pipeline={c.pipeline.trim()} /> : null}
          {step === DONE && made.corpus && made.pipeline ? (
            <DoneStep corpus={made.corpus} pipeline={made.pipeline} accept={acceptFor(c.parser)} />
          ) : null}
        </div>
        <footer className="wl-foot">
          {step >= 1 && step <= LAST_CHOICE ? <Progress step={step} /> : null}
          {step === 0 ? (
            <Button variant="ghost" onClick={onSkip}>
              Set up on my own
            </Button>
          ) : null}
          <span className="wl-foot__spacer" />
          {step >= 1 && step <= LAST_CHOICE ? (
            <Button variant="ghost" icon={ArrowLeft} onClick={() => go(step - 1)}>
              Back
            </Button>
          ) : null}
          {step === 0 ? (
            <Button type="submit" variant="primary" data-autofocus>
              Start <ArrowRight size={14} strokeWidth={1.75} aria-hidden />
            </Button>
          ) : step < LAST_CHOICE ? (
            <Button ref={continueRef} type="submit" variant="primary">
              Continue <ArrowRight size={14} strokeWidth={1.75} aria-hidden />
            </Button>
          ) : step === LAST_CHOICE ? (
            <Button type="submit" variant="primary">
              Create pipeline
            </Button>
          ) : step === CREATE ? (
            failed ? (
              <>
                <Button variant="ghost" icon={ArrowLeft} onClick={() => go(TASK_STEP[failed.id])}>
                  Change {STEPS[TASK_STEP[failed.id]]!.toLowerCase()} choice
                </Button>
                <Button type="submit" variant="primary" icon={RotateCw} data-autofocus>
                  Try again
                </Button>
              </>
            ) : (
              <Button variant="primary" loading disabled>
                Creating…
              </Button>
            )
          ) : (
            <>
              <Button variant="ghost" onClick={onFinish}>
                Close
              </Button>
              <Button
                variant="primary"
                onClick={() => {
                  onFinish();
                  navigate(`/corpora/${made.corpus!.id}/pipelines?selected=${made.pipeline!.id}`);
                }}
              >
                Open pipeline <ArrowRight size={14} strokeWidth={1.75} aria-hidden />
              </Button>
            </>
          )}
        </footer>
      </div>
    </form>
  );
}

// ---------- The chain (left column) ----------

type NodeId = "corpus" | "parse" | "chunk" | "embed" | "vec" | "fts" | "retrieve";
type NodeState = "todo" | "current" | "set" | "busy" | "done" | "failed" | "off";

function useNodeStates(step: number, tasks: Task[], built: boolean): Record<NodeId, NodeState> {
  const task = (id: TaskId) => tasks.find((t) => t.id === id)?.state;
  const choice = (at: number): NodeState => (step === at ? "current" : step > at ? "set" : "todo");
  const fromTask = (id: TaskId | null, fallback: NodeState): NodeState => {
    if (step < CREATE) return fallback;
    const own = id ? task(id) : undefined;
    if (own === "busy") return "busy";
    if (own === "failed") return "failed";
    const p = task("pipeline");
    if (built || p === "done") return "done";
    return p === "busy" ? "busy" : "set";
  };
  const single = (id: TaskId): NodeState => {
    const s = task(id);
    return s === "busy" ? "busy" : s === "failed" ? "failed" : s === "done" ? "done" : "set";
  };
  return {
    corpus: step >= CREATE ? single("corpus") : choice(4),
    parse: fromTask("parser", choice(1)),
    chunk: fromTask(null, step > 1 ? "set" : "todo"),
    embed: fromTask("embedder", choice(2)),
    vec: fromTask("store", choice(3)),
    fts: fromTask(null, step >= 4 ? "set" : "todo"),
    retrieve: step >= CREATE ? single("active") : choice(4),
  };
}

function Chain({
  choices: c,
  step,
  states,
  names,
  pipeline,
  onJump,
}: {
  choices: Choices;
  step: number;
  states: Record<NodeId, NodeState>;
  names: { text: string; parser: string | null; embedder: string | null; store: string | null };
  pipeline?: Pipeline;
  onJump: (step: number) => void;
}) {
  const fp = (id: string) => pipeline?.nodes.find((n) => n.node_id === id)?.node_fp;
  const dense = c.retrieval === "dense";
  const nodes: { id: NodeId; label: string; value: ReactNode; step?: number }[] = [
    { id: "parse", label: "Parse", step: 1, value: names.parser ? <>{names.text} <span className="wl-node__plus">+</span> {names.parser}</> : names.text },
    { id: "chunk", label: "Chunk", value: "recursive · 1000 chars" },
    { id: "embed", label: "Embed", step: 2, value: c.embedder.mode === "openai" && !c.embedder.model.trim() ? "model not set yet" : names.embedder },
    { id: "vec", label: "Vector index", step: 3, value: (c.store.mode === "qdrant" && !c.store.url.trim()) || (c.store.mode === "chroma" && !c.store.host.trim()) ? "address not set yet" : names.store },
    { id: "fts", label: "Keyword index", value: dense ? "not used" : "SQLite FTS5" },
  ];
  return (
    <aside className="wl-chain" aria-label="Pipeline being set up">
      <div className="wl-corpus" data-state={states.corpus}>
        <span className="wl-corpus__label">Corpus</span>
        <span className="wl-corpus__name mono">{c.corpus.trim() || (step > 4 ? "" : "unnamed")}</span>
        <NodeMark state={states.corpus} />
      </div>
      <ol className="wl-nodes">
        {nodes.map((n, i) => {
          const state = n.id === "fts" && dense && step >= 4 ? "off" : states[n.id];
          const jump = n.step !== undefined && n.step < step && step <= LAST_CHOICE;
          const show = state !== "todo";
          const value = fp(n.id);
          return (
            <li key={n.id} className="wl-node" data-state={state} style={{ "--i": i } as React.CSSProperties}>
              <span className="wl-node__wire" aria-hidden />
              <div className="wl-node__box">
                <span className="wl-node__head">
                  {jump ? (
                    <button type="button" className="wl-node__label wl-node__jump" onClick={() => onJump(n.step!)} title={`Change ${n.label.toLowerCase()}`}>
                      {n.label}
                    </button>
                  ) : (
                    <span className="wl-node__label">{n.label}</span>
                  )}
                  <NodeMark state={state} />
                </span>
                <span className="wl-node__value mono">{show ? n.value : n.step ? `Choose in step ${n.step}` : n.id === "fts" ? "Set in step 4" : "Default settings"}</span>
                {value ? (
                  <span className="wl-fp" style={{ "--i": i } as React.CSSProperties}>
                    <Fingerprint value={value} label={`${n.label} fingerprint`} />
                  </span>
                ) : null}
              </div>
            </li>
          );
        })}
      </ol>
      <div className="wl-retrieve" data-state={states.retrieve}>
        <span className="wl-node__wire" aria-hidden />
        <span className="wl-node__label">Retrieve</span>
        <span className="wl-node__value mono">{step < 4 ? "Choose in step 4" : dense ? "dense · top 8" : "RRF fusion · top 8"}</span>
      </div>
    </aside>
  );
}

function NodeMark({ state }: { state: NodeState }) {
  if (state === "busy") return <span className="rc-spinner wl-mark" aria-label="Working" />;
  if (state === "done") return <Check size={13} strokeWidth={2.25} className="wl-mark wl-mark--ok" aria-label="Created" />;
  if (state === "failed") return <CircleX size={13} strokeWidth={2} className="wl-mark wl-mark--bad" aria-label="Failed" />;
  if (state === "current") return <span className="wl-mark wl-mark--now">editing</span>;
  return null;
}

function Progress({ step }: { step: number }) {
  return (
    <span className="wl-progress" aria-label={`Step ${step} of 4`}>
      <span className="wl-progress__bars" aria-hidden>
        {[1, 2, 3, 4].map((i) => (
          <span key={i} data-on={i <= step || undefined} />
        ))}
      </span>
      <span className="wl-progress__text">
        Step <span className="num">{step}</span> of <span className="num">4</span>
      </span>
    </span>
  );
}

// ---------- Steps ----------

export function Head({ title, children }: { title: ReactNode; children?: ReactNode }) {
  return (
    <header className="wl-head">
      <h2 id="wl-title">{title}</h2>
      {children ? <p>{children}</p> : null}
    </header>
  );
}

function Welcome() {
  return (
    <>
      <header className="wl-head wl-head--hello">
        <h2 id="wl-title">Set up your first pipeline</h2>
        <p>
          A pipeline reads documents through a chain of steps: parse, chunk, embed and index. Each step is fingerprinted, so pipelines that share a step
          share its work. Pick a component for each one and the chain on the left fills in as you go.
        </p>
      </header>
      <dl className="wl-facts">
        <div>
          <dt>Takes</dt>
          <dd>About two minutes, four choices</dd>
        </div>
        <div>
          <dt>Creates</dt>
          <dd>Nothing until the last step, where you review it first</dd>
        </div>
        <div>
          <dt>Works without</dt>
          <dd>Any services of your own: built-in components cover every step</dd>
        </div>
        <div>
          <dt>Changes later</dt>
          <dd>Clone the pipeline and swap one component; unchanged steps keep their work</dd>
        </div>
      </dl>
    </>
  );
}

type StepProps = { c: Choices; set: (p: Partial<Choices>) => void; errors: Errors };

/** One option in a radio list; `more` renders only while the option is selected. */
function Option({ name, checked, onSelect, title, meta, children, more, tag, arrived, onAnimationEnd }: { name: string; checked: boolean; onSelect: () => void; title: ReactNode; meta?: ReactNode; children?: ReactNode; more?: ReactNode; tag?: ReactNode; arrived?: boolean; onAnimationEnd?: () => void }) {
  return (
    <div
      className="wl-opt"
      data-on={checked || undefined}
      data-arrived={arrived || undefined}
      onAnimationEnd={arrived ? (event) => {
        if (event.animationName === "wl-arrive") onAnimationEnd?.();
      } : undefined}
    >
      <label className="wl-opt__row">
        <input type="radio" name={name} checked={checked} onChange={onSelect} />
        <span className="wl-opt__dot" aria-hidden />
        <span className="wl-opt__text">
          <span className="wl-opt__title">
            {title}
            {tag}
          </span>
          {meta ? <span className="wl-opt__meta mono">{meta}</span> : null}
          {children ? <span className="wl-opt__desc">{children}</span> : null}
        </span>
      </label>
      {checked && more ? <div className="wl-opt__more">{more}</div> : null}
    </div>
  );
}

const Tag = ({ children }: { children: ReactNode }) => <span className="rc-badge wl-tag">{children}</span>;

export function ParseStep({ c, set, errors, providers, ocrStatus, textParser, arrival, animateArrival, onAnimationEnd, onFound }: StepProps & {
  providers: Provider[];
  ocrStatus?: OcrStatus;
  textParser: string;
  arrival?: { id: string; message: string };
  animateArrival: boolean;
  onAnimationEnd: () => void;
  onFound: (service: OcrService, label: string) => void;
}) {
  const existing = providers.filter((p) => p.kind === "parser" && !p.builtin);
  const found = (ocrStatus?.services ?? []).filter((s) => !s.provider_id);
  const p = c.parser;
  return (
    <>
      <Head title="How should documents be read?">
        <span className="mono">{textParser}</span> reads plain text, Markdown and HTML in-process. PDFs, scans and Office files need a parser service, such as an OCR shim.
      </Head>
      <div className="sr-only" role="status">{arrival?.message}</div>
      <div className="wl-opts" role="radiogroup" aria-labelledby="wl-title">
        {existing.map((e) => (
          <Option key={e.id} name="parser" checked={p.mode === "existing" && p.name === e.name} onSelect={() => set({ parser: { mode: "existing", name: e.name } })} title={<span className="mono">{e.name}</span>} meta={String(e.config.base_url ?? e.type)} tag={<Tag>registered</Tag>}>
            Text files stay in-process; everything else goes to this service.
          </Option>
        ))}
        {found.map((s) => {
          const down = s.state === "engine_down" || s.state === "offline";
          return (
            <Option
              key={s.id}
              arrived={animateArrival && p.mode === "shim" && p.service.id === s.id && arrival?.id === s.id}
              onAnimationEnd={onAnimationEnd}
              name="parser"
              checked={p.mode === "shim" && p.service.id === s.id}
              onSelect={() => set({ parser: { mode: "shim", service: s } })}
              title={<>{s.name ?? "Parser service"} found running</>}
              meta={`${s.base_url}${s.version ? ` · ${s.version}` : ""}`}
              tag={<Tag>{s.state === "detected" ? "detected" : ocrLabel(s)}</Tag>}
            >
              {down || s.state === "no_ocr" ? s.message : "Registered as a parser provider when you create the pipeline."}
            </Option>
          );
        })}
        {ocrStatus ? (
          <Option
            name="parser"
            checked={p.mode === "guide"}
            onSelect={() => set({ parser: defaultGuide(ocrStatus) })}
            title="Start an OCR shim"
            more={p.mode === "guide" ? (
              <ShimGuide
                choice={p}
                status={ocrStatus}
                providers={providers}
                errors={errors}
                onChange={(parser) => set({ parser })}
                onFound={onFound}
                onReuse={(name) => set({ parser: { mode: "existing", name } })}
              />
            ) : null}
          >
            Pick an engine and run the setup command. {ocrStatus.discovery_enabled ? "RAGCentre watches for it and selects it when it answers." : "Enter the service URL when it starts."}
          </Option>
        ) : null}
        <Option name="parser" checked={p.mode === "text"} onSelect={() => set({ parser: { mode: "text" } })} title="Text files only">
          No service needed. Add a parser later by cloning the pipeline.
        </Option>
        <Option
          name="parser"
          checked={p.mode === "url"}
          onSelect={() => set({ parser: { mode: "url", url: "" } })}
          title="Another parser service"
          more={
            p.mode === "url" ? (
              <Field label="Service URL" hint="RAGCentre reads its /info when the pipeline is created, to pin the service version." error={errors.parserUrl}>
                {(a11y) => <TextInput {...a11y} mono value={p.url} onChange={(e) => set({ parser: { mode: "url", url: e.target.value } })} placeholder="http://parse-01:5001" autoFocus />}
              </Field>
            ) : null
          }
        >
          Any service speaking <span className="mono">ragcentre.parser/v1</span>.
        </Option>
      </div>
    </>
  );
}

export function EmbedStep({ c, set, errors, providers }: StepProps & { providers: Provider[] }) {
  const existing = providers.filter((p) => p.kind === "embedder");
  const e = c.embedder;
  return (
    <>
      <Head title="Which model embeds the chunks?">The embedder turns each chunk into a vector. Changing it later re-embeds and re-indexes, but parsing and chunking are reused.</Head>
      <div className="wl-opts" role="radiogroup" aria-labelledby="wl-title">
        {existing.map((p) => (
          <Option key={p.id} name="embedder" checked={e.mode === "existing" && e.name === p.name} onSelect={() => set({ embedder: { mode: "existing", name: p.name } })} title={<span className="mono">{p.name}</span>} meta={String(p.config.model ?? p.type)} tag={<Tag>registered</Tag>} />
        ))}
        <Option name="embedder" checked={e.mode === "hash"} onSelect={() => set({ embedder: { mode: "hash" } })} title="Built-in hash embedder" tag={<Tag>no setup</Tag>}>
          Lexical hashing; no model or GPU. Good for trying RAGCentre, not for real semantic search.
        </Option>
        <Option
          name="embedder"
          checked={e.mode === "openai"}
          onSelect={() => set({ embedder: { mode: "openai", url: "", model: "" } })}
          title="OpenAI-compatible server"
          more={
            e.mode === "openai" ? (
              <div className="wl-fields">
                <Field label="Base URL" error={errors.embedUrl}>
                  {(a11y) => <TextInput {...a11y} mono value={e.url} onChange={(ev) => set({ embedder: { ...e, url: ev.target.value } })} placeholder="http://gpu-01:8080/v1" autoFocus />}
                </Field>
                <Field label="Model" error={errors.embedModel} hint="If the server needs an API key, add it on the provider's page after setup.">
                  {(a11y) => <TextInput {...a11y} mono value={e.model} onChange={(ev) => set({ embedder: { ...e, model: ev.target.value } })} placeholder="BAAI/bge-small-en-v1.5" />}
                </Field>
              </div>
            ) : null
          }
        >
          vLLM, TEI, Infinity, Ollama or any server exposing <span className="mono">POST /embeddings</span>.
        </Option>
      </div>
    </>
  );
}

export function StoreStep({ c, set, errors, providers }: StepProps & { providers: Provider[] }) {
  const existing = providers.filter((p) => p.kind === "vector_store");
  const s = c.store;
  return (
    <>
      <Head title="Where are the vectors stored?">Each pipeline gets its own collection, so pipelines can share one store without mixing their indexes.</Head>
      <div className="wl-opts" role="radiogroup" aria-labelledby="wl-title">
        {existing.map((p) => (
          <Option key={p.id} name="store" checked={s.mode === "existing" && s.name === p.name} onSelect={() => set({ store: { mode: "existing", name: p.name } })} title={<span className="mono">{p.name}</span>} meta={p.type} tag={<Tag>registered</Tag>} />
        ))}
        <Option name="store" checked={s.mode === "sqlite"} onSelect={() => set({ store: { mode: "sqlite" } })} title="SQLite on this server" tag={<Tag>no setup</Tag>}>
          A local file with exact search. Best under about 100,000 vectors.
        </Option>
        <Option
          name="store"
          checked={s.mode === "qdrant"}
          onSelect={() => set({ store: { mode: "qdrant", url: "" } })}
          title="Qdrant"
          more={
            s.mode === "qdrant" ? (
              <Field label="Server URL" error={errors.storeUrl}>
                {(a11y) => <TextInput {...a11y} mono value={s.url} onChange={(e) => set({ store: { mode: "qdrant", url: e.target.value } })} placeholder="http://qdrant:6333" autoFocus />}
              </Field>
            ) : null
          }
        >
          A Qdrant server you run.
        </Option>
        <Option
          name="store"
          checked={s.mode === "chroma"}
          onSelect={() => set({ store: { mode: "chroma", host: "", port: "8000" } })}
          title="Chroma"
          more={
            s.mode === "chroma" ? (
              <div className="wl-fields wl-fields--host">
                <Field label="Host" error={errors.storeHost}>
                  {(a11y) => <TextInput {...a11y} mono value={s.host} onChange={(e) => set({ store: { ...s, host: e.target.value } })} placeholder="chroma" autoFocus />}
                </Field>
                <Field label="Port" error={errors.storePort}>
                  {(a11y) => <TextInput {...a11y} mono inputMode="numeric" value={s.port} onChange={(e) => set({ store: { ...s, port: e.target.value } })} />}
                </Field>
              </div>
            ) : null
          }
        >
          A Chroma server you run.
        </Option>
      </div>
      <p className="wl-note">PostgreSQL (pgvector) and Pinecone need a secret first. Add them from Providers, then clone this pipeline onto them.</p>
    </>
  );
}

export function NameStep({ c, set, errors }: StepProps) {
  return (
    <>
      <Head title="Name it and choose how to search">A corpus is a set of documents you search together. It can hold many pipelines, so you can compare them on the same documents.</Head>
      <div className="wl-fields wl-fields--names">
        <Field label="Corpus" error={errors.corpus}>
          {(a11y) => <TextInput {...a11y} value={c.corpus} onChange={(e) => set({ corpus: e.target.value })} placeholder="e.g. support-docs" autoComplete="off" data-autofocus />}
        </Field>
        <Field label="Pipeline" error={errors.pipeline}>
          {(a11y) => <TextInput {...a11y} mono value={c.pipeline} onChange={(e) => set({ pipeline: e.target.value })} autoComplete="off" />}
        </Field>
      </div>
      <div className="wl-opts" role="radiogroup" aria-label="Search">
        <Option name="retrieval" checked={c.retrieval === "hybrid"} onSelect={() => set({ retrieval: "hybrid" })} title="Hybrid" tag={<Tag>recommended</Tag>}>
          Vector and keyword indexes over the same chunks, fused with RRF. Catches exact terms that embeddings miss.
        </Option>
        <Option name="retrieval" checked={c.retrieval === "dense"} onSelect={() => set({ retrieval: "dense" })} title="Dense">
          One vector index. The simplest semantic search.
        </Option>
      </div>
    </>
  );
}

function CreateStep({ tasks, pipeline }: { tasks: Task[]; pipeline: string }) {
  const failed = tasks.find((t) => t.state === "failed");
  const done = tasks.length > 0 && tasks.every((t) => t.state === "done");
  return (
    <>
      <Head title={failed ? "Setup stopped" : done ? `Created ${pipeline}` : `Creating ${pipeline}`}>
        {failed ? "Everything above the failed line was created and will be reused when you try again." : "Each request runs in order; the chain on the left follows along."}
      </Head>
      <ol className="wl-log" aria-live="polite">
        {tasks.map((t) => (
          <li key={t.id} className="wl-log__line" data-state={t.state}>
            <span className="wl-log__icon">
              {t.state === "busy" ? <span className="rc-spinner" aria-hidden /> : t.state === "done" ? <CircleCheck size={14} strokeWidth={1.75} aria-hidden /> : t.state === "failed" ? <CircleX size={14} strokeWidth={1.75} aria-hidden /> : <span className="wl-log__wait" aria-hidden />}
            </span>
            <span className="wl-log__label">
              {t.label}
              <span className="sr-only">{t.state === "done" ? ", done" : t.state === "failed" ? ", failed" : t.state === "busy" ? ", running" : ", waiting"}</span>
            </span>
            <span className="wl-log__detail mono">{t.detail}</span>
            <span className="wl-log__ms num mono">{t.ms !== undefined ? `${Math.max(1, Math.round(t.ms))} ms` : ""}</span>
            {t.state === "failed" ? <TaskError error={t.error} /> : null}
          </li>
        ))}
      </ol>
    </>
  );
}

export function TaskError({ error }: { error: unknown }) {
  const api = error instanceof ApiError ? error : null;
  const next = nextStep(error);
  return (
    <div className="wl-log__error" role="alert">
      <p>{api?.message ?? (error instanceof Error ? error.message : "The request failed.")}</p>
      {next ? <p className="wl-log__next">{next}</p> : null}
      {api ? <code>{api.code} · HTTP {api.status}</code> : null}
    </div>
  );
}

interface UploadItem {
  key: string;
  name: string;
  size: number;
  progress: number;
  state: "up" | "added" | "exists" | "failed";
  message?: string;
}

function DoneStep({ corpus, pipeline, accept }: { corpus: Corpus; pipeline: Pipeline; accept?: string }) {
  const { upload, isCurrent } = useTransport();
  const qc = useQueryClient();
  const [items, setItems] = useState<UploadItem[]>([]);
  const [over, setOver] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  const add = (files: FileList | File[]) => {
    const list = Array.from(files);
    if (!list.length) return;
    const fresh = list.map((file, i) => ({ key: `${Date.now()}-${i}-${file.name}`, file }));
    setItems((xs) => [...xs, ...fresh.map(({ key, file }) => ({ key, name: file.name, size: file.size, progress: 0, state: "up" as const }))]);
    const patch = (key: string, p: Partial<UploadItem>) => setItems((xs) => xs.map((x) => (x.key === key ? { ...x, ...p } : x)));
    const queue = [...fresh];
    const worker = async () => {
      for (let it = queue.shift(); it && isCurrent(); it = queue.shift()) {
        const form = new FormData();
        form.set("file", it.file, it.file.name);
        form.set("external_id", it.file.name);
        const key = it.key;
        try {
          const res = await upload<Document>(`/corpora/${corpus.id}/documents`, form, { onProgress: (f) => patch(key, { progress: f }) });
          patch(key, { state: res.status === 201 ? "added" : "exists", progress: 1 });
        } catch (e) {
          if (!isCurrent()) return;
          patch(key, { state: "failed", message: e instanceof Error ? e.message : "Upload failed." });
        }
      }
    };
    void Promise.all([worker(), worker(), worker()]).then(() => qc.invalidateQueries({ queryKey: ["corpora"] }));
  };

  const added = items.filter((i) => i.state === "added").length;
  return (
    <>
      <Head title={<>{pipeline.name} is ready for documents</>}>
        It is the active pipeline of <strong>{corpus.name}</strong>. Documents you add now are parsed, chunked, embedded and indexed in the background.
      </Head>
      <div className="wl-made">
        <span className="wl-made__label">Pipeline fingerprint</span>
        <Fingerprint value={pipeline.fingerprint} length={12} label="Pipeline fingerprint" />
        <span className="wl-made__hint">The node fingerprints are now on the chain. A second pipeline that shares a step shows the same colour there.</span>
      </div>
      <div
        className="wl-drop"
        data-over={over || undefined}
        onDragOver={(e) => {
          e.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setOver(false);
          add(e.dataTransfer.files);
        }}
      >
        <FileUp size={18} strokeWidth={1.5} aria-hidden className="wl-drop__icon" />
        <span className="wl-drop__text">
          <strong>Drop documents here</strong>
          <span>{accept ? "Text, Markdown and HTML files." : "Any file type your parser reads."} You can also do this later from the corpus.</span>
        </span>
        <Button size="sm" onClick={() => input.current?.click()} data-autofocus>
          Choose files
        </Button>
        <input
          ref={input}
          type="file"
          multiple
          accept={accept}
          hidden
          onChange={(e) => {
            if (e.target.files) add(e.target.files);
            e.target.value = "";
          }}
        />
      </div>
      {items.length ? (
        <ul className="wl-files" aria-live="polite" aria-label={`${added} of ${items.length} uploaded`}>
          {items.map((i) => (
            <li key={i.key} data-state={i.state}>
              {i.state === "failed" ? <CircleX size={14} aria-hidden className="wl-files__bad" /> : i.state === "up" ? <span className="rc-spinner" aria-hidden /> : <CircleCheck size={14} aria-hidden className="wl-files__ok" />}
              <span className="wl-files__name mono">{i.name}</span>
              <span className="wl-files__size num mono">{bytes(i.size)}</span>
              <span className="wl-files__state">{i.state === "up" ? `${Math.round(i.progress * 100)}%` : i.state === "added" ? "Queued for ingestion" : i.state === "exists" ? "Already in corpus" : i.message}</span>
              <span className="wl-files__bar" aria-hidden>
                <span style={{ transform: `scaleX(${i.progress})` }} />
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </>
  );
}
