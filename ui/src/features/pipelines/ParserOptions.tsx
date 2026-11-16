import { RotateCcw } from "lucide-react";
import { useId, useState } from "react";
import { Button, IconButton } from "../../components/Button";
import { Checkbox, Field, Select, TextArea, TextInput } from "../../components/Field";
import { NumberInput } from "../../components/NumberInput";
import { isObject, type OptionSpec } from "./optionSpecs";

type Options = Record<string, unknown>;

const show = (v: unknown) => (v === null ? "none" : typeof v === "string" ? JSON.stringify(v) : Array.isArray(v) || isObject(v) ? JSON.stringify(v) : String(v));

function hintFor(s: OptionSpec): string | undefined {
  const parts: string[] = [];
  if (s.description) parts.push(/[.!?]$/.test(s.description) ? s.description : `${s.description}.`);
  // A null default is "unset", which the placeholder already says; a description that states its default says it better.
  if (s.default !== undefined && s.default !== null && !/\bdefault\b/i.test(s.description ?? "")) parts.push(`Default ${show(s.default)}.`);
  return parts.join(" ") || undefined;
}

const code = (key: string) => <code className="mono">{key}</code>;

/** Parse the text of a JSON box: a blank box is unset, text that is not JSON is an error to show, not a value. */
function readJson(text: string): { ok: true; value: unknown } | { ok: false } {
  const t = text.trim();
  if (!t) return { ok: true, value: undefined };
  try {
    return { ok: true, value: JSON.parse(t) };
  } catch {
    return { ok: false };
  }
}

/** One property edited as JSON text; a blank box means unset. */
function JsonKey(props: { spec: OptionSpec; value: unknown; onChange: (v: unknown) => void; error?: string }) {
  // Keyed by the value, so a change made elsewhere (Reset, the JSON view) starts the box afresh.
  return <JsonKeyBox key={props.value === undefined ? "" : JSON.stringify(props.value)} {...props} />;
}

function JsonKeyBox({ spec, value, onChange, error }: { spec: OptionSpec; value: unknown; onChange: (v: unknown) => void; error?: string }) {
  const text = value === undefined ? "" : JSON.stringify(value);
  const [invalid, setInvalid] = useState(false);
  return (
    <Field label={code(spec.key)} hint={hintFor(spec) ?? "A JSON value."} error={invalid ? "Not valid JSON." : (error ?? null)}>
      {(a11y) => (
        <TextArea
          {...a11y}
          mono
          defaultValue={text}
          placeholder="not set"
          spellCheck={false}
          onBlur={(e) => {
            const r = readJson(e.target.value);
            setInvalid(!r.ok);
            if (r.ok) onChange(r.value);
          }}
        />
      )}
    </Field>
  );
}

/** A comma-separated list of strings. It keeps what was typed while the field has focus. */
function StringList({ spec, value, onChange, error }: { spec: OptionSpec; value: unknown; onChange: (v: string[] | undefined) => void; error?: string }) {
  const [draft, setDraft] = useState<string | null>(null);
  const shown = Array.isArray(value) ? value.join(", ") : "";
  return (
    <Field label={code(spec.key)} hint={hintFor(spec) ? `${hintFor(spec)} Separate items with commas.` : "Separate items with commas."} error={error ?? null}>
      {(a11y) => (
        <TextInput
          {...a11y}
          mono
          value={draft ?? shown}
          placeholder="not set"
          onFocus={() => setDraft(shown)}
          onBlur={() => setDraft(null)}
          onChange={(e) => {
            setDraft(e.target.value);
            const items = e.target.value.split(",").map((s) => s.trim()).filter(Boolean);
            onChange(items.length ? items : undefined);
          }}
        />
      )}
    </Field>
  );
}

/** A short list of JSON values (`true`, `false`, `"auto"`) as a select; "Not set" leaves the option out. */
function ChoiceSelect({ spec, choices, value, onChange, error }: { spec: OptionSpec; choices: (boolean | string | number)[]; value: unknown; onChange: (v: unknown) => void; error?: string }) {
  const keyOf = (v: unknown) => JSON.stringify(v);
  const current = value === undefined ? "" : keyOf(value);
  const listed = choices.some((v) => keyOf(v) === current);
  return (
    <Field label={code(spec.key)} hint={hintFor(spec)} error={error ?? null}>
      {(a11y) => (
        <Select {...a11y} value={current} onChange={(e) => onChange(e.target.value === "" ? undefined : (JSON.parse(e.target.value) as unknown))}>
          <option value="">{spec.default !== undefined && spec.default !== null ? `Not set (${show(spec.default)})` : "Not set"}</option>
          {choices.map((v) => (
            <option key={keyOf(v)} value={keyOf(v)}>
              {keyOf(v)}
            </option>
          ))}
          {current && !listed ? <option value={current}>{current} (not allowed)</option> : null}
        </Select>
      )}
    </Field>
  );
}

/**
 * `[first, last]` as two number boxes. Only a complete, ordered pair is written to the options; while one
 * box is empty (or the order is wrong) what was typed is kept here and the route is left without the option.
 */
function PairInput({ spec, min, value, onChange, error }: { spec: OptionSpec; min?: number; value: unknown; onChange: (v: unknown) => void; error?: string }) {
  const complete = Array.isArray(value) && value.length === 2 && value.every((n) => typeof n === "number") ? (value as [number, number]) : null;
  const [partial, setPartial] = useState<[number | undefined, number | undefined] | null>(null);
  const [first, last] = complete ?? partial ?? [undefined, undefined];
  const put = (a: number | undefined, b: number | undefined) => {
    if (a === undefined && b === undefined) {
      setPartial(null);
      onChange(undefined);
    } else if (a !== undefined && b !== undefined && a <= b) {
      setPartial(null);
      onChange([a, b]);
    } else {
      setPartial([a, b]);
      onChange(undefined);
    }
  };
  const local =
    first !== undefined && last !== undefined && first > last ? "The first number must not be larger than the last." : partial ? "Enter both numbers, or clear both." : null;
  const shown = error ?? local;
  const messageId = useId();
  // Both boxes are described by the one message under them; a problem marks both as invalid.
  const note = shown ? messageId : hintFor(spec) ? messageId : undefined;
  return (
    <fieldset className="opt-pair" aria-describedby={note}>
      <legend className="rc-field__label">{code(spec.key)}</legend>
      <div className="opt-pair__row">
        <NumberInput label="first" min={min} value={first} onChange={(v) => put(v, last)} placeholder="first" describedBy={note} invalid={Boolean(shown)} />
        <NumberInput label="last" min={min} value={last} onChange={(v) => put(first, v)} placeholder="last" describedBy={note} invalid={Boolean(shown)} />
      </div>
      {shown ? (
        <p className="rc-field__error" id={messageId}>
          {shown}
        </p>
      ) : hintFor(spec) ? (
        <p className="rc-field__hint" id={messageId}>
          {hintFor(spec)}
        </p>
      ) : null}
    </fieldset>
  );
}

function OptionControl({ spec, value, onChange, error }: { spec: OptionSpec; value: unknown; onChange: (v: unknown) => void; error?: string }) {
  const c = spec.control;
  const hint = hintFor(spec);
  if (c.kind === "boolean")
    return (
      <Field hint={hint} error={error ?? null}>
        {(a11y) => (
          <Checkbox
            {...a11y}
            checked={typeof value === "boolean" ? value : spec.default === true}
            onChange={(e) => onChange(e.target.checked)}
            label={code(spec.key)}
          />
        )}
      </Field>
    );
  if (c.kind === "enum")
    return (
      <Field label={code(spec.key)} hint={hint} error={error ?? null}>
        {(a11y) => (
          <Select {...a11y} value={typeof value === "string" ? value : ""} onChange={(e) => onChange(e.target.value || undefined)}>
            <option value="">{spec.default !== undefined && spec.default !== null ? `Not set (${show(spec.default)})` : "Not set"}</option>
            {c.choices.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
            {typeof value === "string" && !c.choices.includes(value) ? <option value={value}>{value} (not allowed)</option> : null}
          </Select>
        )}
      </Field>
    );
  if (c.kind === "choice") return <ChoiceSelect spec={spec} choices={c.choices} value={value} onChange={onChange} error={error} />;
  if (c.kind === "pair") return <PairInput spec={spec} min={c.min} value={value} onChange={onChange} error={error} />;
  if (c.kind === "integer" || c.kind === "number")
    return (
      <NumberInput
        label={code(spec.key)}
        integer={c.kind === "integer"}
        min={c.min}
        max={c.max}
        exclusiveMin={c.exclusiveMin}
        exclusiveMax={c.exclusiveMax}
        hint={hint}
        placeholder={typeof spec.default === "number" ? String(spec.default) : "not set"}
        value={typeof value === "number" ? value : undefined}
        onChange={onChange}
        error={error}
      />
    );
  if (c.kind === "string")
    return (
      <Field label={code(spec.key)} hint={hint} error={error ?? null}>
        {(a11y) => (
          <TextInput
            {...a11y}
            value={typeof value === "string" ? value : ""}
            placeholder={typeof spec.default === "string" && spec.default ? spec.default : "not set"}
            onChange={(e) => onChange(e.target.value === "" ? undefined : e.target.value)}
          />
        )}
      </Field>
    );
  if (c.kind === "strings") return <StringList spec={spec} value={value} onChange={onChange} error={error} />;
  return <JsonKey spec={spec} value={value} onChange={onChange} error={error} />;
}

/** The whole-object JSON box: the same control the parse route has when its parser reports no schema. */
export function JsonOptions(props: { value: Options | undefined; onChange: (v: Options | undefined) => void; label: string; error?: string | null }) {
  return <JsonOptionsBox key={props.value && Object.keys(props.value).length ? JSON.stringify(props.value) : ""} {...props} />;
}

function JsonOptionsBox({ value, onChange, label, error }: { value: Options | undefined; onChange: (v: Options | undefined) => void; label: string; error?: string | null }) {
  const text = value && Object.keys(value).length ? JSON.stringify(value) : "";
  const [invalid, setInvalid] = useState<string | null>(null);
  return (
    <Field label={label} hint='A JSON object passed to the service, e.g. {"ocr": true}. Leave blank for none.' error={invalid ?? error ?? null}>
      {(a11y) => (
        <TextInput
          {...a11y}
          mono
          defaultValue={text}
          placeholder="{}"
          onBlur={(e) => {
            const r = readJson(e.target.value);
            if (!r.ok) return setInvalid("Not valid JSON.");
            if (r.value !== undefined && !isObject(r.value)) return setInvalid('Must be a JSON object, like {"ocr": true}.');
            setInvalid(null);
            onChange(r.value as Options | undefined);
          }}
        />
      )}
    </Field>
  );
}

/**
 * A parse route's options, edited as a form built from the parser's `options_schema`.
 * An unset control is absent from the options: defaults are shown, never written (they would change
 * the fingerprint). `problems` have paths relative to the options object: "", ".key", ".key[0]".
 */
export function ParserOptions({
  specs,
  value,
  onChange,
  problems,
}: {
  specs: OptionSpec[];
  value: Options | undefined;
  onChange: (v: Options | undefined) => void;
  problems: { path: string; message: string }[];
}) {
  const [asJson, setAsJson] = useState(false);
  const current = value ?? {};
  const known = new Set(specs.map((s) => s.key));
  const unknown = Object.keys(current).filter((k) => !known.has(k));
  const errFor = (key: string) => problems.find((p) => p.path === `.${key}` || p.path.startsWith(`.${key}.`) || p.path.startsWith(`.${key}[`))?.message;
  const whole = problems.find((p) => p.path === "")?.message;

  const set = (key: string, v: unknown) => {
    const next = { ...current };
    if (v === undefined) delete next[key];
    else next[key] = v;
    onChange(Object.keys(next).length ? next : undefined);
  };

  const toggle = (
    <Button size="sm" variant="ghost" aria-pressed={asJson} onClick={() => setAsJson(!asJson)}>
      {asJson ? "Edit as form" : "Edit as JSON"}
    </Button>
  );

  if (asJson)
    return (
      <div className="opts">
        <div className="opts__head">
          <span className="opts__title">Options</span>
          {toggle}
        </div>
        <JsonOptions
          label="Options as JSON"
          value={value}
          onChange={onChange}
          error={problems.length ? problems.map((p) => `${p.path.replace(/^\./, "") || "options"}: ${p.message}`).join(" ") : null}
        />
      </div>
    );

  return (
    <div className="opts" role="group" aria-label="Parser options">
      <div className="opts__head">
        <span className="opts__title">Options</span>
        <span className="opts__note">Only the options you set are sent; the rest use the parser's defaults.</span>
        {toggle}
      </div>
      {whole ? <p className="rc-field__error">{whole}</p> : null}
      <div className="cfg-grid">
        {specs.map((s) => (
          <div key={s.key} className="opt">
            <OptionControl spec={s} value={current[s.key]} onChange={(v) => set(s.key, v)} error={errFor(s.key)} />
            {s.key in current ? (
              <span className="opt__reset">
                <IconButton icon={RotateCcw} label={`Reset ${s.key}`} onClick={() => set(s.key, undefined)} />
              </span>
            ) : null}
          </div>
        ))}
        {unknown.map((k) => (
          <div key={k} className="opt">
            <JsonKey spec={{ key: k, control: { kind: "json" }, description: "This parser does not list this option.", default: undefined }} value={current[k]} onChange={(v) => set(k, v)} error={errFor(k)} />
            <span className="opt__reset">
              <IconButton icon={RotateCcw} label={`Remove ${k}`} onClick={() => set(k, undefined)} />
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
