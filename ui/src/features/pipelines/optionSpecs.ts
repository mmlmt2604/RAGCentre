// What a parser's `options_schema` says about each option, reduced to how the form edits it.

type Json = Record<string, unknown>;

/** How one schema property is edited. Anything the form cannot express well falls back to `json`. */
export type Control =
  | { kind: "boolean" }
  | { kind: "enum"; choices: string[] }
  /** A short list of JSON values (`true`, `false`, `"auto"`, or those of a nullable boolean): a select. */
  | { kind: "choice"; choices: (boolean | string | number)[] }
  | { kind: "integer" | "number"; min?: number; max?: number; exclusiveMin?: number; exclusiveMax?: number }
  | { kind: "string" }
  | { kind: "strings" }
  /** `[first, last]`, two whole numbers (a page range). */
  | { kind: "pair"; min?: number }
  | { kind: "json" };

export interface OptionSpec {
  key: string;
  control: Control;
  description: string | null;
  /** `undefined` when the schema names no default. A default is only ever shown, never written. */
  default: unknown;
}

export const isObject = (v: unknown): v is Json => typeof v === "object" && v !== null && !Array.isArray(v);
const num = (v: unknown) => (typeof v === "number" ? v : undefined);
type Scalar = boolean | string | number;
const isScalar = (v: unknown): v is Scalar => typeof v === "boolean" || typeof v === "string" || typeof v === "number";

/** The values one alternative of a union allows, when they are a few plain values; otherwise null. */
function valuesOf(alt: Json): Scalar[] | null {
  if ("const" in alt) return isScalar(alt.const) ? [alt.const] : null;
  if (Array.isArray(alt.enum)) return alt.enum.length && alt.enum.every(isScalar) ? (alt.enum as Scalar[]) : null;
  if (alt.type === "boolean") return [true, false];
  return null;
}

/** `X | None` is `anyOf [X, {type: null}]`: the control of X, and whether null was allowed. Unset is how the form says null. */
function controlFor(p: Json): Control {
  const union = Array.isArray(p.anyOf) ? p.anyOf : Array.isArray(p.oneOf) ? p.oneOf : null;
  if (union) {
    const alts = union.filter(isObject);
    if (alts.length !== union.length) return { kind: "json" };
    const rest = alts.filter((a) => a.type !== "null");
    const nullable = rest.length < alts.length;
    const only = rest[0];
    if (rest.length === 1 && only) {
      const inner = controlFor(only);
      // A nullable boolean has three states, which a checkbox cannot show.
      return nullable && inner.kind === "boolean" ? { kind: "choice", choices: [true, false] } : inner;
    }
    const lists = rest.map(valuesOf);
    if (rest.length > 1 && lists.every((l): l is Scalar[] => l !== null)) {
      const seen = new Set<string>();
      const choices = lists.flat().filter((v) => (seen.has(JSON.stringify(v)) ? false : (seen.add(JSON.stringify(v)), true)));
      return { kind: "choice", choices };
    }
    return { kind: "json" };
  }
  if (Array.isArray(p.allOf) || "const" in p) return { kind: "json" };
  if (Array.isArray(p.enum)) return p.enum.length && p.enum.every((v) => typeof v === "string") ? { kind: "enum", choices: p.enum as string[] } : { kind: "json" };
  switch (p.type) {
    case "boolean":
      return { kind: "boolean" };
    case "integer":
    case "number":
      return { kind: p.type, min: num(p.minimum), max: num(p.maximum), exclusiveMin: num(p.exclusiveMinimum), exclusiveMax: num(p.exclusiveMaximum) };
    case "string":
      return { kind: "string" };
    case "array": {
      const items = p.items;
      if (isObject(items) && items.type === "integer" && p.minItems === 2 && p.maxItems === 2 && !("enum" in items)) return { kind: "pair", min: num(items.minimum) };
      // Free text items, however many are allowed: the server words an item count that is wrong.
      return isObject(items) && items.type === "string" && !("enum" in items) ? { kind: "strings" } : { kind: "json" };
    }
    default:
      return { kind: "json" };
  }
}

/** The options a parser's `options_schema` describes, in schema order; null when it describes none. */
export function optionSpecs(schema: unknown): OptionSpec[] | null {
  if (!isObject(schema) || !isObject(schema.properties)) return null;
  const entries = Object.entries(schema.properties);
  if (!entries.length) return null;
  return entries.map(([key, raw]) => {
    const p = isObject(raw) ? raw : {};
    return { key, control: controlFor(p), description: typeof p.description === "string" ? p.description : null, default: p.default };
  });
}
