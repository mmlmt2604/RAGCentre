import { setupWorker } from "msw/browser";
import { initialScenario, loadScenario, startSimulation } from "./db";
import { handlers } from "./handlers";

export async function startMock(): Promise<void> {
  loadScenario(initialScenario());
  startSimulation();
  const worker = setupWorker(...handlers);
  await worker.start({ onUnhandledFrame: "bypass", quiet: true });
}
