import { Plus, Trash2 } from "lucide-react";
import type { PipelineConfig, Provider, RetrievalConfig } from "../../api/types";
import { Button, IconButton } from "../../components/Button";
import { Checkbox, Field, Select, TextInput } from "../../components/Field";
import { NumberInput } from "../../components/NumberInput";

function without(r: RetrievalConfig, key: "rerank" | "expand"): RetrievalConfig {
  const next = { ...r };
  delete next[key];
  return next;
}

/** Sources, fusion (two or more sources), optional reranker and neighbour expansion, final top_k. */
export function RetrievalEditor({
  config,
  providers,
  problems,
  onChange,
}: {
  config: PipelineConfig;
  providers: Provider[];
  problems: { path: string; message: string }[];
  onChange: (r: RetrievalConfig) => void;
}) {
  const r = config.retrieval;
  const indexes = config.nodes.filter((n) => n.type === "index_vector" || n.type === "index_fts");
  const err = (path: string) => problems.find((p) => p.path === path)?.message;
  const general = problems.filter((p) => p.path === "retrieval.sources" || p.path === "retrieval.fusion" || p.path === "retrieval");
  const setSources = (sources: RetrievalConfig["sources"]) => {
    const next: RetrievalConfig = { ...r, sources };
    if (sources.length >= 2 && !next.fusion) next.fusion = { method: "rrf", k: 60 };
    if (sources.length < 2) delete next.fusion;
    onChange(next);
  };
  const rerankers = providers.filter((p) => p.kind === "reranker");

  return (
    <section className="retr" aria-labelledby="retr-title">
      <h3 id="retr-title">Retrieval</h3>
      <p className="node-ed__desc">How a query uses the indexes: each source searches one index; two or more sources are fused; then optional reranking and neighbour expansion.</p>
      {general.length ? (
        <ul className="retr__problems" role="alert">
          {general.map((p, i) => (
            <li key={i}>{p.message}</li>
          ))}
        </ul>
      ) : null}

      <div className="retr__sources">
        {r.sources.map((s, i) => (
          <div key={i} className="retr__source">
            <Field label="Source id" error={err(`retrieval.sources[${i}].id`) ?? null}>
              {(a11y) => <TextInput {...a11y} mono value={s.id} onChange={(e) => setSources(r.sources.map((x, j) => (j === i ? { ...x, id: e.target.value } : x)))} />}
            </Field>
            <Field label="Index" error={err(`retrieval.sources[${i}].index`) ?? null}>
              {(a11y) => (
                <Select {...a11y} value={s.index} onChange={(e) => setSources(r.sources.map((x, j) => (j === i ? { ...x, index: e.target.value } : x)))}>
                  <option value="">Choose…</option>
                  {indexes.map((n) => (
                    <option key={n.id} value={n.id}>
                      {n.id} ({n.type === "index_vector" ? "vector" : "keyword"})
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <NumberInput label="top_k" min={1} max={1000} value={s.top_k} onChange={(v) => setSources(r.sources.map((x, j) => (j === i ? { ...x, top_k: v ?? 0 } : x)))} error={err(`retrieval.sources[${i}].top_k`)} />
            <IconButton icon={Trash2} label={`Remove source ${s.id || i + 1}`} onClick={() => setSources(r.sources.filter((_, j) => j !== i))} />
          </div>
        ))}
        <Button
          size="sm"
          icon={Plus}
          disabled={!indexes.length}
          title={indexes.length ? undefined : "Add an index node first"}
          onClick={() => {
            const used = new Set(r.sources.map((s) => s.index));
            const next = indexes.find((n) => !used.has(n.id)) ?? indexes[0]!;
            const id = next.type === "index_vector" ? "dense" : "sparse";
            setSources([...r.sources, { id: r.sources.some((x) => x.id === id) ? `${id}_${r.sources.length + 1}` : id, index: next.id, top_k: 30 }]);
          }}
        >
          Add source
        </Button>
      </div>

      {r.sources.length >= 2 && r.fusion ? (
        <div className="cfg-grid">
          <Field label="Fusion" hint="Reciprocal rank fusion needs no score calibration; weighted sums normalised scores." error={err("retrieval.fusion") ?? null}>
            {(a11y) => (
              <Select {...a11y} value={r.fusion!.method} onChange={(e) => onChange({ ...r, fusion: { ...r.fusion!, method: e.target.value as "rrf" } })}>
                <option value="rrf">rrf (reciprocal rank)</option>
                <option value="weighted">weighted</option>
              </Select>
            )}
          </Field>
          {r.fusion.method === "rrf" ? <NumberInput label="RRF k" min={1} placeholder="60" value={r.fusion.k} onChange={(v) => onChange({ ...r, fusion: { ...r.fusion!, k: v } })} /> : null}
          {r.sources.map((s) => (
            <NumberInput
              key={s.id}
              label={`Weight of ${s.id || "source"}`}
              integer={false}
              min={0}
              placeholder="1"
              value={r.fusion?.weights?.[s.id]}
              onChange={(v) => {
                const weights = { ...(r.fusion?.weights ?? {}) };
                if (v === undefined) delete weights[s.id];
                else weights[s.id] = v;
                onChange({ ...r, fusion: { ...r.fusion!, weights: Object.keys(weights).length ? weights : undefined } });
              }}
            />
          ))}
        </div>
      ) : null}

      <div className="retr__optional">
        <Checkbox
          checked={Boolean(r.rerank)}
          onChange={(e) => onChange(e.target.checked ? { ...r, rerank: { provider: rerankers[0]?.name ?? "", top_n: 30 } } : without(r, "rerank"))}
          label="Rerank the fused results"
        />
        {r.rerank ? (
          <div className="cfg-grid">
            <Field label="Reranker" error={err("retrieval.rerank.provider") ?? null}>
              {(a11y) => (
                <Select {...a11y} value={r.rerank!.provider} onChange={(e) => onChange({ ...r, rerank: { ...r.rerank!, provider: e.target.value } })}>
                  <option value="">Choose…</option>
                  {rerankers.map((p) => (
                    <option key={p.id} value={p.name}>
                      {p.name}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <NumberInput label="Rerank top_n" min={1} max={1000} value={r.rerank.top_n} onChange={(v) => onChange({ ...r, rerank: { ...r.rerank!, top_n: v ?? 0 } })} error={err("retrieval.rerank.top_n")} />
          </div>
        ) : null}
        <Checkbox
          checked={Boolean(r.expand)}
          onChange={(e) => onChange(e.target.checked ? { ...r, expand: { neighbors: 1 } } : without(r, "expand"))}
          label="Add neighbouring chunks as context"
        />
        {r.expand ? (
          <div className="cfg-grid">
            <NumberInput label="Neighbours each side" min={0} max={5} value={r.expand.neighbors} onChange={(v) => onChange({ ...r, expand: { neighbors: v ?? 0 } })} error={err("retrieval.expand.neighbors")} />
          </div>
        ) : null}
      </div>

      <div className="cfg-grid">
        <NumberInput label="Final top_k" min={1} max={1000} hint="No larger than the sources feeding it" value={r.top_k} onChange={(v) => onChange({ ...r, top_k: v ?? 0 })} error={err("retrieval.top_k")} />
      </div>
    </section>
  );
}
