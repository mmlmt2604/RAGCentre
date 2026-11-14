// The single mock state instance, and the scenario selection.
import { reapDeleted } from "./documents";
import { tick } from "./engine";
import { advanceRuns } from "./eval";
import { buildScenario, SCENARIOS, type ScenarioName } from "./scenarios";
import type { MockState } from "./store";

const SCENARIO_KEY = "ragcentre.mock.scenario";

let state: MockState = buildScenario("ready");
let current: ScenarioName = "ready";

export function db(): MockState {
  return state;
}

export function currentScenario(): ScenarioName {
  return current;
}

export function loadScenario(name: ScenarioName): MockState {
  current = name;
  state = buildScenario(name);
  return state;
}

/** Scenario from `?scenario=`, then the remembered one, then "ready". */
export function initialScenario(): ScenarioName {
  const fromUrl = new URLSearchParams(window.location.search).get("scenario");
  if (fromUrl && (SCENARIOS as readonly string[]).includes(fromUrl)) {
    rememberScenario(fromUrl as ScenarioName);
    return fromUrl as ScenarioName;
  }
  try {
    const saved = localStorage.getItem(SCENARIO_KEY);
    if (saved && (SCENARIOS as readonly string[]).includes(saved)) return saved as ScenarioName;
  } catch {
    /* storage unavailable */
  }
  return "ready";
}

export function rememberScenario(name: ScenarioName): void {
  try {
    localStorage.setItem(SCENARIO_KEY, name);
  } catch {
    /* storage unavailable */
  }
}

let timer: ReturnType<typeof setInterval> | null = null;

/** Run the ingestion simulator in the background (browser only). */
export function startSimulation(intervalMs = 900): void {
  if (timer) return;
  timer = setInterval(() => {
    tick(state);
    reapDeleted(state);
    advanceRuns(state, 6);
  }, intervalMs);
}

export function stopSimulation(): void {
  if (timer) clearInterval(timer);
  timer = null;
}
