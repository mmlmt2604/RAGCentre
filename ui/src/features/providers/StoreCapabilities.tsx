import { Info, TriangleAlert } from "lucide-react";
import type { StoreCapabilities as Caps } from "../../api/types";

/** What a vector store type guarantees, with a visible warning for each guarantee it does not give.
 *  The values are the type's best case: an instance can report less (see `ragcentre store check`). */
export function StoreCapabilitiesPanel({ caps, immutable }: { caps: Caps; immutable: string[] }) {
  const warnings: string[] = [];
  if (!caps.filter_exact)
    warnings.push("Filtered search is not exact: a query with a metadata filter can return fewer than k results even when k records match.");
  if (caps.consistency === "eventual")
    warnings.push("Eventually consistent: a record may not be searchable right after it is written, so a pipeline marked ready can briefly miss new chunks.");
  if (caps.max_dim !== null) warnings.push(`Vectors are limited to ${caps.max_dim.toLocaleString("en-US")} dimensions; a larger embedder cannot be indexed here.`);
  return (
    <div className="store-caps" aria-label="Store capabilities">
      <dl className="store-caps__facts">
        <div>
          <dt>Metrics</dt>
          <dd className="mono">{caps.metrics.join(", ")}</dd>
        </div>
        <div>
          <dt>Max batch</dt>
          <dd className="mono">{caps.max_batch.toLocaleString("en-US")}</dd>
        </div>
        <div>
          <dt>Max dimensions</dt>
          <dd className="mono">{caps.max_dim === null ? "no known limit" : caps.max_dim.toLocaleString("en-US")}</dd>
        </div>
        <div>
          <dt>Consistency</dt>
          <dd className="mono">{caps.consistency}</dd>
        </div>
        <div>
          <dt>Exact filters</dt>
          <dd className="mono">{caps.filter_exact ? "yes" : "no"}</dd>
        </div>
      </dl>
      {warnings.length ? (
        <ul className="store-caps__warn" role="list">
          {warnings.map((w) => (
            <li key={w}>
              <TriangleAlert size={14} strokeWidth={2} aria-hidden /> <span>{w}</span>
            </li>
          ))}
        </ul>
      ) : null}
      {immutable.length ? (
        <p className="store-caps__once">
          <Info size={14} strokeWidth={2} aria-hidden /> <span>Set once: {immutable.join(", ")}. Changing one later means a new provider and a new index.</span>
        </p>
      ) : null}
    </div>
  );
}
