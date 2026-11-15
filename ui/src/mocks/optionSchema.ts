// Validate a parse route's options against a parser's `options_schema`, with the paths and wording of
// the backend (src/ragcentre/pipeline/graph.py, option_problems). Covers the keywords the shims'
// pydantic schemas use; anything else is accepted.
type Json = Record<string, unknown>;
export interface OptionProblem {
  /** "" for the options object itself, ".key" for an option, ".key[0]" for an item of one. */
  suffix: string;
  message: string;
}

const isObject = (v: unknown): v is Json => typeof v === "object" && v !== null && !Array.isArray(v);

const TYPE_NAMES: Record<string, string> = { boolean: "true or false", integer: "a whole number", number: "a number", string: "text", array: "a list", object: "an object", null: "null" };

const quoted = (v: unknown) => (typeof v === "string" ? `"${v}"` : String(v));

function isType(type: string, v: unknown): boolean {
  switch (type) {
    case "boolean": return typeof v === "boolean";
    case "integer": return typeof v === "number" && Number.isInteger(v);
    case "number": return typeof v === "number" && Number.isFinite(v);
    case "string": return typeof v === "string";
    case "array": return Array.isArray(v);
    case "object": return isObject(v);
    case "null": return v === null;
    default: return true;
  }
}

function check(schema: Json, value: unknown, path: string, out: OptionProblem[]): void {
  if (Array.isArray(schema.anyOf)) {
    const alternatives = schema.anyOf.filter(isObject);
    const failures = alternatives.map((alt) => {
      const found: OptionProblem[] = [];
      check(alt, value, path, found);
      return found;
    });
    if (failures.some((f) => f.length === 0)) return;
    // `X | None` is anyOf [X, null]: report why X failed rather than a sentence about null.
    const reasons = failures.map((f) => f[0]?.message ?? "").filter((m) => m && m !== "Must be null.");
    const first = reasons[0];
    if (reasons.length === 1 && first) out.push({ suffix: path, message: first });
    else {
      const parts = [...new Set(reasons.map((m) => m.replace(/^Must be /, "").replace(/\.$/, "")))];
      out.push({ suffix: path, message: `Must be ${parts.join(" or ")}.` });
    }
    return;
  }
  if ("const" in schema && value !== schema.const) return void out.push({ suffix: path, message: `Must be ${quoted(schema.const)}.` });
  if (Array.isArray(schema.enum) && !schema.enum.includes(value)) return void out.push({ suffix: path, message: `Must be one of: ${schema.enum.map(quoted).join(", ")}.` });
  const types = typeof schema.type === "string" ? [schema.type] : Array.isArray(schema.type) ? (schema.type as string[]) : [];
  if (types.length && !types.some((t) => isType(t, value))) return void out.push({ suffix: path, message: `Must be ${types.map((t) => TYPE_NAMES[t] ?? t).join(" or ")}.` });
  if (typeof value === "number") {
    if (typeof schema.minimum === "number" && value < schema.minimum) out.push({ suffix: path, message: `Must be at least ${schema.minimum}.` });
    if (typeof schema.maximum === "number" && value > schema.maximum) out.push({ suffix: path, message: `Must be at most ${schema.maximum}.` });
  }
  if (Array.isArray(value)) {
    if (typeof schema.minItems === "number" && value.length < schema.minItems) out.push({ suffix: path, message: `Must have at least ${schema.minItems} items.` });
    if (typeof schema.maxItems === "number" && value.length > schema.maxItems) out.push({ suffix: path, message: `Must have at most ${schema.maxItems} items.` });
    if (isObject(schema.items)) value.forEach((item, i) => check(schema.items as Json, item, `${path}[${i}]`, out));
  }
  if (isObject(value) && isObject(schema.properties)) {
    const props = schema.properties;
    for (const [key, v] of Object.entries(value)) {
      const sub = props[key];
      if (isObject(sub)) check(sub, v, `${path}.${key}`, out);
      else if (schema.additionalProperties === false) out.push({ suffix: `${path}.${key}`, message: "This parser has no such option." });
    }
  }
}

/** Problems with `options`, or none when the parser reports no schema. */
export function optionProblems(schema: unknown, options: unknown): OptionProblem[] {
  if (!isObject(schema)) return [];
  const out: OptionProblem[] = [];
  check(schema, options ?? {}, "", out);
  return out;
}
