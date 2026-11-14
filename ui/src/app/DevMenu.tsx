// Development-only controls for the mock API. Never bundled when VITE_MOCK=0 or in production.
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Checkbox, Select } from "../components/Field";
import { currentScenario, db, rememberScenario } from "../mocks/db";
import { DEV_TOKEN, SCENARIO_LABELS, SCENARIOS, type ScenarioName } from "../mocks/scenarios";

export default function DevMenu() {
  const qc = useQueryClient();
  const [failing, setFailing] = useState(db().failNetwork);

  return (
    <span className="dev-menu" title={`Mock API. In the auth scenario the token is "${DEV_TOKEN}".`}>
      <span className="dev-menu__tag">Mock</span>
      <Select
        aria-label="Mock scenario"
        value={currentScenario()}
        onChange={(e) => {
          rememberScenario(e.target.value as ScenarioName);
          const url = new URL(window.location.href);
          url.searchParams.set("scenario", e.target.value);
          window.location.assign(url.toString());
        }}
      >
        {SCENARIOS.map((s) => (
          <option key={s} value={s}>
            {SCENARIO_LABELS[s]}
          </option>
        ))}
      </Select>
      <Checkbox
        label="Fail requests"
        checked={failing}
        onChange={(e) => {
          db().failNetwork = e.target.checked;
          setFailing(e.target.checked);
          void qc.invalidateQueries();
        }}
      />
    </span>
  );
}
