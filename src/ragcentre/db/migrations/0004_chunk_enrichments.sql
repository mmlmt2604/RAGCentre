-- Text written by enrich nodes (chunk enrichment). Chunk ids stay those of the chunk node, so
-- sources that read enriched and plain text of the same chunks still fuse by chunk id.
CREATE TABLE chunk_enrichments (
  enrich_node_fp TEXT NOT NULL,
  chunk_id TEXT NOT NULL,
  document_id TEXT NOT NULL,
  doc_revision INTEGER NOT NULL,
  generated TEXT NOT NULL,                -- what this node's LLM call returned
  enrichment TEXT NOT NULL,               -- the prefix through this node (earlier enrich nodes first)
  PRIMARY KEY (enrich_node_fp, chunk_id)
);
CREATE INDEX chunk_enrichments_doc ON chunk_enrichments (enrich_node_fp, document_id);
CREATE INDEX chunk_enrichments_document ON chunk_enrichments (document_id);
