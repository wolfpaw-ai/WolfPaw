-- Wolfpaw — memory improvement step 4 (HNSW for conversational recall).
--
-- Swaps the `message_embeddings` vector index from IVFFlat to HNSW.
--
-- Why: `message_embeddings` is the fastest-growing vector index (one row
-- per persisted message) and it backs the per-thread recency-weighted
-- recall on the chat hot path. IVFFlat's recall degrades as the table
-- grows unless the k-means `lists` are re-tuned and the index is
-- periodically `VACUUM ANALYZE`d; HNSW keeps high recall-at-latency into
-- the millions of vectors with no clustering assumption and no post-
-- backfill maintenance step. At current scale the build is trivially
-- fast; a plain (non-CONCURRENT) CREATE INDEX is fine here since the
-- migrate step runs before the app accepts traffic.
--
-- m / ef_construction are pgvector's defaults, stated explicitly so the
-- graph parameters are visible and easy to tune later. Query-time recall
-- is governed by the `hnsw.ef_search` GUC (default 40) — not set here.
--
-- The other vector indexes (plans / skills / tools / workspace_files)
-- grow far more slowly and are left on IVFFlat for now; the same swap
-- applies to them if/when they warrant it.

DROP INDEX IF EXISTS message_embeddings_ivf;

CREATE INDEX IF NOT EXISTS message_embeddings_hnsw
    ON message_embeddings USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
