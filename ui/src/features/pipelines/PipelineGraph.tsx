import { CircleAlert } from "lucide-react";
import { useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { COLUMNS, type NodeType } from "./graph";
import { NODE_LABEL } from "./model";

export interface GraphNode {
  id: string;
  type: NodeType;
  inputs: string[];
  summary: string;
  /** Extra content under the summary: a fingerprint, counts, a reused/new mark. */
  extra?: ReactNode;
  /** Visual state: problems are marked; "dim" for nodes only the other side of a diff has. */
  tone?: "default" | "problem" | "changed" | "same" | "dim";
  problems?: string[];
}

interface Edge {
  from: string;
  to: string;
  d: string;
}

/**
 * The pipeline as fixed columns by node type (parse → chunk → enrich → embed → index; enrich only when used) with edges drawn
 * between cards. Selection makes the cards buttons; without `onSelect` the graph is read-only.
 */
export function PipelineGraph({
  nodes,
  selected,
  onSelect,
  label,
  footer,
  showColumns,
}: {
  nodes: GraphNode[];
  selected?: string | null;
  onSelect?: (id: string) => void;
  label: string;
  /** Per-column footer, e.g. an "add node" button. */
  footer?: (column: string) => ReactNode;
  /** Optional columns to draw even when empty, so two graphs side by side line up. */
  showColumns?: string[];
}) {
  const box = useRef<HTMLDivElement>(null);
  const [edges, setEdges] = useState<Edge[]>([]);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const signature = nodes.map((n) => `${n.id}<${n.inputs.join(",")}`).join("|");

  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const measure = () => {
      const root = el.getBoundingClientRect();
      const pos = (id: string) => el.querySelector<HTMLElement>(`[data-node="${CSS.escape(id)}"]`)?.getBoundingClientRect();
      const next: Edge[] = [];
      for (const n of nodes) {
        const to = pos(n.id);
        for (const input of n.inputs) {
          const from = pos(input);
          if (!from || !to || !from.width) continue;
          const x1 = from.right - root.left;
          const y1 = from.top + from.height / 2 - root.top;
          const x2 = to.left - root.left;
          const y2 = to.top + to.height / 2 - root.top;
          const mid = (x1 + x2) / 2;
          next.push({ from: input, to: n.id, d: `M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}` });
        }
      }
      setEdges(next);
      setSize({ w: root.width, h: root.height });
    };
    measure();
    const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(measure) : null;
    ro?.observe(el);
    return () => ro?.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature]);

  const byId = new Set(nodes.map((n) => n.id));
  return (
    <div className="pg" ref={box} role="group" aria-label={label}>
      <svg className="pg__edges" width={size.w} height={size.h} aria-hidden>
        {edges.map((e) => (
          <path key={`${e.from}>${e.to}`} d={e.d} data-on={selected && (e.from === selected || e.to === selected) ? "" : undefined} />
        ))}
      </svg>
      {COLUMNS.map((col) => {
        const items = nodes.filter((n) => col.types.includes(n.type));
        if (col.optional && !items.length && !footer && !showColumns?.includes(col.key)) return null;
        return (
          <div key={col.key} className="pg__col">
            <div className="pg__colhead">{col.label}</div>
            {items.map((n) => {
              const dangling = n.inputs.filter((i) => i && !byId.has(i));
              const head = (
                <>
                  <span className="pg__title">
                    <span className="mono">{n.id || "(no id)"}</span>
                    <span className="pg__type">{NODE_LABEL[n.type]}</span>
                    {n.problems?.length ? <CircleAlert size={13} strokeWidth={2.25} className="pg__alert" aria-label={`${n.problems.length} problem${n.problems.length > 1 ? "s" : ""}`} /> : null}
                  </span>
                  <span className="pg__summary mono">{n.summary}</span>
                  {dangling.length ? <span className="pg__dangling">reads from missing “{dangling.join(", ")}”</span> : null}
                </>
              );
              const tone = n.problems?.length ? "problem" : (n.tone ?? "default");
              // The extras can hold buttons (fingerprint tokens), so the select button wraps only the head.
              return (
                <div key={n.id} data-node={n.id} className={`pg__node pg__node--${tone}`} data-selected={selected === n.id || undefined} title={n.problems?.join("\n")}>
                  {onSelect ? (
                    <button type="button" className="pg__select" aria-pressed={selected === n.id} onClick={() => onSelect(n.id)}>
                      {head}
                    </button>
                  ) : (
                    head
                  )}
                  {n.extra ? <span className="pg__extra">{n.extra}</span> : null}
                </div>
              );
            })}
            {footer?.(col.key)}
          </div>
        );
      })}
    </div>
  );
}
