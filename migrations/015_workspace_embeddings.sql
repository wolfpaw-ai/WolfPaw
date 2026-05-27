-- Workspace doc embeddings — semantic search across the user's docs.
--
-- Mirrors the pattern already used by message_embeddings / plans /
-- skills / tools: one vector(1024) column, one ivfflat index over cosine
-- distance. Each workspace_files row (each *version* of a file) gets
-- its own embedding when the writer enqueues `embed_workspace_file_job`.
-- Older versions retain their embeddings; the search tool filters to
-- the latest version per filename so superseded text doesn't surface.

ALTER TABLE workspace_files
    ADD COLUMN IF NOT EXISTS embedding vector(1024);

CREATE INDEX IF NOT EXISTS workspace_files_embedding_ivf
    ON workspace_files USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
