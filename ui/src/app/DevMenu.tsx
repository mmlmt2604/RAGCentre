// Development-only controls for the mock API. Never bundled when VITE_MOCK=0 or in production.
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Button } from "../components/Button";
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
      <Button size="sm" variant="ghost" onClick={() => {
        const state = db();
        const url = state.inContainer ? "http://docling:8701" : "http://127.0.0.1:8701";
        if (!state.runningShims.includes(url) && !state.startingShims.some((shim) => shim.url === url)) {
          state.startingShims.push({ url, at: Date.now() + 5000 });
        }
      }}>
        Start a shim in 5 s
      </Button>
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
