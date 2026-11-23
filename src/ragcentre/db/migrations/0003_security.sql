ALTER TABLE provider_secrets ADD COLUMN destination_hash TEXT;
ALTER TABLE indexes ADD COLUMN broken INTEGER NOT NULL DEFAULT 0;
ALTER TABLE chunks ADD COLUMN doc_revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE index_records ADD COLUMN doc_revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE parse_cache ADD COLUMN content_sha256 TEXT;

CREATE INDEX parse_cache_content ON parse_cache(content_sha256);
CREATE INDEX documents_content ON documents(content_sha256);

UPDATE chunks AS c SET doc_revision = (
    SELECT r.doc_revision FROM node_runs r
    WHERE r.document_id = c.document_id AND r.node_fp = c.chunk_node_fp AND r.status = 'succeeded'
)
WHERE EXISTS (
    SELECT 1 FROM node_runs r
    WHERE r.document_id = c.document_id AND r.node_fp = c.chunk_node_fp AND r.status = 'succeeded'
);

UPDATE index_records AS ir SET doc_revision = coalesce((
    SELECT r.doc_revision FROM indexes i JOIN node_runs r ON r.corpus_id = i.corpus_id AND r.node_fp = i.node_fp
    WHERE i.id = ir.index_id AND r.document_id = ir.document_id AND r.status = 'succeeded'
), 0);

UPDATE indexes AS i SET broken = 1
WHERE EXISTS (
    SELECT 1 FROM indexes j
    WHERE j.id != i.id AND j.kind = i.kind AND j.physical_name = i.physical_name
);
