/** A parse route's options as the form sees them, read from a parser's published `options_schema`. */
export type OptionControl =
  | { kind: "boolean" }
  | { kind: "enum"; choices: string[] }
  | { kind: "integer" | "number"; min?: number; max?: number }
  | { kind: "string" }
  | { kind: "json" };

export interface OptionSpec {
  key: string;
  control: OptionControl;
  description: string | null;
  default: unknown;
}

const isObject = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

function control(spec: Record<string, unknown>): OptionControl {
  if (Array.isArray(spec.enum)) return spec.enum.length > 0 && spec.enum.every((c) => typeof c === "string") ? { kind: "enum", choices: spec.enum as string[] } : { kind: "json" };
  switch (spec.type) {
    case "boolean":
      return { kind: "boolean" };
    case "integer":
    case "number":
      return {
        kind: spec.type,
        min: typeof spec.minimum === "number" ? spec.minimum : undefined,
        max: typeof spec.maximum === "number" ? spec.maximum : undefined,
      };
    case "string":
      return { kind: "string" };
    default:
      return { kind: "json" }; // arrays, mixed enums and anything this form does not know
  }
}

/** The options a schema declares, or `null` when it declares none this form can show (the
 *  caller then falls back to a JSON box). */
export function optionSpecs(schema: unknown): OptionSpec[] | null {
  if (!isObject(schema) || !isObject(schema.properties)) return null;
  const specs = Object.entries(schema.properties).map(([key, raw]) => {
    const spec = isObject(raw) ? raw : {};
    return { key, control: control(spec), description: typeof spec.description === "string" ? spec.description : null, default: spec.default };
  });
  return specs.length > 0 ? specs : null;
}
