import { AuthGate } from "./features/auth/AuthGate";
import { AccountPage, SetupPage } from "./features/auth/AuthForms";
import { Require } from "./features/auth/Access";
import { MembersPage } from "./features/members/MembersPage";
import { UsersPage, TeamsPage } from "./features/members/UsersPage";
import { TeamTokensPage } from "./features/members/TeamTokensPage";
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
import { SourceDetail } from "./features/sources/SourceDetail";
import { SourceList } from "./features/sources/SourceList";

const wrap = (el: React.ReactNode) => <ErrorBoundary>{el}</ErrorBoundary>;

export const routes: RouteObject[] = [{ path: "setup", element: wrap(<SetupPage />) }, { element: <AuthGate />, children: [
  {
    element: <Shell />,
    errorElement: <RouteError />,
    children: [
      {
        element: <PageLayout />,
        children: [
          { index: true, element: wrap(<CorpusList />) },
          { path: "corpora/new", element: wrap(<Require capability="edit"><CorpusNew /></Require>) },
          { path: "providers", element: wrap(<Require capability="providers"><ProviderList /></Require>) },
          { path: "providers/new", element: wrap(<Require capability="manage"><ProviderNew /></Require>) },
          { path: "providers/:providerId", element: wrap(<Require capability="providers"><ProviderDetail /></Require>) },
          { path: "activity", element: wrap(<ActivityPage />) },
          { path: "account", element: wrap(<AccountPage />) },
          { path: "members", element: wrap(<Require capability="members"><MembersPage /></Require>) },
          { path: "team-tokens", element: wrap(<Require capability="manage"><TeamTokensPage /></Require>) },
          { path: "users", element: wrap(<Require capability="platform"><UsersPage /></Require>) },
          { path: "teams", element: wrap(<Require capability="platform"><TeamsPage /></Require>) },
          { path: "admin", element: wrap(<Require capability="platform"><AdminPage /></Require>) },
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
          { path: "sources", element: wrap(<Require capability="manage"><SourceList /></Require>) },
          { path: "sources/:sourceId", element: wrap(<Require capability="manage"><SourceDetail /></Require>) },
          { path: "pipelines", element: wrap(<PipelineList />) },
          { path: "pipelines/new", element: wrap(<Require capability="edit"><PipelineBuilder /></Require>) },
          { path: "pipelines/compare", element: wrap(<PipelineDiff />) },
          { path: "pipelines/:pipelineId", element: wrap(<PipelineDetail />) },
          { path: "query", element: wrap(<QueryPlayground />) },
          { path: "eval", element: wrap(<EvalHome />) },
          { path: "eval/datasets/:datasetId", element: wrap(<DatasetDetail />) },
          { path: "eval/runs/:runId", element: wrap(<RunComparison />) },
          { path: "eval/runs/:runId/items", element: wrap(<ItemDiff />) },
          { path: "settings", element: wrap(<Require capability="edit"><CorpusSettings /></Require>) },
          { path: "*", element: <NotFound /> },
        ],
      },
    ],
  },
] }];
