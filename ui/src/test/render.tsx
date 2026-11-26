import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { setupServer } from "msw/node";
import { createMemoryRouter, RouterProvider } from "react-router";
import { setCsrf } from "../api/client";
import { ToastProvider } from "../components/Toast";
import { loadScenario } from "../mocks/db";
import { handlers, setLatency } from "../mocks/handlers";
import type { ScenarioName } from "../mocks/scenarios";
import { routes } from "../routes";

export const server = setupServer(...handlers);

beforeAll(() => {
  setLatency(0, 0);
  server.listen();
});
afterEach(() => {
  server.resetHandlers();
  setCsrf(null);
});
afterAll(() => server.close());

/** Render the whole app at `path` against the mock API in the given scenario. */
export function renderApp(path: string, scenario: ScenarioName = "ready", setup?: (state: ReturnType<typeof loadScenario>) => void) {
  const state = loadScenario(scenario);
  setup?.(state);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <RouterProvider router={router} />
      </ToastProvider>
    </QueryClientProvider>,
  );
  return { ...utils, router, state, queryClient };
}
