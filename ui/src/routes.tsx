import { AuthGate } from "./features/auth/AuthGate";
import { AccountPage, SetupPage } from "./features/auth/AuthForms";
import { InitializePage, JoinPage, RecoveryPage } from "./features/auth/LinkPages";
import { SettingsLayout, TeamSettingsOverview, InstanceSettingsOverview } from "./features/members/SettingsLayout";
import { TeamSetupPage } from "./features/welcome/TeamSetupPage";
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

export const routes: RouteObject[] = [
  { path: "setup", element: wrap(<SetupPage />) },
  { path: "join", element: wrap(<JoinPage />) },
  { path: "recover", element: wrap(<RecoveryPage />) },
  { path: "initialize", element: wrap(<InitializePage />) },
  { element: <AuthGate />, children: [
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
          { path: "members", element: <Navigate to="/settings/team/members" replace /> },
          { path: "team-tokens", element: <Navigate to="/settings/team/tokens" replace /> },
          { path: "users", element: <Navigate to="/settings/instance/accounts" replace /> },
          { path: "teams", element: <Navigate to="/settings/instance/teams" replace /> },
          { path: "admin", element: wrap(<Require capability="platform"><AdminPage /></Require>) },
          { path: "settings/team", element: wrap(<Require capability="manage"><SettingsLayout scope="team" /></Require>), children: [
            { index: true, element: wrap(<TeamSettingsOverview />) },
            { path: "members", element: wrap(<MembersPage />) },
            { path: "tokens", element: wrap(<TeamTokensPage />) },
            { path: "setup", element: wrap(<TeamSetupPage />) },
          ] },
          { path: "settings/instance", element: wrap(<Require capability="platform"><SettingsLayout scope="instance" /></Require>), children: [
            { index: true, element: wrap(<InstanceSettingsOverview />) },
            { path: "accounts", element: wrap(<UsersPage />) },
            { path: "teams", element: wrap(<TeamsPage />) },
            { path: "health", element: wrap(<AdminPage />) },
          ] },
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
