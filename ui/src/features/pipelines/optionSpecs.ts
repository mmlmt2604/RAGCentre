// What a parser's `options_schema` says about each option, reduced to how the form edits it.

type Json = Record<string, unknown>;

/** How one schema property is edited. Anything the form cannot express well falls back to `json`. */
export type Control =
  | { kind: "boolean" }
  | { kind: "enum"; choices: string[] }
  | { kind: "integer" | "number"; min?: number; max?: number }
  | { kind: "string" }
  | { kind: "strings" }
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

function controlFor(p: Json): Control {
  if (Array.isArray(p.anyOf) || Array.isArray(p.oneOf) || Array.isArray(p.allOf) || "const" in p) return { kind: "json" };
  if (Array.isArray(p.enum)) return p.enum.length && p.enum.every((v) => typeof v === "string") ? { kind: "enum", choices: p.enum as string[] } : { kind: "json" };
  switch (p.type) {
    case "boolean":
      return { kind: "boolean" };
    case "integer":
    case "number":
      return { kind: p.type, min: num(p.minimum), max: num(p.maximum) };
    case "string":
      return { kind: "string" };
    case "array": {
      const items = p.items;
      const plain = isObject(items) && items.type === "string" && !("enum" in items) && p.minItems === undefined && p.maxItems === undefined;
      return plain ? { kind: "strings" } : { kind: "json" };
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
