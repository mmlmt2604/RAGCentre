import "@fontsource/geist/400.css";
import "@fontsource/geist/500.css";
import "@fontsource/geist/600.css";
import "@fontsource/geist-mono/400.css";
import "@fontsource/geist-mono/500.css";
import "./styles/tokens.css";
import "./styles/base.css";
import "./styles/components.css";
import "./styles/shell.css";
import "./styles/pipelines.css";
import "./styles/forms.css";
import "./styles/documents.css";
import "./styles/builder.css";
import "./styles/query.css";
import "./styles/eval.css";
import "./styles/ops.css";
import "./styles/ocr.css";
import "./styles/welcome.css";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router";
import { ApiError } from "./api/client";
import { ToastProvider } from "./components/Toast";
import { initTheme } from "./lib/theme";
import { routes } from "./routes";

initTheme();

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 1000,
      refetchOnWindowFocus: true,
      // Retry network blips, never 4xx answers.
      retry: (failures, error) => failures < 2 && error instanceof ApiError && (error.isNetwork || error.status >= 500),
    },
  },
});

async function start() {
  if (import.meta.env.DEV && import.meta.env.VITE_MOCK !== "0") {
    const { startMock } = await import("./mocks/browser");
    await startMock();
  }
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <ToastProvider>
          <RouterProvider router={createBrowserRouter(routes)} />
        </ToastProvider>
      </QueryClientProvider>
    </StrictMode>,
  );
}

void start();
