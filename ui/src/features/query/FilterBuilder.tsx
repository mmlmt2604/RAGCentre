import { Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import type { FieldType, MetadataSchema } from "../../api/types";
import { Button, IconButton } from "../../components/Button";
import { Segmented, Select, TextInput } from "../../components/Field";
import { filterFields, filterToRows, FilterError, LIST_OPS, OP_LABEL, opsFor, rowsToFilter, validateFilter, type FilterRow, type Op } from "../../lib/filters";

export interface FilterState {
  filter: Record<string, unknown> | null;
  error: string | null;
}

const TYPE_WORD: Record<FieldType, string> = { str: "text", int: "integer", float: "number", bool: "true/false" };

/**
 * Rows of field, operator and value, restricted to the corpus schema and to operators valid for
 * the field's type, combined with all/any. A raw JSON mode takes any filter the language allows.
 */
export function FilterBuilder({ schema, initial, onChange }: { schema: MetadataSchema; initial: Record<string, unknown> | null; onChange: (s: FilterState) => void }) {
  const fields = filterFields(schema);
  const fromInitial = initial ? filterToRows(initial) : null;
  const [mode, setMode] = useState<"rows" | "json">(initial && !fromInitial ? "json" : "rows");
  const [rows, setRows] = useState<FilterRow[]>(fromInitial?.rows ?? []);
  const [combine, setCombine] = useState<"all" | "any">(fromInitial?.combine ?? "all");
  const [json, setJson] = useState(initial ? JSON.stringify(initial, null, 2) : "");
  const [rowErrors, setRowErrors] = useState<Record<number, string>>({});
  const [jsonError, setJsonError] = useState<string | null>(null);

  const emitRows = (next: FilterRow[], c = combine) => {
    setRows(next);
    const { filter, errors } = rowsToFilter(next, c, fields);
    setRowErrors(errors);
    onChange({ filter, error: Object.values(errors)[0] ?? null });
  };
  const emitJson = (text: string) => {
    setJson(text);
    if (!text.trim()) {
      setJsonError(null);
      return onChange({ filter: null, error: null });
    }
    try {
      const v = JSON.parse(text) as unknown;
      validateFilter(v, fields);
      setJsonError(null);
      onChange({ filter: v as Record<string, unknown>, error: null });
    } catch (e) {
      const msg = e instanceof FilterError ? e.message : "Not valid JSON.";
      setJsonError(msg);
      onChange({ filter: null, error: msg });
    }
  };
  const switchMode = (m: "rows" | "json") => {
    if (m === "json") {
      const { filter } = rowsToFilter(rows, combine, fields);
      setJson(filter ? JSON.stringify(filter, null, 2) : "");
      setJsonError(null);
    } else {
      let parsed: unknown;
      try {
        parsed = json.trim() ? JSON.parse(json) : null;
      } catch {
        return setJsonError("Fix the JSON before switching to rows.");
      }
      const r = parsed ? filterToRows(parsed) : { rows: [], combine: "all" as const };
      if (!r) return setJsonError("This filter nests $and/$or more deeply than rows can show; keep editing it as JSON.");
      setRows(r.rows);
      setCombine(r.combine);
    }
    setMode(m);
  };

  return (
    <div className="fb">
      <div className="fb__head">
        {mode === "rows" && rows.length > 1 ? (
          <Segmented
            label="Combine conditions"
            value={combine}
            onChange={(c) => {
              setCombine(c);
              emitRows(rows, c);
            }}
            options={[
              { value: "all", label: "Match all" },
              { value: "any", label: "Match any" },
            ]}
          />
        ) : null}
        <span className="page-head__spacer" />
        <Segmented
          label="Filter editor"
          value={mode}
          onChange={switchMode}
          options={[
            { value: "rows", label: "Rows" },
            { value: "json", label: "JSON" },
          ]}
        />
      </div>
      {mode === "rows" ? (
        <>
          {rows.map((r, i) => {
            const type = fields[r.field];
            const ops = type ? opsFor(type) : (["$eq"] as Op[]);
            return (
              <div key={i} className="fb__row">
                <Select
                  aria-label={`Condition ${i + 1} field`}
                  value={r.field}
                  onChange={(e) => {
                    const t = fields[e.target.value];
                    emitRows(rows.map((x, j) => (j === i ? { field: e.target.value, op: t && opsFor(t).includes(x.op) ? x.op : "$eq", value: t === "bool" ? "true" : "" } : x)));
                  }}
                >
                  <option value="">Field…</option>
                  {Object.entries(fields).map(([name, t]) => (
                    <option key={name} value={name}>
                      {name} ({TYPE_WORD[t]})
                    </option>
                  ))}
                </Select>
                <Select aria-label={`Condition ${i + 1} operator`} value={r.op} onChange={(e) => emitRows(rows.map((x, j) => (j === i ? { ...x, op: e.target.value as Op } : x)))}>
                  {ops.map((o) => (
                    <option key={o} value={o}>
                      {OP_LABEL[o]}
                    </option>
                  ))}
                </Select>
                {type === "bool" && !LIST_OPS.includes(r.op) ? (
                  <Select aria-label={`Condition ${i + 1} value`} value={r.value} onChange={(e) => emitRows(rows.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)))}>
                    <option value="true">true</option>
                    <option value="false">false</option>
                  </Select>
                ) : (
                  <TextInput
                    aria-label={`Condition ${i + 1} value`}
                    mono={type === "int" || type === "float"}
                    value={r.value}
                    placeholder={LIST_OPS.includes(r.op) ? "a, b, c" : type ? TYPE_WORD[type] : "value"}
                    aria-invalid={rowErrors[i] ? true : undefined}
                    onChange={(e) => emitRows(rows.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)))}
                  />
                )}
                <IconButton icon={Trash2} label={`Remove condition ${i + 1}`} onClick={() => emitRows(rows.filter((_, j) => j !== i))} />
                {rowErrors[i] ? <p className="rc-field__error fb__err">{rowErrors[i]}</p> : null}
              </div>
            );
          })}
          <Button size="sm" variant="ghost" icon={Plus} onClick={() => setRows([...rows, { field: "", op: "$eq", value: "" }])}>
            Add condition
          </Button>
        </>
      ) : (
        <div className="rc-field">
          <textarea
            className="rc-input fb__json mono"
            aria-label="Filter as JSON"
            rows={5}
            spellCheck={false}
            value={json}
            placeholder='{"$and": [{"lang": "en"}, {"year": {"$gte": 2024}}]}'
            onChange={(e) => emitJson(e.target.value)}
          />
          {jsonError ? <p className="rc-field__error">{jsonError}</p> : <p className="rc-field__hint">Fields: {Object.keys(fields).join(", ")}. Operators: $eq $ne $gt $gte $lt $lte $in $nin, combined with $and / $or.</p>}
        </div>
      )}
    </div>
  );
}
