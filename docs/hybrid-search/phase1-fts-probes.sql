-- =============================================================================
-- Phase 1 / Task 1.1 — Full-text search probe script (READ-ONLY on production)
-- =============================================================================
-- Purpose: let tasks 1.2 / 1.3 re-confirm the frozen lexical-contract facts
-- (regconfig = 'simple', simple-vs-english tokenization, index reachability) on
-- the *live* Hivemind database without reinterpretation.
--
-- SAFETY:
--   * SECTION A (SELECT probes) and SECTION B (EXPLAIN of existing objects) are
--     strictly read-only and safe to run against production with a read/login
--     role. They create/modify nothing.
--   * SECTION C (index-usage demo) creates and drops TEMP objects. Run it ONLY in
--     an isolated scratch schema / a database branch / the throwaway local
--     cluster described in phase1-lexical-contract.md §14. NEVER run SECTION C
--     against production.
--
-- Expected outputs below were captured 2026-07-28 on an isolated PostgreSQL 14.15
-- cluster and are recorded in docs/hybrid-search/phase1-lexical-contract.json
-- (simple_vs_english_evidence). Re-running should reproduce them bit-for-bit.
-- =============================================================================


-- =============================================================================
-- SECTION A — pure SELECT tokenization probes (safe on production)
-- =============================================================================

-- The two built-in configurations both exist.
SELECT string_agg(cfgname, ', ' ORDER BY cfgname) AS configs_present
FROM pg_ts_config WHERE cfgname IN ('simple','english');
-- expected: english, simple

-- A1. EXACT IDENTIFIERS — simple vs english (golden exact_name / spelling_variant)
SELECT 'WanVideoSampler' AS name,
       to_tsvector('simple',  'WanVideoSampler') AS simple_tsv,
       to_tsvector('english', 'WanVideoSampler') AS english_tsv;
-- expected simple:  'wanvideosampler':1
-- expected english: 'wanvideosampl':1     <- stemmer strips '-er' (decisive)

SELECT 'FLUX.1' AS name,
       to_tsvector('simple','FLUX.1') AS simple_tsv,
       to_tsvector('english','FLUX.1') AS english_tsv;
-- expected (both): 'flux.1':1

SELECT 'Wan 2.2' AS name,
       to_tsvector('simple','Wan 2.2') AS simple_tsv,
       to_tsvector('english','Wan 2.2') AS english_tsv;
-- expected (both): '2.2':2 'wan':1

SELECT 'model.safetensors' AS name,
       to_tsvector('simple','model.safetensors') AS tsv;
-- expected: 'model.safetensors':1

SELECT 'LTX-Video' AS name,
       to_tsvector('simple','LTX-Video') AS tsv;
-- expected: 'ltx':2 'ltx-video':1 'video':3

SELECT 'lightx2v_I2V_14B.safetensors' AS name,
       to_tsvector('simple','lightx2v_I2V_14B.safetensors') AS tsv;
-- expected: 'lightx2v_i2v_14b.safetensors':1

-- A2. STEMMING / STOPWORDS on natural prose (the only english advantage)
SELECT to_tsvector('simple',  'the message is not a control net running configs') AS simple_tsv,
       to_tsvector('english', 'the message is not a control net running configs') AS english_tsv;
-- expected simple:  keeps the/is/not/a; 'running','configs' literal
-- expected english: drops the/is/not/a; 'run','config' stemmed

-- A3. MULTILINGUAL / community text (Discord is global)
SELECT to_tsvector('simple',  '动漫 视频 anime video') AS simple_tsv,
       to_tsvector('english', '动漫 视频 anime video') AS english_tsv;
-- expected simple:  '动漫':1 '视频':2 'anime':3 'video':4
-- expected english: '动漫':1 '视频':2 'anim':3  'video':4   <- stems 'anime'

-- A4. CODE FRAGMENT tokenization (workflow_code / code_fragment gates)
SELECT to_tsvector('simple',
  'class WanVideoSampler(ModelSpec):' || E'\n' ||
  '    def __init__(self, lora_weight=0.8, num_frames=81):') AS code_simple;
-- expected lexemes include: class, wanvideosampler, modelspec, def, init, self,
--   lora, weight, 0.8, num, frames, 81  (symbols preserved, NOT stemmed)

-- A5. CONFIG MISMATCH (the core simple-vs-english resolution)
SELECT to_tsvector('english','WanVideoSampler') @@ websearch_to_tsquery('simple','WanVideoSampler')  AS simple_q_vs_eng_vec;
SELECT to_tsvector('english','WanVideoSampler') @@ websearch_to_tsquery('english','WanVideoSampler') AS eng_q_vs_eng_vec;
SELECT to_tsvector('simple','WanVideoSampler')  @@ websearch_to_tsquery('simple','WanVideoSampler')  AS simple_q_vs_sim_vec;
-- expected: FALSE, TRUE, TRUE  (a simple query cannot use an english-indexed vector)

-- A6. IDENTIFIER QUERY MISMATCH (why the exact-identifier arm is mandatory)
SELECT to_tsvector('simple','Wan 2.2') @@ websearch_to_tsquery('simple','Wan2.2') AS nospace_misses;
SELECT to_tsvector('simple','Wan 2.2') @@ websearch_to_tsquery('simple','Wan 2.2') AS spaced_hits;
-- expected: FALSE, TRUE   (normalize_identifier collapses both to 'wan22')

-- A7. QUERY CONSTRUCTORS on 'simple' (golden default + phrase arms)
SELECT websearch_to_tsquery('simple','FLUX.1 controlnet settings') AS fts_default;
SELECT phraseto_tsquery('simple','LTX-Video') AS phrase;
SELECT websearch_to_tsquery('simple','"block swap" -deluxe') AS fts_phrase_neg;
SELECT websearch_to_tsquery('simple','controlnet OR settings') AS fts_or;
-- websearch bare words are AND'd; quoted => adjacency; '-' => negation; OR supported.

-- A8. ts_rank normalization flag 32 (frozen rank flag)
SELECT ts_rank(to_tsvector('simple', repeat('class Foo ', 2000)),
               websearch_to_tsquery('simple','foo'), 32)  AS long_doc_norm32,
       ts_rank(to_tsvector('simple','class Foo bar'),
               websearch_to_tsquery('simple','foo'), 32)  AS short_doc_norm32;
-- long_doc_norm32 is DAMPENED relative to flag 0 (run flag 0 to compare); this is
-- why flag 32 is required so giant workflow-Python docs do not dominate ranking.

-- A9. WEIGHTED tsvector (resource-prose / distillation shapes)
SELECT setweight(to_tsvector('simple','WanVideoSampler'), 'A')
     || setweight(to_tsvector('simple','sampler wan alias'), 'B')
     || setweight(to_tsvector('simple','body mentions WanVideoSampler'), 'C') AS resource_prose_tsv;
SELECT setweight(to_tsvector('simple','What is the best upscale model?'), 'A')
     || setweight(to_tsvector('simple','for anime-style video'), 'B')
     || setweight(to_tsvector('simple','Use a 4x model then a 2x model.'), 'C') AS distillation_tsv;
-- A/B/C weight letters present (A=1.0 B=0.4 C=0.2 under ts_rank defaults).


-- =============================================================================
-- SECTION B — EXPLAIN of the EXISTING live index expression (safe on production)
-- =============================================================================
-- Confirm the live Discord index expression and that a 'simple' query over the
-- underlying table will NOT use it (it will seq/bitmap-scan or use a different
-- index). Run with COSTS OFF for a compact plan.

-- B1. What is the live message FTS index expression?
SELECT indexname, indexdef
FROM pg_indexes
WHERE tablename = 'discord_messages'
  AND indexdef LIKE '%to_tsvector%';
-- expected: idx_discord_messages_content_fts USING gin (to_tsvector('english'::regconfig, content))

-- B2. Plan for a 'simple' query (canonical). Without a 'simple' index on prod it
--     cannot use the english expression index. (Read-only EXPLAIN.)
EXPLAIN (COSTS OFF)
SELECT message_id
FROM public.discord_messages
WHERE to_tsvector('simple'::regconfig, coalesce(content,''))
      @@ websearch_to_tsquery('simple','WanVideoSampler')
  AND is_deleted = false;
-- pre-1.3: Seq/Bitmap scan, the english index is NOT used.


-- =============================================================================
-- SECTION C — index-usage demo (ISOLATED SCRATCH ONLY — NOT production)
-- =============================================================================
-- Reproduces the §1.1 EXPLAIN proof (a 'simple' query uses the simple expression
-- index and cannot use the english one). Run ONLY in a scratch schema / DB branch
-- / throwaway local cluster. Creates + drops a temp-style table.
--
-- BEGIN;  -- only if scratch supports it
-- DROP TABLE IF EXISTS scratch_probe_msgs;
-- CREATE TABLE scratch_probe_msgs (message_id bigint, content text);
-- INSERT INTO scratch_probe_msgs
--   SELECT g, CASE WHEN g=777 THEN 'exact needle WanVideoSampler'
--                  ELSE 'filler message '||g END
--   FROM generate_series(1,50000) g;
-- CREATE INDEX scratch_msgs_english_fts
--   ON scratch_probe_msgs USING gin (to_tsvector('english'::regconfig, content));
-- CREATE INDEX scratch_msgs_simple_fts
--   ON scratch_probe_msgs USING gin (to_tsvector('simple'::regconfig, content));
-- ANALYZE scratch_probe_msgs;
-- SET enable_seqscan = off;
-- EXPLAIN (COSTS OFF)
-- SELECT message_id FROM scratch_probe_msgs
-- WHERE to_tsvector('simple'::regconfig, content) @@ websearch_to_tsquery('simple','needle');
-- -- expected: Bitmap Index Scan on scratch_msgs_simple_fts (english index UNREACHABLE)
-- DROP TABLE scratch_probe_msgs;
-- RESET enable_seqscan;
-- COMMIT;
