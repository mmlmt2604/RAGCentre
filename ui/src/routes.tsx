import { Navigate, type RouteObject } from "react-router";
import { CorpusList, ErrorBoundary, NotFound, RouteError } from "./app/pages";
import { CorpusLayout, PageLayout, Shell } from "./app/Shell";
import { ActivityPage } from "./features/activity/ActivityPage";
import { AdminPage } from "./features/admin/AdminPage";
import { CorpusNew } from "./features/corpora/CorpusNew";
import { CorpusSettings } from "./features/corpora/CorpusSettings";
import { DocumentDetail } from "./features/documents/DocumentDetail";
import { DocumentList } from "./features/documents/DocumentList";
import { PipelineBuilder } from "./features/pipelines/PipelineBuilder";
import { PipelineDetail } from "./features/pipelines/PipelineDetail";
import { PipelineDiff } from "./features/pipelines/PipelineDiff";
import { PipelineList } from "./features/pipelines/PipelineList";
import { DatasetDetail } from "./features/eval/DatasetDetail";
import { EvalHome } from "./features/eval/EvalHome";
import { ItemDiff } from "./features/eval/ItemDiff";
import { RunComparison } from "./features/eval/RunComparison";
import { QueryPlayground } from "./features/query/QueryPlayground";
import { ProviderDetail } from "./features/providers/ProviderDetail";
import { ProviderList } from "./features/providers/ProviderList";
import { ProviderNew } from "./features/providers/ProviderNew";

const wrap = (el: React.ReactNode) => <ErrorBoundary>{el}</ErrorBoundary>;

export const routes: RouteObject[] = [
  {
    element: <Shell />,
    errorElement: <RouteError />,
    children: [
      {
        element: <PageLayout />,
        children: [
          { index: true, element: wrap(<CorpusList />) },
          { path: "corpora/new", element: wrap(<CorpusNew />) },
          { path: "providers", element: wrap(<ProviderList />) },
          { path: "providers/new", element: wrap(<ProviderNew />) },
          { path: "providers/:providerId", element: wrap(<ProviderDetail />) },
          { path: "activity", element: wrap(<ActivityPage />) },
          { path: "admin", element: wrap(<AdminPage />) },
          { path: "*", element: <NotFound /> },
        ],
      },
      {
        path: "corpora/:corpusId",
        element: <CorpusLayout />,
        children: [
          { index: true, element: <Navigate to="pipelines" replace /> },
          { path: "documents", element: wrap(<DocumentList />) },
          { path: "documents/:documentId", element: wrap(<DocumentDetail />) },
          { path: "pipelines", element: wrap(<PipelineList />) },
          { path: "pipelines/new", element: wrap(<PipelineBuilder />) },
          { path: "pipelines/compare", element: wrap(<PipelineDiff />) },
          { path: "pipelines/:pipelineId", element: wrap(<PipelineDetail />) },
          { path: "query", element: wrap(<QueryPlayground />) },
          { path: "eval", element: wrap(<EvalHome />) },
          { path: "eval/datasets/:datasetId", element: wrap(<DatasetDetail />) },
          { path: "eval/runs/:runId", element: wrap(<RunComparison />) },
          { path: "eval/runs/:runId/items", element: wrap(<ItemDiff />) },
          { path: "settings", element: wrap(<CorpusSettings />) },
          { path: "*", element: <NotFound /> },
        ],
      },
    ],
  },
];
