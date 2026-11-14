import { ApiError } from "../api/client";

/** Actionable next steps for error codes a user can do something about (§21.2). */
const NEXT_STEP: Record<string, string> = {
  network_error: "Check that the RAGCentre server is running, then retry.",
  index_incomplete: "This pipeline is still building. Wait for it to finish, or query it anyway.",
  index_not_built: "This pipeline's indexes have not been built yet. Check its status page.",
  index_mismatch: "The physical index no longer matches this pipeline. Rebuild it by cloning the pipeline.",
  embedding_dimension_mismatch: "The embedder now returns a different vector size than the index. Create a new provider and pipeline.",
  pipeline_archived: "Archived pipelines cannot be used. Pick another pipeline.",
  pipeline_is_active: "Make another pipeline active first, then archive this one.",
  provider_in_use: "Archive the pipelines that use this provider first.",
  provider_error: "A provider service failed. Check the provider and retry.",
  provider_version_drift: "The service now reports a different version. Create a new provider pinned to it.",
  secret_storage_disabled: "Set RAGCENTRE_SECRET_KEY on the server, or read the secret from an environment variable.",
  unauthorized: "Enter the server's access token.",
  invalid_filter: "Fix the filter: use only this corpus's metadata fields, with values of the field's type.",
  immutable_field: "This setting is permanent. Create a new provider instead.",
};

export function nextStep(error: unknown): string | null {
  return error instanceof ApiError ? (NEXT_STEP[error.code] ?? null) : null;
}
