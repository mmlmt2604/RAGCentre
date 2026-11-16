// Validate a parse route's options against a parser's `options_schema`, with the paths and wording of
// the backend: `option_problems` in src/ragcentre/pipeline/graph.py is the reference, and both are tested
// against the cases in tests/fixtures/option_problems.json.
//
// Mirrors the way jsonschema (Draft 2020-12) walks a schema: keywords in the schema's own order, every
// keyword reported (a wrong type and a wrong minimum are two problems), `$ref` to `#/...` followed, and the
// final list sorted by the path of the failing node. A failed `type` is dropped when `enum` or `const` on the
// same node failed too: the sentence that names the allowed values says it better.
//
// Keywords the backend words with jsonschema's own text (`pattern`, `format`, `multipleOf`, `uniqueItems`,
// `not`, `if`, `contains`, `oneOf` matching two alternatives, ...) are not checked here: the mock accepts
// what it cannot word the same way.
type Json = Record<string, unknown>;
type Seg = string | number;

export interface OptionProblem {
  /** "" for the options object itself, ".key" for an option, ".key[0]" for an item of one. */
  suffix: string;
  message: string;
}

interface Err {
  /** Path of the object or value that failed, as jsonschema's `absolute_path`. */
  path: Seg[];
  kind: string;
  message: string;
  /** For `additionalProperties` and `required`: the keys, reported one problem each. */
  keys?: string[];
}

const isObject = (v: unknown): v is Json => typeof v === "object" && v !== null && !Array.isArray(v);

const TYPE_NAMES: Record<string, string> = { boolean: "true or false", integer: "a whole number", number: "a number", string: "text", array: "a list", object: "an object", null: "null" };

/** A JSON value as a message writes it: text quoted, `true`/`false`/`null`, numbers plain. */
const literal = (v: unknown): string => (typeof v === "string" ? `"${v}"` : v === null ? "null" : String(v));

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

const same = (a: unknown, b: unknown) => (typeof a === "object" || typeof b === "object" ? JSON.stringify(a) === JSON.stringify(b) : a === b);

/** `Must be true or false or "auto".`, `Must have at most 2 items or be null.`: one sentence per alternative, joined. */
function joinAlternatives(messages: string[]): string {
  const pieces = [...new Set(messages.map((m) => m.replace(/\.+$/, "").replace(/^Must /, "")))];
  let text = pieces[0] ?? "";
  for (let i = 1; i < pieces.length; i++) {
    const previous = pieces[i - 1]!;
    const piece = pieces[i]!;
    text += " or " + (previous.startsWith("be ") && piece.startsWith("be ") ? piece.slice(3) : piece);
  }
  return `Must ${text}.`;
}

/** Python's `re.search`, with a pattern JavaScript cannot compile counting as no match. */
function matches(pattern: string, text: string): boolean {
  try {
    return new RegExp(pattern).test(text);
  } catch {
    return false;
  }
}

function resolveRef(root: Json, ref: string): unknown {
  if (!ref.startsWith("#")) return undefined;
  let node: unknown = root;
  for (const part of ref.slice(1).split("/").filter(Boolean)) {
    const key = decodeURIComponent(part).replace(/~1/g, "/").replace(/~0/g, "~");
    node = isObject(node) || Array.isArray(node) ? (node as Record<string, unknown>)[key] : undefined;
  }
  return node;
}

function validate(root: Json, schema: unknown, value: unknown, path: Seg[], depth = 0): Err[] {
  if (!isObject(schema) || depth > 32) return [];
  const out: Err[] = [];
  const add = (kind: string, message: string, extra: Partial<Err> = {}) => out.push({ path, kind, message, ...extra });
  let typeError: Err | undefined;
  let pinned = false; // this node's own `enum` or `const` failed (not one of an `allOf` or `$ref` below it)
  for (const [kind, rule] of Object.entries(schema)) {
    switch (kind) {
      case "$ref":
        if (typeof rule === "string") out.push(...validate(root, resolveRef(root, rule), value, path, depth + 1));
        break;
      case "type": {
        const names = typeof rule === "string" ? [rule] : Array.isArray(rule) ? rule.map(String) : [];
        if (names.length && !names.some((t) => isType(t, value))) {
          add(kind, `Must be ${names.map((t) => TYPE_NAMES[t] ?? t).join(" or ")}.`);
          typeError = out[out.length - 1];
        }
        break;
      }
      case "enum":
        if (Array.isArray(rule) && !rule.some((v) => same(v, value))) {
          add(kind, `Must be one of: ${rule.map(literal).join(", ")}.`);
          pinned = true;
        }
        break;
      case "const":
        if (!same(rule, value)) {
          add(kind, `Must be ${literal(rule)}.`);
          pinned = true;
        }
        break;
      case "minimum":
        if (typeof value === "number" && typeof rule === "number" && value < rule) add(kind, `Must be at least ${rule}.`);
        break;
      case "maximum":
        if (typeof value === "number" && typeof rule === "number" && value > rule) add(kind, `Must be at most ${rule}.`);
        break;
      case "exclusiveMinimum":
        if (typeof value === "number" && typeof rule === "number" && value <= rule) add(kind, `Must be greater than ${rule}.`);
        break;
      case "exclusiveMaximum":
        if (typeof value === "number" && typeof rule === "number" && value >= rule) add(kind, `Must be less than ${rule}.`);
        break;
      case "minLength":
        if (typeof value === "string" && typeof rule === "number" && [...value].length < rule) add(kind, `Must have at least ${rule} characters.`);
        break;
      case "maxLength":
        if (typeof value === "string" && typeof rule === "number" && [...value].length > rule) add(kind, `Must have at most ${rule} characters.`);
        break;
      case "minItems":
        if (Array.isArray(value) && typeof rule === "number" && value.length < rule) add(kind, `Must have at least ${rule} items.`);
        break;
      case "maxItems":
        if (Array.isArray(value) && typeof rule === "number" && value.length > rule) add(kind, `Must have at most ${rule} items.`);
        break;
      case "items":
        if (Array.isArray(value) && isObject(rule)) value.forEach((item, i) => out.push(...validate(root, rule, item, [...path, i], depth + 1)));
        break;
      case "properties":
        if (isObject(value) && isObject(rule))
          for (const [key, sub] of Object.entries(rule)) if (key in value) out.push(...validate(root, sub, value[key], [...path, key], depth + 1));
        break;
      case "required": {
        const missing = isObject(value) && Array.isArray(rule) ? rule.filter((k): k is string => typeof k === "string" && !(k in value)) : [];
        if (missing.length) add(kind, `Must have ${missing.join(", ")}.`, { keys: missing });
        break;
      }
      case "additionalProperties": {
        if (!isObject(value)) break;
        const declared = isObject(schema.properties) ? schema.properties : {};
        const patterns = isObject(schema.patternProperties) ? Object.keys(schema.patternProperties) : [];
        const extras = Object.keys(value).filter((k) => !(k in declared) && !patterns.some((p) => matches(p, k)));
        if (rule === false && extras.length) add(kind, "Must have only the options the parser lists.", { keys: extras });
        else if (isObject(rule)) for (const k of extras) out.push(...validate(root, rule, value[k], [...path, k], depth + 1));
        break;
      }
      case "allOf":
        if (Array.isArray(rule)) for (const sub of rule) out.push(...validate(root, sub, value, path, depth + 1));
        break;
      case "anyOf":
      case "oneOf": {
        if (!Array.isArray(rule) || !rule.length) break;
        const branches = rule.map((sub) => validate(root, sub, value, path, depth + 1));
        const valid = branches.filter((b) => b.length === 0).length;
        if (valid > 0) break; // `oneOf` matching two alternatives is worded by jsonschema itself: not mirrored
        add(kind, joinAlternatives(branches.flat().map((e) => e.message)));
        break;
      }
      default:
        break;
    }
  }
  // pydantic writes a Literal as {"enum": [...], "type": "string"}: a wrong value fails both. Keep the
  // sentence that names the allowed values. Only on the same node, as the backend does: an `enum` inside an
  // `allOf` is another node, and the wrong type is still worth saying.
  if (typeError && pinned) return out.filter((e) => e !== typeError);
  return out;
}

const key = (path: Seg[]) => path.map(String);
function byPath(a: Err, b: Err): number {
  const x = key(a.path);
  const y = key(b.path);
  for (let i = 0; i < Math.min(x.length, y.length); i++) {
    if (x[i]! < y[i]!) return -1;
    if (x[i]! > y[i]!) return 1;
  }
  return x.length - y.length;
}

/** Problems with `options`, or none when the parser reports no schema. */
export function optionProblems(schema: unknown, options: unknown): OptionProblem[] {
  if (!isObject(schema)) return [];
  const found: OptionProblem[] = [];
  for (const e of validate(schema, schema, options ?? {}, []).sort(byPath)) {
    const where = e.path.map((p) => (typeof p === "string" ? `.${p}` : `[${p}]`)).join("");
    if (e.kind === "additionalProperties" && e.keys) for (const k of e.keys) found.push({ suffix: `${where}.${k}`, message: "This parser has no such option." });
    else if (e.kind === "required" && e.keys) for (const k of e.keys) found.push({ suffix: `${where}.${k}`, message: "Is required." });
    else found.push({ suffix: where, message: e.message });
  }
  return found;
}
