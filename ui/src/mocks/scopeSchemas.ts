// Synthetic catalog metadata matching the bundled connectors' supported scope fields.
import type { ScopeSchema } from "../features/sources/scopeModel";
const field = (title: string, description: string) => ({ type: "string", title, description });
export const connectorScopeSchemas: Record<string, ScopeSchema> = {
  s3: {
    type: "object", required: ["bucket"],
    properties: { bucket: field("Bucket", "Bucket name without s3://."), prefix: field("Prefix", "Only keys starting with this exact prefix, for example manuals/.") },
    "x-broad-scope": { unless_any: ["prefix"], message: "Without a prefix, this source imports all supported objects in the selected bucket." },
  },
  sharepoint: {
    type: "object", required: ["drive_id"],
    properties: { drive_id: field("Drive ID", "Microsoft Graph drive ID for the document library."), folder: field("Folder", "Path within the drive; includes descendants.") },
    "x-broad-scope": { unless_any: ["folder"], message: "Without a folder, this source imports all supported files in the selected drive." },
  },
  confluence: { type: "object", required: ["space_key"], properties: { space_key: field("Space key", "Imports current pages in this space.") } },
  gdrive: {
    type: "object", required: [],
    properties: { shared_drive_id: field("Shared drive ID", "Optional shared drive ID."), folder_id: field("Folder ID", "Only direct children of this folder; nested folders are not traversed.") },
    "x-broad-scope": { unless_any: ["folder_id"], message: "Without a folder, this source imports all supported files visible in the selected shared drive, or all files visible to the service account when no drive is selected." },
  },
  notion: {
    type: "object", required: [],
    properties: { database_id: field("Database ID", "Optional database to query."), parent_page_id: field("Parent page ID", "Only pages whose immediate parent is this page. Both filters apply when both are set.") },
    "x-broad-scope": { unless_any: ["database_id", "parent_page_id"], message: "Without a database or parent page, this source imports all pages shared with the Notion integration." },
  },
};
