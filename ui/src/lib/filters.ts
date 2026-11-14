// The metadata filter language (IMPLEMENTATION_PLAN.md §10.2): validation with the backend's
// messages, a matcher (used by the mock), and the row model of the query screen's filter builder.
import type { FieldType, MetadataSchema, Scalar } from "../api/types";

export type Op = "$eq" | "$ne" | "$gt" | "$gte" | "$lt" | "$lte" | "$in" | "$nin";
export const RANGE_OPS: Op[] = ["$gt", "$gte", "$lt", "$lte"];
export const LIST_OPS: Op[] = ["$in", "$nin"];
const OPS = new Set<string>(["$eq", "$ne", ...RANGE_OPS, ...LIST_OPS]);
const MAX_DEPTH = 5;
const MAX_LIST = 100;

export const OP_LABEL: Record<Op, string> = {
  $eq: "is",
  $ne: "is not",
  $gt: ">",
  $gte: "≥",
  $lt: "<",
  $lte: "≤",
  $in: "is one of",
  $nin: "is none of",
};

/** Field types for filtering: the corpus schema plus document_id. */
export function filterFields(schema: MetadataSchema): Record<string, FieldType> {
  return { ...Object.fromEntries(Object.entries(schema.fields).map(([k, v]) => [k, v.type])), document_id: "str" };
}

/** Operators that make sense for a field type: ranges only for numbers. */
export function opsFor(type: FieldType): Op[] {
  return type === "int" || type === "float" ? ["$eq", "$ne", ...RANGE_OPS, ...LIST_OPS] : ["$eq", "$ne", ...LIST_OPS];
}

function typeOk(v: unknown, t: FieldType): boolean {
  if (t === "str") return typeof v === "string";
  if (t === "bool") return typeof v === "boolean";
  if (t === "int") return typeof v === "number" && Number.isInteger(v);
  return typeof v === "number" && Number.isFinite(v);
}

export class FilterError extends Error {
  constructor(
    readonly path: string,
    message: string,
  ) {
    super(path ? `${path}: ${message}` : message);
  }
}

/** Throw a FilterError (with the backend's wording) if the filter is not valid for these fields. */
export function validateFilter(raw: unknown, fields: Record<string, FieldType>, path = "", depth = 0): void {
  if (depth > MAX_DEPTH) throw new FilterError(path, `filters may nest $and/$or at most ${MAX_DEPTH} deep`);
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new FilterError(path, "a filter must be a JSON object");
  const entries = Object.entries(raw);
  if (!entries.length) throw new FilterError(path, "a filter object must not be empty");
  for (const [key, value] of entries) {
    const sub = path ? `${path}.${key}` : key;
    if (key === "$and" || key === "$or") {
      if (!Array.isArray(value) || !value.length) throw new FilterError(sub, `${key} takes a non-empty list of filters`);
      value.forEach((item, i) => validateFilter(item, fields, `${sub}[${i}]`, depth + 1));
      continue;
    }
    if (key.startsWith("$")) throw new FilterError(sub, `unknown operator ${key}`);
    const type = fields[key];
    if (!type) throw new FilterError(sub, `unknown field '${key}'; filterable fields are: ${Object.keys(fields).sort().join(", ")}`);
    const ops = value && typeof value === "object" && !Array.isArray(value) ? Object.entries(value) : [["$eq", value] as [string, unknown]];
    if (!ops.length) throw new FilterError(sub, "an operator object must not be empty");
    for (const [op, v] of ops) {
      if (!OPS.has(op)) throw new FilterError(sub, `unknown operator ${op}`);
      if (RANGE_OPS.includes(op as Op) && type !== "int" && type !== "float") throw new FilterError(sub, `${op} applies to int and float fields only; ${key} is ${type}`);
      if (LIST_OPS.includes(op as Op)) {
        if (!Array.isArray(v) || !v.length) throw new FilterError(sub, `${op} takes a non-empty list`);
        if (v.length > MAX_LIST) throw new FilterError(sub, `${op} takes at most ${MAX_LIST} values`);
        v.forEach((x, i) => {
          if (!typeOk(x, type)) throw new FilterError(`${sub}[${i}]`, `expected a ${type} value for ${key}`);
        });
      } else if (!typeOk(v, type)) throw new FilterError(sub, `expected a ${type} value for ${key}, got ${Array.isArray(v) ? "list" : v === null ? "null" : typeof v}`);
    }
  }
}

/** Does a record's metadata satisfy the (valid) filter? Missing fields never match a comparison. */
export function matches(raw: unknown, meta: Record<string, unknown>): boolean {
  const f = raw as Record<string, unknown>;
  return Object.entries(f).every(([key, value]) => {
    if (key === "$and") return (value as unknown[]).every((x) => matches(x, meta));
    if (key === "$or") return (value as unknown[]).some((x) => matches(x, meta));
    const actual = meta[key];
    const ops = value && typeof value === "object" && !Array.isArray(value) ? Object.entries(value) : [["$eq", value] as [string, unknown]];
    return ops.every(([op, v]) => {
      switch (op) {
        case "$eq":
          return actual === v;
        case "$ne":
          return actual !== v;
        case "$in":
          return (v as unknown[]).includes(actual);
        case "$nin":
          return !(v as unknown[]).includes(actual);
        default: {
          if (typeof actual !== "number") return false;
          const n = v as number;
          return op === "$gt" ? actual > n : op === "$gte" ? actual >= n : op === "$lt" ? actual < n : actual <= n;
        }
      }
    });
  });
}

// ---------- builder rows ----------

export interface FilterRow {
  field: string;
  op: Op;
  /** As typed; lists are comma-separated. */
  value: string;
}

function parseScalar(text: string, type: FieldType): Scalar | undefined {
  const t = text.trim();
  if (type === "str") return text;
  if (type === "bool") return t === "true" ? true : t === "false" ? false : undefined;
  if (t === "") return undefined;
  const n = Number(t);
  if (!Number.isFinite(n) || (type === "int" && !Number.isInteger(n))) return undefined;
  return n;
}

/** Rows -> filter JSON, or the first row's problem. Empty rows are ignored; no rows -> null. */
export function rowsToFilter(rows: FilterRow[], combine: "all" | "any", fields: Record<string, FieldType>): { filter: Record<string, unknown> | null; errors: Record<number, string> } {
  const conds: Record<string, unknown>[] = [];
  const errors: Record<number, string> = {};
  rows.forEach((r, i) => {
    if (!r.field) return;
    const type = fields[r.field];
    if (!type) return void (errors[i] = "Choose a field.");
    if (LIST_OPS.includes(r.op)) {
      const parts = r.value.split(",").map((x) => x.trim()).filter((x) => x !== "");
      const values = parts.map((p) => parseScalar(p, type));
      if (!values.length) return void (errors[i] = "Enter one or more values, separated by commas.");
      if (values.some((v) => v === undefined)) return void (errors[i] = `Every value must be ${type === "int" ? "a whole number" : type === "float" ? "a number" : type === "bool" ? "true or false" : "text"}.`);
      conds.push({ [r.field]: { [r.op]: values } });
      return;
    }
    const v = parseScalar(r.value, type);
    if (v === undefined) return void (errors[i] = type === "bool" ? "Choose true or false." : type === "int" ? "Enter a whole number." : "Enter a number.");
    conds.push({ [r.field]: { [r.op]: v } });
  });
  if (!conds.length) return { filter: null, errors };
  return { filter: conds.length === 1 ? conds[0]! : { [combine === "all" ? "$and" : "$or"]: conds }, errors };
}

/** Filter JSON -> rows, when it is simple enough for the builder (one level of $and/$or). */
export function filterToRows(f: unknown): { rows: FilterRow[]; combine: "all" | "any" } | null {
  if (!f || typeof f !== "object" || Array.isArray(f)) return null;
  const obj = f as Record<string, unknown>;
  const keys = Object.keys(obj);
  let combine: "all" | "any" = "all";
  let items: unknown[] = [obj];
  if (keys.length === 1 && (keys[0] === "$and" || keys[0] === "$or")) {
    combine = keys[0] === "$and" ? "all" : "any";
    items = obj[keys[0]] as unknown[];
  } else if (keys.length > 1) items = keys.map((k) => ({ [k]: obj[k] }));
  const rows: FilterRow[] = [];
  for (const item of items) {
    if (!item || typeof item !== "object") return null;
    for (const [field, value] of Object.entries(item)) {
      if (field.startsWith("$")) return null;
      const ops = value && typeof value === "object" && !Array.isArray(value) ? Object.entries(value) : [["$eq", value] as [string, unknown]];
      for (const [op, v] of ops) rows.push({ field, op: op as Op, value: Array.isArray(v) ? v.join(", ") : String(v) });
    }
  }
  return { rows, combine };
}
