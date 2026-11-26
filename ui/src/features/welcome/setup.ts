import type { ProviderCreate } from "../../api/types";

export type SetupTask = "parser" | "embedder" | "store" | "corpus" | "pipeline" | "active" | "check_services";
export interface ProviderPick { reuse?: string | null; create?: ProviderCreate | null; }
export interface SetupDraft {
  mode: "trial" | "custom";
  corpus_name: string;
  pipeline_name: string;
  retrieval: "hybrid" | "dense";
  parser: ProviderPick | null;
  embedder: ProviderPick | null;
  store: ProviderPick | null;
  dismissed: boolean;
  choice_step: number;
}
export interface SetupOperation {
  id: string;
  input_version: number;
  input_hash: string;
  draft: SetupDraft;
  steps: { step: SetupTask; state: "pending" | "running" | "succeeded" | "failed"; resource_id: string | null; attempts: number; error_code: string | null; lease_expires_at: string | null }[];
  created_at: string;
  updated_at: string;
}
export interface SetupReadiness {
  owner: boolean;
  corpus_id: string | null;
  pipeline_id: string | null;
  pipeline_fingerprint: string | null;
  services: boolean;
  providers: { id: string; name: string; credentials: Record<string, boolean>; verified: boolean }[];
  indexed_documents: number;
  first_result_at: string | null;
  ready: boolean;
}
export interface SetupState {
  draft: { version: number; draft: SetupDraft };
  draft_hash: string;
  operation: SetupOperation | null;
  readiness: SetupReadiness;
}
