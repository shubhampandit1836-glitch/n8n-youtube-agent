-- ============================================================
-- ShortsPilot — pipeline_db schema (v1)
-- Step 1-B. Idempotency & state ledger.
-- Safe to re-run: every statement is IF NOT EXISTS / OR REPLACE.
--
-- Conventions:
--   * All timestamps TIMESTAMPTZ, stored UTC.
--   * channel_id defaults to 'default' until Phase 3 multi-tenancy.
-- ============================================================

-- 1. TOPICS — duplicate-topic guard (slug-level; embeddings land in Phase 2)
CREATE TABLE IF NOT EXISTS topics (
  id          BIGSERIAL PRIMARY KEY,
  topic_slug  TEXT NOT NULL,
  topic_text  TEXT NOT NULL,
  channel_id  TEXT NOT NULL DEFAULT 'default',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT topics_channel_slug_key UNIQUE (channel_id, topic_slug)
);

-- 2. RUNS — one row per pipeline execution
CREATE TABLE IF NOT EXISTS runs (
  run_id            TEXT PRIMARY KEY,
  channel_id        TEXT NOT NULL DEFAULT 'default',
  status            TEXT NOT NULL DEFAULT 'running'
                    CONSTRAINT runs_status_chk CHECK (status IN
                      ('running', 'succeeded', 'failed', 'dup_skipped')),
  stage             TEXT
                    CONSTRAINT runs_stage_chk CHECK (stage IN
                      ('script', 'eval', 'tts', 'visuals', 'render', 'metadata', 'uploaded')),
  topic_slug        TEXT,
  title             TEXT,
  error             TEXT,
  video_path        TEXT,
  youtube_video_id  TEXT,
  cost_json         JSONB,
  started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_runs_channel_status ON runs (channel_id, status);
CREATE INDEX IF NOT EXISTS idx_runs_topic_slug     ON runs (topic_slug);

-- 3. UPLOADS — the no-double-upload guarantee
CREATE TABLE IF NOT EXISTS uploads (
  run_id            TEXT PRIMARY KEY REFERENCES runs(run_id),
  youtube_video_id  TEXT UNIQUE NOT NULL,
  title             TEXT,
  privacy           TEXT NOT NULL DEFAULT 'private',
  quota_units       INT  NOT NULL DEFAULT 1600,
  uploaded_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 4. QUOTA_USAGE — daily quota ledger
CREATE TABLE IF NOT EXISTS quota_usage (
  usage_date  DATE NOT NULL,
  service     TEXT NOT NULL DEFAULT 'youtube',
  units       INT  NOT NULL DEFAULT 0,
  PRIMARY KEY (usage_date, service)
);

-- updated_at auto-maintenance on runs
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS runs_set_updated_at ON runs;
CREATE TRIGGER runs_set_updated_at
  BEFORE UPDATE ON runs
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();
