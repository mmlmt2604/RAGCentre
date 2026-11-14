// Turn a provider type's `config_schema` (JSON Schema from pydantic, or the mock's simpler form)
// into form fields, and convert and validate form values. Used by the provider forms and the mock.

export type FieldKind = "string" | "integer" | "number" | "boolean" | "enum";

export interface ConfigField {
  key: string;
  label: string;
  description: string | null;
  kind: FieldKind;
  options: string[];
  required: boolean;
  nullable: boolean; // optional with no default: empty means "not set" (null)
  default: unknown;
  min: number | null;
  max: number | null;
  exclusiveMin: number | null;
  minLength: number | null;
  format: string | null;
}

/** Fields the server fills in itself; never shown as inputs. */
export const SERVER_FIELDS = new Set(["impl_version", "service_name", "service_version"]);

const ACRONYMS: Record<string, string> = { url: "URL", api: "API", id: "ID", dsn: "DSN", http: "HTTP", uri: "URI" };

export function humanize(key: string): string {
  const words = key.split("_").map((w) => ACRONYMS[w] ?? w);
  const first = words[0] ?? "";
  return [first.charAt(0).toUpperCase() + first.slice(1), ...words.slice(1)].join(" ");
}

type Json = Record<string, unknown>;

function num(v: unknown): number | null {
  return typeof v === "number" ? v : null;
}

export function configFields(schema: Record<string, unknown> | undefined): ConfigField[] {
  const props = (schema?.properties ?? {}) as Record<string, Json>;
  const required = new Set((schema?.required as string[] | undefined) ?? []);
  return Object.entries(props)
    .filter(([key]) => !SERVER_FIELDS.has(key))
    .map(([key, raw]) => {
      // pydantic writes `X | None` as anyOf [X, {type: null}]
      const variants = Array.isArray(raw.anyOf) ? (raw.anyOf as Json[]) : [raw];
      const main = variants.find((v) => v.type !== "null") ?? raw;
      const nullable = variants.some((v) => v.type === "null");
      const options = Array.isArray(main.enum) ? (main.enum as unknown[]).map(String) : [];
      const t = main.type;
      const kind: FieldKind = options.length ? "enum" : t === "integer" ? "integer" : t === "number" ? "number" : t === "boolean" ? "boolean" : "string";
      const description = typeof raw.description === "string" ? raw.description : null;
      return {
        key,
        label: humanize(key),
        description,
        kind,
        options,
        required: required.has(key),
        nullable: nullable || (!required.has(key) && !("default" in raw) && kind !== "boolean"),
        default: raw.default,
        min: num(main.minimum),
        max: num(main.maximum),
        exclusiveMin: num(main.exclusiveMinimum),
        minLength: num(main.minLength),
        format: typeof main.format === "string" ? main.format : null,
      };
    });
}

/** Form state is strings for inputs and booleans for checkboxes. */
export type FormValues = Record<string, string | boolean>;

export function initialValues(fields: ConfigField[], config: Record<string, unknown> = {}): FormValues {
  const out: FormValues = {};
  for (const f of fields) {
    const v = f.key in config ? config[f.key] : f.default;
    if (f.kind === "boolean") out[f.key] = v === true;
    else out[f.key] = v === null || v === undefined ? "" : String(v);
  }
  return out;
}

const URL_RE = /^https?:\/\/[^\s/]+/i;

/** Convert form values to a config object, or return per-field errors. Empty optional fields are omitted. */
export function toConfig(fields: ConfigField[], values: FormValues): { config: Record<string, unknown>; errors: Record<string, string> } {
  const config: Record<string, unknown> = {};
  const errors: Record<string, string> = {};
  for (const f of fields) {
    const v = values[f.key];
    if (f.kind === "boolean") {
      config[f.key] = v === true;
      continue;
    }
    const text = typeof v === "string" ? v.trim() : "";
    if (text === "") {
      if (f.required) errors[f.key] = `${f.label} is required.`;
      else if (f.nullable && f.default === null) config[f.key] = null;
      // otherwise omitted: the server applies the default
      continue;
    }
    if (f.kind === "integer" || f.kind === "number") {
      const n = Number(text);
      if (!Number.isFinite(n) || (f.kind === "integer" && !Number.isInteger(n))) {
        errors[f.key] = f.kind === "integer" ? "Enter a whole number." : "Enter a number.";
        continue;
      }
      if (f.min !== null && n < f.min) errors[f.key] = `Must be at least ${f.min}.`;
      else if (f.exclusiveMin !== null && n <= f.exclusiveMin) errors[f.key] = `Must be more than ${f.exclusiveMin}.`;
      else if (f.max !== null && n > f.max) errors[f.key] = `Must be at most ${f.max}.`;
      else config[f.key] = n;
      continue;
    }
    if (f.kind === "enum" && !f.options.includes(text)) {
      errors[f.key] = `Choose one of ${f.options.join(", ")}.`;
      continue;
    }
    if ((f.format === "uri" || f.key.endsWith("url")) && !URL_RE.test(text)) {
      errors[f.key] = "Enter a URL starting with http:// or https://.";
      continue;
    }
    if (f.minLength !== null && text.length < f.minLength) {
      errors[f.key] = `${f.label} is required.`;
      continue;
    }
    config[f.key] = text;
  }
  return { config, errors };
}

/** The settings worth showing in a list row, in order of usefulness. */
export function keySettings(config: Record<string, unknown>): string {
  for (const key of ["model", "base_url", "url", "host", "path", "schema"]) {
    const v = config[key];
    if (typeof v === "string" && v) return v;
  }
  if (typeof config.dimensions === "number") return `${config.dimensions} dimensions`;
  return "";
}

export const PROVIDER_NAME_RE = /^[a-z][a-z0-9-]{0,62}$/;
export const PROVIDER_NAME_HINT = "Lowercase letters, digits and hyphens, starting with a letter. Pipelines refer to providers by this name.";

export const METADATA_FIELD_RE = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;

/** A config value for read-only display. */
export function display(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "boolean") return v ? "true" : "false";
  return String(v);
}
