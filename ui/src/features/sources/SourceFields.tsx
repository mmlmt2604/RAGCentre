import { useState } from "react";
import { Checkbox, Field, Select, TextInput } from "../../components/Field";
import { parseJsonObject } from "./model";
import { broadScopeMessage, SCHEDULES, schedulePreset, type SchedulePreset, type ScopeSchema } from "./scopeModel";

export function ScopeFields({ value, onChange, schema, error, confirmed, onConfirm }: {
  value: string; onChange: (value: string) => void; schema: ScopeSchema | null;
  error?: string; confirmed: boolean; onConfirm: (value: boolean) => void;
}) {
  const [advanced, setAdvanced] = useState(false);
  const parsed = parseJsonObject(value);
  const hasUnsupportedValue = schema && Object.keys(schema.properties).some((key) => parsed.value?.[key] != null && typeof parsed.value[key] !== "string");
  const showJson = advanced || !schema || !parsed.value || hasUnsupportedValue;
  const broad = broadScopeMessage(value, schema);
  const change = (next: string) => { onConfirm(false); onChange(next); };
  return <div className="source-scope-fields">
    {schema ? <Checkbox label="Advanced scope JSON" checked={!!showJson} onChange={(e) => setAdvanced(e.target.checked)} /> : null}
    {showJson ? <Field label="Scope JSON" error={error} hint="Connector-specific scope. Additional fields are kept when switching to the supported fields.">
      {(a11y) => <textarea {...a11y} className="rc-input mono" rows={5} spellCheck={false} value={value} onChange={(e) => change(e.target.value)} />}
    </Field> : <>
      {Object.entries(schema!.properties).map(([key, field]) => <Field key={key} label={`${field.title}${schema!.required.includes(key) ? " *" : ""}`} hint={field.description} error={error?.startsWith(field.title) ? error : undefined}>
        {(a11y) => <TextInput {...a11y} mono value={typeof parsed.value?.[key] === "string" ? parsed.value[key] as string : ""} required={schema!.required.includes(key)} onChange={(e) => {
          const next = { ...parsed.value };
          if (e.target.value === "") delete next[key]; else next[key] = e.target.value;
          change(JSON.stringify(next, null, 2));
        }} />}
      </Field>)}
      {error && !Object.values(schema!.properties).some((field) => error.startsWith(field.title)) ? <p className="rc-field__error" role="alert">{error}</p> : null}
    </>}
    {broad ? <div className="prov-new__note">
      <p>{broad}</p>
      <Checkbox label="I confirm this broad import scope" checked={confirmed} onChange={(e) => onConfirm(e.target.checked)} />
    </div> : null}
    <p className="rc-field__hint">Connection checks verify the saved provider. A sync determines whether this source's bucket, folder or pages can be read.</p>
  </div>;
}

export function ScheduleFields({ value, onChange, error }: { value: string; onChange: (value: string) => void; error?: string }) {
  const [custom, setCustom] = useState(() => schedulePreset(value) === "custom");
  const preset = custom ? "custom" : schedulePreset(value);
  return <>
    <Field label="Schedule" hint="Manual sources run only when you choose Sync now. Scheduled sources require a running scheduler.">
      {(a11y) => <Select {...a11y} value={preset} onChange={(e) => {
        const next = e.target.value as SchedulePreset;
        setCustom(next === "custom");
        if (next !== "custom") onChange(String(SCHEDULES[next]));
      }}>
        <option value="manual">Manual</option><option value="hourly">Hourly</option><option value="daily">Daily</option><option value="custom">Custom</option>
      </Select>}
    </Field>
    {preset === "custom" ? <Field label="Custom interval (seconds)" error={error} hint="Use any supported whole number of seconds; 0 means manual.">
      {(a11y) => <TextInput {...a11y} mono inputMode="numeric" value={value} onChange={(e) => onChange(e.target.value)} />}
    </Field> : error ? <p className="rc-field__error" role="alert">{error}</p> : null}
  </>;
}
