import { ArrowLeft, Plus } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import YAML from "yaml";
import { ApiError } from "../../api/client";
import { useCorpus, useCreatePipeline, usePipeline, usePipelines, usePlan, useProviders, useProviderTypes } from "../../api/hooks";
import type { PipelineConfig, PipelineNode, PipelineProblem } from "../../api/types";
import { Button } from "../../components/Button";
import { CodeEditor } from "../../components/Code";
import { Field, Select, TextInput } from "../../components/Field";
import { Fingerprint } from "../../components/Fingerprint";
import { ErrorState, Skeleton } from "../../components/States";
import { Tabs } from "../../components/Tabs";
import { useDebounced } from "../../lib/useDebounced";
import { useToast } from "../../lib/toast";
import { blankNode, cloneConfig, inputsOf, nodeSummary, problemNodeIndex, removeNode, renameNode, template, type NodeType, type TemplateKey } from "./graph";
import { NodeEditor } from "./NodeEditor";
import { PipelineGraph, type GraphNode } from "./PipelineGraph";
import { PlanPanel } from "./PlanPanel";
import { RetrievalEditor } from "./RetrievalEditor";

const TEMPLATES: { key: TemplateKey; title: string; desc: string }[] = [
  { key: "dense", title: "Dense", desc: "Parse, chunk, embed, one vector index. The simplest semantic search." },
  { key: "hybrid", title: "Hybrid", desc: "Vector and keyword indexes over the same chunks, fused with RRF. A strong default." },
  { key: "rerank", title: "Hybrid with reranker", desc: "Hybrid, then a reranker reorders the fused results. Needs a reranker provider." },
  { key: "blank", title: "Blank", desc: "Just a parse node. Build the rest yourself." },
];

/** Parse YAML or JSON text into a configuration, or say why it is not one. */
function parseConfig(text: string): { config?: PipelineConfig; error?: string } {
  try {
    const v = YAML.parse(text) as unknown;
    if (!v || typeof v !== "object" || Array.isArray(v)) return { error: "The text must be a mapping with name, nodes and retrieval." };
    const c = v as Partial<PipelineConfig>;
    if (!Array.isArray(c.nodes)) return { error: "nodes must be a list." };
    const object = (x: unknown): x is Record<string, unknown> => x !== null && typeof x === "object" && !Array.isArray(x);
    for (const [i, node] of c.nodes.entries()) {
      if (!object(node) || typeof node.id !== "string" || !["parse", "chunk", "enrich", "embed", "index_vector", "index_fts"].includes(node.type)) {
        return { error: `nodes[${i}] needs a string id and a supported node type.` };
      }
      if (node.type === "parse") {
        if (!Array.isArray(node.routes) || node.routes.some((r) => !object(r) || typeof r.mime !== "string" || typeof r.provider !== "string" || (r.options !== undefined && !object(r.options)))) {
          return { error: `nodes[${i}].routes must contain MIME patterns, provider names and optional options objects.` };
        }
      } else if (!Array.isArray(node.inputs) || node.inputs.some((input: unknown) => typeof input !== "string")) {
        return { error: `nodes[${i}].inputs must be a list of node ids.` };
      }
      if (node.type === "chunk" && !["fixed", "recursive", "structure", "page", "http"].includes(node.strategy)) return { error: `nodes[${i}].strategy is not supported.` };
      if ("provider" in node && typeof node.provider !== "string") return { error: `nodes[${i}].provider must be a name.` };
      if ("options" in node && node.options !== undefined && !object(node.options)) return { error: `nodes[${i}].options must be an object.` };
    }
    if (!object(c.retrieval)) return { error: "retrieval must be an object." };
    if (!Array.isArray(c.retrieval.sources) || c.retrieval.sources.some((s) => !object(s) || typeof s.id !== "string" || typeof s.index !== "string" || typeof s.top_k !== "number")) return { error: "retrieval.sources must contain an id, index and numeric top_k." };
    if (typeof c.retrieval.top_k !== "number") return { error: "retrieval.top_k must be a number." };
    for (const key of ["fusion", "rerank", "expand"] as const) {
      if (c.retrieval[key] !== undefined && !object(c.retrieval[key])) return { error: `retrieval.${key} must be an object.` };
    }
    return { config: { name: typeof c.name === "string" ? c.name : "", nodes: c.nodes, retrieval: c.retrieval } };
  } catch (e) {
    const err = e as { message?: string; linePos?: { line: number }[] };
    return { error: err.message?.split("\n")[0] ?? "Not valid YAML or JSON." };
  }
}

export function PipelineBuilder() {
  const { corpusId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const from = params.get("from");
  const tpl = params.get("template") as TemplateKey | null;
  const corpus = useCorpus(corpusId);
  const source = usePipeline(from ?? undefined);
  const pipelines = usePipelines(corpusId);
  const archived = usePipelines(corpusId, true);
  const providers = useProviders();
  // The starting configuration: a clone or a template, once the data it needs has loaded. The
  // editor is keyed on the start, so it keeps its own state from then on.
  const start = useMemo((): PipelineConfig | null => {
    if (from) {
      if (!source.data || !pipelines.data || !archived.data) return null;
      const names = new Set([...pipelines.data.items, ...archived.data.items].map((p) => p.name));
      return cloneConfig(source.data.config, names);
    }
    if (!tpl || !providers.data) return null;
    const first = (kind: string) => providers.data!.items.find((p) => p.kind === kind)?.name;
    return template(tpl, { embedder: first("embedder"), store: first("vector_store"), reranker: first("reranker") });
  }, [from, tpl, source.data, pipelines.data, archived.data, providers.data]);

  const back = (
    <Link to={`/corpora/${corpusId}/pipelines`} className="back-link">
      <ArrowLeft size={14} aria-hidden /> Pipelines
    </Link>
  );

  const failed = [source, corpus, providers, pipelines, archived].find((query) => query.isError);
  if (failed) return <div className="page-pad">{back}<ErrorState error={failed.error} onRetry={() => void failed.refetch()} /></div>;

  if (!from && !tpl) {
    const existing = pipelines.data?.items ?? [];
    return (
      <div className="page-pad narrow-page">
        <header className="page-head">
          <div>
            {back}
            <h1>New pipeline</h1>
          </div>
        </header>
        {existing.length ? (
          <section className="form-section" aria-labelledby="clone-title">
            <h2 id="clone-title">Clone an existing pipeline</h2>
            <p className="form-section__desc">The fastest way to try one change: every step you keep is shared with the original, so only the changed steps and those after them run.</p>
            <div className="inline-form">
              <Field label="Pipeline">
                {(a11y) => (
                  <Select {...a11y} defaultValue="" onChange={(e) => e.target.value && setParams({ from: e.target.value })}>
                    <option value="" disabled>
                      Choose a pipeline…
                    </option>
                    {existing.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name}
                        {p.is_active ? " (active)" : ""}
                      </option>
                    ))}
                  </Select>
                )}
              </Field>
            </div>
          </section>
        ) : null}
        <section className="form-section" aria-labelledby="tpl-title">
          <h2 id="tpl-title">{existing.length ? "Or start from a template" : "Start from a template"}</h2>
          <div className="choice-list">
            {TEMPLATES.map((t) => (
              <button key={t.key} type="button" className="choice" onClick={() => setParams({ template: t.key })}>
                <span className="choice__title">{t.title}</span>
                <span className="choice__desc">{t.desc}</span>
              </button>
            ))}
          </div>
        </section>
      </div>
    );
  }

  if (!start || !corpus.data) {
    return (
      <div className="page-pad">
        {back}
        <Skeleton width={260} height={22} />
        <Skeleton width="100%" height={320} />
      </div>
    );
  }
  return <Builder key={from ?? tpl ?? ""} corpusId={corpusId} initial={start} documentCount={corpus.data.document_count} sourceName={source.data?.name ?? null} back={back} />;
}

function Builder({ corpusId, initial, documentCount, sourceName, back }: { corpusId: string; initial: PipelineConfig; documentCount: number; sourceName: string | null; back: React.ReactNode }) {
  const navigate = useNavigate();
  const toast = useToast();
  const providers = useProviders();
  const types = useProviderTypes();
  const pipelines = usePipelines(corpusId);
  const create = useCreatePipeline(corpusId);
  const [config, setConfig] = useState(initial);
  const [view, setView] = useState<"form" | "code">("form");
  const [code, setCode] = useState("");
  const [codeError, setCodeError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(initial.nodes[1]?.id ?? initial.nodes[0]?.id ?? null);

  const debounced = useDebounced(config, 350);
  const plan = usePlan(corpusId, debounced);
  const stale = debounced !== config || plan.isFetching;
  const problems: PipelineProblem[] = plan.data && !plan.data.valid ? plan.data.problems : [];
  const planNodes = new Map((plan.data?.valid ? plan.data.nodes : []).map((n) => [n.node_id, n]));

  const problemsFor = (n: PipelineNode, i: number) => problems.filter((p) => p.node_id === n.id || problemNodeIndex(p.path) === i).map((p) => p.message);

  const graph: GraphNode[] = config.nodes.map((n, i) => {
    const pn = planNodes.get(n.id);
    return {
      id: n.id,
      type: n.type,
      inputs: inputsOf(n),
      summary: nodeSummary(n),
      problems: problemsFor(n, i),
      extra: pn ? (
        <>
          <Fingerprint value={pn.node_fp} length={6} unique={!pn.reused} label={`${n.id} step`} />
          <span className={pn.reused ? "pg__reused" : "pg__new"}>{pn.reused ? "reused" : `new · ${pn.runs_to_create} runs`}</span>
        </>
      ) : undefined,
    };
  });

  const switchView = (v: "form" | "code") => {
    if (v === "code") {
      setCode(YAML.stringify(config, { lineWidth: 0 }));
      setCodeError(null);
      setView("code");
      return;
    }
    const parsed = parseConfig(code);
    if (parsed.error) {
      setCodeError(`Fix the text before switching to the form: ${parsed.error}`);
      return;
    }
    setConfig(parsed.config!);
    setView("form");
  };

  const update = (n: PipelineNode) => setConfig((c) => ({ ...c, nodes: c.nodes.map((x) => (x.id === selected ? n : x)) }));
  const add = (type: NodeType) => {
    const node = blankNode(type, config);
    setConfig({ ...config, nodes: [...config.nodes, node] });
    setSelected(node.id);
  };

  const selectedNode = config.nodes.find((n) => n.id === selected) ?? null;
  const items = providers.data?.items ?? [];
  const nameProblem = problems.find((p) => p.path === "name")?.message;
  const createErr = create.error;

  const submit = () =>
    create.mutate(config, {
      onSuccess: (p) => {
        toast({ tone: "ok", title: `Created ${p.name}`, body: p.status === "ready" ? "Every step already existed, so it is ready now." : "Ingestion has started." });
        navigate(`/corpora/${corpusId}/pipelines/${p.id}`);
      },
    });

  return (
    <div className="builder">
      <div className="builder__main">
        <header className="builder__head">
          {back}
          <h1>{sourceName ? `New pipeline from ${sourceName}` : "New pipeline"}</h1>
          {sourceName ? (
            <p className="page-head__sub">Pipelines cannot be edited once created; change the copy and only the steps you change, and those after them, run again.</p>
          ) : null}
          <div className="builder__name field-name">
            <Field label="Name *" error={nameProblem ?? null}>
              {(a11y) => <TextInput {...a11y} mono value={config.name} onChange={(e) => setConfig({ ...config, name: e.target.value })} placeholder="e.g. hybrid-structure-1200" />}
            </Field>
          </div>
        </header>
        <Tabs<"form" | "code">
          label="Editor"
          value={view}
          onChange={switchView}
          items={[
            { value: "form", label: "Form" },
            { value: "code", label: "YAML" },
          ]}
        />
        {view === "form" ? (
          <div className="builder__form">
            <PipelineGraph
              label="Pipeline steps"
              nodes={graph}
              selected={selected}
              onSelect={setSelected}
              footer={(col) => (
                <span className="pg__add">
                  {col === "parse" && !config.nodes.some((n) => n.type === "parse") ? <AddButton label="Parse" onClick={() => add("parse")} /> : null}
                  {col === "chunk" ? <AddButton label="Chunk" onClick={() => add("chunk")} /> : null}
                  {col === "enrich" ? <AddButton label="Enrich" onClick={() => add("enrich")} /> : null}
                  {col === "embed" ? <AddButton label="Embed" onClick={() => add("embed")} /> : null}
                  {col === "index" ? (
                    <>
                      <AddButton label="Vector index" onClick={() => add("index_vector")} />
                      <AddButton label="Keyword index" onClick={() => add("index_fts")} />
                    </>
                  ) : null}
                </span>
              )}
            />
            {selectedNode ? (
              <NodeEditor
                key={selectedNode.id}
                node={selectedNode}
                config={config}
                providers={items}
                types={types.data?.items ?? []}
                problems={problems}
                onChange={update}
                onRename={(to) => {
                  if (!to || to === selectedNode.id) return;
                  setConfig(renameNode(config, selectedNode.id, to));
                  setSelected(to);
                }}
                onRemove={() => {
                  setConfig(removeNode(config, selectedNode.id));
                  setSelected(null);
                }}
              />
            ) : (
              <p className="muted builder__hint">Select a step in the graph to change its settings.</p>
            )}
            <RetrievalEditor config={config} providers={items} problems={problems} onChange={(retrieval) => setConfig({ ...config, retrieval })} />
          </div>
        ) : (
          <div className="builder__code">
            <p className="node-ed__desc">The same configuration as YAML (JSON also works). The plan updates as you type; options the form does not cover can be set here.</p>
            <CodeEditor
              value={code}
              label="Pipeline configuration"
              language="yaml"
              rows={28}
              onChange={(t) => {
                setCode(t);
                const parsed = parseConfig(t);
                if (parsed.config) {
                  setConfig(parsed.config);
                  setCodeError(null);
                } else setCodeError(parsed.error ?? null);
              }}
            />
            {codeError ? (
              <p className="rc-field__error" role="alert">
                {codeError}
              </p>
            ) : null}
          </div>
        )}
      </div>
      <PlanPanel
        plan={plan.data}
        loading={plan.isLoading}
        error={codeError ? new Error("Fix the YAML before creating this pipeline. The editor shows what needs attention.") : plan.error}
        onRetry={codeError ? undefined : () => void plan.refetch()}
        stale={stale}
        pipelines={pipelines.data?.items ?? []}
        documentCount={documentCount}
        onSelectNode={(id) => {
          if (view === "code") switchView("form");
          setSelected(id);
        }}
        onCreate={submit}
        creating={create.isPending}
        createError={
          createErr ? (
            <div className="form-error" role="alert">
              <strong>{createErr.message}</strong>
              <code>{createErr instanceof ApiError ? createErr.code : "request_failed"}</code>
            </div>
          ) : null
        }
      />
    </div>
  );
}

function AddButton({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <Button size="sm" variant="ghost" icon={Plus} onClick={onClick}>
      {label}
    </Button>
  );
}
