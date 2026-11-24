import { ArrowDown, ArrowUp, Plus, Trash2 } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router";
import { useOcr } from "../../api/hooks";
import type { EnrichMode, PipelineConfig, PipelineNode, Provider, ProviderType } from "../../api/types";
import { Button, IconButton } from "../../components/Button";
import { Checkbox, Field, Select, TextInput } from "../../components/Field";
import { NumberInput } from "../../components/NumberInput";
import { OcrServiceStatus } from "../ocr/OcrIndicator";
import { isProblem, ocrLabel } from "../ocr/model";
import { INPUT_TYPES } from "./graph";
import { NODE_LABEL } from "./model";
import { optionSpecs } from "./optionsSchema";
import { ParserOptions } from "./ParserOptions";

const STRATEGY_HINT: Record<string, string> = {
  fixed: "Fixed-size character windows over the whole document.",
  recursive: "Splits on paragraphs, then lines, sentences and words, merging pieces up to the size.",
  structure: "Follows headings: never merges across a heading, keeps tables and code whole.",
  page: "One chunk per page; falls back to structure when the document has no pages.",
  http: "Your own chunking service (ragcentre.chunker/v1). Options are passed through.",
};
const MODE_HINT: Record<EnrichMode, string> = {
  situate: "A sentence or two placing the chunk within its document, so it can be found on its own (contextual retrieval).",
  summary: "A one or two sentence summary of the chunk.",
  keywords: "A short list of keywords and synonyms the chunk is about.",
  questions: "Questions the chunk answers, in the words a user might ask them.",
};
const DEFAULTS: Record<string, Record<string, number | boolean>> = {
  fixed: { max_chars: 1000, overlap_chars: 100 },
  recursive: { max_chars: 1000, overlap_chars: 100 },
  structure: { max_chars: 1200, overlap_chars: 0, include_heading_path: true },
  page: { max_chars: 4000 },
  http: {},
};

interface ProviderSelectProps {
  kind: Provider["kind"];
  value: string;
  onChange: (v: string) => void;
  providers: Provider[];
  label: string;
  error?: string;
  /** Replaces the default hint. */
  note?: ReactNode;
  optionLabel?: (p: Provider) => string;
}

function ProviderSelect({ kind, value, onChange, providers, label, error, note, optionLabel }: ProviderSelectProps) {
  const options = providers.filter((p) => p.kind === kind);
  return (
    <Field
      label={label}
      error={error ?? null}
      hint={
        note ?? <>
          Only {kind.replace("_", " ")} providers are listed.{" "}
          <Link to={`/providers/new?kind=${kind}`} className="inline-link" target="_blank" rel="noreferrer">
            Add one
          </Link>
        </>
      }
    >
      {(a11y) => (
        <Select {...a11y} value={value} onChange={(e) => onChange(e.target.value)}>
          <option value="">Choose…</option>
          {options.map((p) => (
            <option key={p.id} value={p.name}>
              {optionLabel ? optionLabel(p) : `${p.name} (${p.type})`}
            </option>
          ))}
          {value && !options.some((p) => p.name === value) ? <option value={value}>{value} (not found)</option> : null}
        </Select>
      )}
    </Field>
  );
}

/** The parser picker: each option carries its OCR status, and the hint explains the selected one. */
function ParserSelect(props: Omit<ProviderSelectProps, "kind" | "note" | "optionLabel">) {
  const ocr = useOcr();
  const services = ocr.data?.services ?? [];
  const selected = services.find((s) => s.provider_name && s.provider_name === props.value);
  const found = services.filter((s) => !s.provider_id && s.state === "detected").length;
  const note = selected ? (
    <span className="ocr-hint">
      <OcrServiceStatus service={selected} />
      {isProblem(selected.state) ? <span>{selected.message}</span> : null}
    </span>
  ) : found ? (
    <>
      {found === 1 ? "An OCR shim is running but not added. " : `${found} OCR shims are running but not added. `}
      <Link to="/providers" className="inline-link" target="_blank" rel="noreferrer">
        Add and test
      </Link>
    </>
  ) : null;
  const optionLabel = (p: Provider) => {
    const service = services.find((s) => s.provider_id === p.id);
    return service ? `${p.name} (${p.type}, ${ocrLabel(service)})` : `${p.name} (${p.type})`;
  };
  return <ProviderSelect {...props} kind="parser" note={note} optionLabel={optionLabel} />;
}

function JsonOptions({ value, onChange, label, error }: { value: Record<string, unknown> | undefined; onChange: (v: Record<string, unknown> | undefined) => void; label: string; error?: string }) {
  const text = value && Object.keys(value).length ? JSON.stringify(value) : "";
  return (
    <Field label={label} hint='A JSON object passed to the service, e.g. {"ocr": true}. Leave blank for none.' error={error ?? null}>
      {(a11y) => (
        <TextInput
          {...a11y}
          mono
          defaultValue={text}
          placeholder="{}"
          onBlur={(e) => {
            const t = e.target.value.trim();
            if (!t) return onChange(undefined);
            try {
              const v = JSON.parse(t);
              if (v && typeof v === "object" && !Array.isArray(v)) {
                e.target.setCustomValidity("");
                onChange(v as Record<string, unknown>);
              } else throw new Error();
            } catch {
              e.target.setCustomValidity("Not a JSON object");
              e.target.reportValidity();
            }
          }}
        />
      )}
    </Field>
  );
}

/** Settings for the selected node. Changes go straight into the configuration. */
export function NodeEditor({
  node,
  config,
  providers,
  types,
  problems,
  onChange,
  onRename,
  onRemove,
}: {
  node: PipelineNode;
  config: PipelineConfig;
  providers: Provider[];
  types: ProviderType[];
  problems: { path: string; message: string }[];
  onChange: (n: PipelineNode) => void;
  onRename: (to: string) => void;
  onRemove: () => void;
}) {
  const index = config.nodes.findIndex((n) => n.id === node.id);
  const errAt = (suffix: string) => problems.find((p) => p.path === `nodes[${index}].${suffix}`)?.message;
  // Problems under `nodes[i].<prefix>`, with the prefix removed (".dpi", ".page_range[0]", ...).
  const problemsUnder = (prefix: string) => {
    const full = `nodes[${index}].${prefix}`;
    return problems.filter((p) => p.path === full || p.path.startsWith(`${full}.`) || p.path.startsWith(`${full}[`)).map((p) => ({ path: p.path.slice(full.length), message: p.message }));
  };
  const wantInput = INPUT_TYPES[node.type];
  // Stacked enrich nodes may not read from themselves.
  const candidates = config.nodes.filter((n) => wantInput.includes(n.type) && n.id !== node.id);
  const wantLabel = wantInput.map((t) => NODE_LABEL[t]!.toLowerCase()).join(" or ");
  const input = "inputs" in node ? (node.inputs?.[0] ?? "") : "";

  return (
    <section className="node-ed" aria-label={`Settings for ${node.id}`}>
      <header className="node-ed__head">
        <h3>
          {NODE_LABEL[node.type]} <span className="mono">{node.id}</span>
        </h3>
        <span className="page-head__spacer" />
        <Button size="sm" variant="ghost" icon={Trash2} onClick={onRemove}>
          Remove node
        </Button>
      </header>
      <div className="cfg-grid">
        <Field label="Node id" hint="Lowercase letters, digits and underscores. Retrieval sources refer to index nodes by id." error={errAt("id") ?? null}>
          {(a11y) => <TextInput {...a11y} mono key={node.id} defaultValue={node.id} onBlur={(e) => e.target.value !== node.id && onRename(e.target.value.trim())} />}
        </Field>
        {wantInput.length ? (
          <Field label="Reads from" hint={`A ${wantLabel} node.`} error={errAt("inputs") ?? null}>
            {(a11y) => (
              <Select {...a11y} value={input} onChange={(e) => onChange({ ...node, inputs: [e.target.value] } as PipelineNode)}>
                <option value="">Choose…</option>
                {candidates.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.id}
                  </option>
                ))}
              </Select>
            )}
          </Field>
        ) : null}
      </div>

      {node.type === "parse" ? (
        <div className="routes">
          <p className="node-ed__desc">Each file goes to the first route whose MIME pattern matches it. Order matters.</p>
          {node.routes.map((r, i) => {
            const set = (patch: Partial<typeof r>) => onChange({ ...node, routes: node.routes.map((x, j) => (j === i ? { ...x, ...patch } : x)) });
            const move = (d: -1 | 1) => {
              const routes = [...node.routes];
              const [item] = routes.splice(i, 1);
              routes.splice(i + d, 0, item!);
              onChange({ ...node, routes });
            };
            const specs = optionSpecs(providers.find((p) => p.kind === "parser" && p.name === r.provider)?.config.options_schema);
            return (
              <div key={i} className={specs ? "route route--schema" : "route"}>
                <span className="route__n mono">{i + 1}</span>
                <Field label="MIME pattern" error={errAt(`routes[${i}].mime`) ?? null}>
                  {(a11y) => <TextInput {...a11y} mono value={r.mime} onChange={(e) => set({ mime: e.target.value })} placeholder="text/*" />}
                </Field>
                <ParserSelect label="Parser" value={r.provider} onChange={(v) => set({ provider: v })} providers={providers} error={errAt(`routes[${i}].provider`)} />
                {specs ? null : <JsonOptions label="Options" value={r.options} onChange={(v) => set({ options: v })} error={errAt(`routes[${i}].options`)} />}
                <span className="route__actions">
                  <IconButton icon={ArrowUp} label={`Move route ${i + 1} up`} disabled={i === 0} onClick={() => move(-1)} />
                  <IconButton icon={ArrowDown} label={`Move route ${i + 1} down`} disabled={i === node.routes.length - 1} onClick={() => move(1)} />
                  <IconButton icon={Trash2} label={`Remove route ${i + 1}`} onClick={() => onChange({ ...node, routes: node.routes.filter((_, j) => j !== i) })} />
                </span>
                {specs ? (
                  <ParserOptions
                    specs={specs}
                    value={r.options}
                    onChange={(v) => set({ options: v })}
                    problems={problemsUnder(`routes[${i}].options`)}
                    asJson={<JsonOptions label="Options as JSON" value={r.options} onChange={(v) => set({ options: v })} error={errAt(`routes[${i}].options`)} />}
                  />
                ) : null}
              </div>
            );
          })}
          <Button size="sm" icon={Plus} onClick={() => onChange({ ...node, routes: [...node.routes, { mime: "*/*", provider: "" }] })}>
            Add route
          </Button>
        </div>
      ) : null}

      {node.type === "chunk" ? (
        <div className="cfg-grid">
          <Field label="Strategy" hint={STRATEGY_HINT[node.strategy]} error={errAt("strategy") ?? null}>
            {(a11y) => (
              <Select {...a11y} value={node.strategy} onChange={(e) => onChange({ ...node, strategy: e.target.value as typeof node.strategy, options: { ...DEFAULTS[e.target.value] } })}>
                {Object.keys(STRATEGY_HINT).map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          {node.strategy === "http" ? (
            <>
              <ProviderSelect kind="chunker" label="Chunker service" value={node.provider ?? ""} onChange={(v) => onChange({ ...node, provider: v })} providers={providers} error={errAt("provider")} />
              <JsonOptions label="Options" value={node.options} onChange={(v) => onChange({ ...node, options: v })} />
            </>
          ) : (
            <>
              <NumberInput
                label="Max characters"
                min={100}
                max={20000}
                placeholder={String(DEFAULTS[node.strategy]?.max_chars ?? "")}
                hint={`Default ${DEFAULTS[node.strategy]?.max_chars}`}
                value={node.options?.max_chars as number | undefined}
                onChange={(v) => onChange({ ...node, options: { ...node.options, max_chars: v } })}
                error={errAt("options.max_chars")}
              />
              {node.strategy !== "page" ? (
                <NumberInput
                  label="Overlap characters"
                  min={0}
                  placeholder={String(DEFAULTS[node.strategy]?.overlap_chars ?? 0)}
                  hint="Less than half of max characters"
                  value={node.options?.overlap_chars as number | undefined}
                  onChange={(v) => onChange({ ...node, options: { ...node.options, overlap_chars: v } })}
                  error={errAt("options.overlap_chars")}
                />
              ) : null}
              {node.strategy === "structure" ? (
                <div className="rc-field cfg-bool">
                  <Checkbox
                    checked={node.options?.include_heading_path !== false}
                    onChange={(e) => onChange({ ...node, options: { ...node.options, include_heading_path: e.target.checked } })}
                    label="Put the heading path in each chunk's context"
                  />
                </div>
              ) : null}
            </>
          )}
        </div>
      ) : null}

      {node.type === "enrich" ? (
        <div className="cfg-grid">
          <ProviderSelect kind="llm" label="LLM" value={node.provider} onChange={(v) => onChange({ ...node, provider: v })} providers={providers} error={errAt("provider")} />
          <Field label="Mode" hint={MODE_HINT[node.options?.mode ?? "situate"]} error={errAt("options.mode") ?? null}>
            {(a11y) => (
              <Select {...a11y} value={node.options?.mode ?? "situate"} onChange={(e) => onChange({ ...node, options: { ...node.options, mode: e.target.value as EnrichMode } })}>
                {(Object.keys(MODE_HINT) as EnrichMode[]).map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          <NumberInput
            label="Document characters"
            min={1000}
            placeholder="default"
            hint="How much of the document the model sees with each chunk. Longer documents are cut."
            value={node.options?.max_doc_chars}
            onChange={(v) => onChange({ ...node, options: { ...node.options, max_doc_chars: v } })}
            error={errAt("options.max_doc_chars")}
          />
          <NumberInput
            label="Max answer tokens"
            min={1}
            placeholder="default"
            hint="Caps the text written per chunk."
            value={node.options?.max_tokens}
            onChange={(v) => onChange({ ...node, options: { ...node.options, max_tokens: v } })}
            error={errAt("options.max_tokens")}
          />
          <Field label="Custom prompt" hint="Replaces the built-in prompt for this mode. It must contain {chunk}; {context}, {heading_path} and {title} are also filled in. The document itself is always sent first as the system message. Leave blank to use the built-in prompt." error={errAt("options.prompt") ?? null}>
            {(a11y) => (
              <TextInput
                {...a11y}
                mono
                key={node.id}
                defaultValue={node.options?.prompt ?? ""}
                placeholder="Write one sentence about {chunk}"
                onBlur={(e) => onChange({ ...node, options: { ...node.options, prompt: e.target.value.trim() || undefined } })}
              />
            )}
          </Field>
          <p className="node-ed__desc">
            Each chunk is sent to the model once; answers are cached by model and prompt, so editing a later step or a copy of this pipeline costs no new calls. At most one enrich node may feed the retrieval sources.
          </p>
        </div>
      ) : null}

      {node.type === "embed" ? (
        <div className="cfg-grid">
          <ProviderSelect kind="embedder" label="Embedder" value={node.provider} onChange={(v) => onChange({ ...node, provider: v })} providers={providers} error={errAt("provider")} />
        </div>
      ) : null}

      {node.type === "index_vector" ? (
        <div className="cfg-grid">
          <ProviderSelect kind="vector_store" label="Vector store" value={node.provider} onChange={(v) => onChange({ ...node, provider: v })} providers={providers} error={errAt("provider")} />
          {(() => {
            const p = providers.find((x) => x.name === node.provider);
            const caps = types.find((t) => t.kind === "vector_store" && t.type === p?.type)?.capabilities;
            const metrics = caps?.metrics ?? ["cosine", "l2", "dot"];
            return (
              <Field label="Metric" hint={p ? `${p.name} supports ${metrics.join(", ")}.` : "Choose a store to see the metrics it supports."} error={errAt("options.metric") ?? null}>
                {(a11y) => (
                  <Select {...a11y} value={node.options?.metric ?? "cosine"} onChange={(e) => onChange({ ...node, options: { ...node.options, metric: e.target.value as "cosine" } })}>
                    {(["cosine", "l2", "dot"] as const).map((m) => (
                      <option key={m} value={m} disabled={!metrics.includes(m)}>
                        {m}
                        {metrics.includes(m) ? "" : " (not supported)"}
                      </option>
                    ))}
                  </Select>
                )}
              </Field>
            );
          })()}
        </div>
      ) : null}

      {node.type === "index_fts" ? (
        <div className="cfg-grid">
          <Field label="Tokenizer" hint="unicode61 splits on words; trigram also matches parts of words and suits code and CJK text.">
            {(a11y) => (
              <Select {...a11y} value={node.options?.tokenizer ?? "unicode61"} onChange={(e) => onChange({ ...node, options: { tokenizer: e.target.value as "unicode61" } })}>
                <option value="unicode61">unicode61</option>
                <option value="trigram">trigram</option>
              </Select>
            )}
          </Field>
        </div>
      ) : null}
    </section>
  );
}
