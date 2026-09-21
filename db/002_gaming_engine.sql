-- ============================================================
-- ShortsPilot — 002_gaming_engine.sql (Gaming Content Engine)
-- Step G-1. Registry, footage library, topic work-queue.
-- Safe to re-run: IF NOT EXISTS throughout.
--
-- Roles: topics (001) = produced-history/dedupe ledger.
--        topic_queue (002) = FUTURE work queue. On completion
--        a queue row is inserted into topics and marked done (G-6).
-- ============================================================

-- 1. GAMES REGISTRY
CREATE TABLE IF NOT EXISTS games (
  game_id       TEXT PRIMARY KEY,            -- slug: 'minecraft', 'bgmi', ...
  name          TEXT NOT NULL,
  genre         TEXT NOT NULL,               -- 'sandbox','battle-royale','action-rpg','horror',...
  cooldown_days INT  NOT NULL DEFAULT 2
                CONSTRAINT games_cooldown_chk CHECK (cooldown_days BETWEEN 0 AND 30),
  last_used_at  TIMESTAMPTZ,                 -- set on upload completion (G-6); drives 48h+ rotation
  is_active     BOOLEAN NOT NULL DEFAULT TRUE
);

-- 2. GAMEPLAY ASSETS — OWN CAPTURES ONLY (product rule, see docs/)
CREATE TABLE IF NOT EXISTS gameplay_assets (
  asset_id      BIGSERIAL PRIMARY KEY,
  game_id       TEXT NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
  file_path     TEXT NOT NULL UNIQUE,        -- container path e.g. /data/assets/gameplay/bgmi/clutch_01.mp4
  duration_sec  REAL,
  tags          TEXT[] NOT NULL DEFAULT '{}',-- ['combat','boss_fight','scary','clutch',...]
  usage_count   INT  NOT NULL DEFAULT 0,
  last_used_at  TIMESTAMPTZ,
  x_offset      REAL NOT NULL DEFAULT 0.5    -- horizontal crop focus 0(left)..1(right); FPS = 0.5
                CONSTRAINT gameplay_xoff_chk CHECK (x_offset BETWEEN 0 AND 1),
  is_active     BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_gameplay_assets_game ON gameplay_assets (game_id);
CREATE INDEX IF NOT EXISTS idx_gameplay_assets_tags ON gameplay_assets USING GIN (tags);

-- 3. TOPIC QUEUE — future work, one row per planned video
CREATE TABLE IF NOT EXISTS topic_queue (
  id                BIGSERIAL PRIMARY KEY,
  channel_id        TEXT NOT NULL DEFAULT 'default',
  game_id           TEXT REFERENCES games(game_id) ON DELETE SET NULL,  -- NULL = cross-game topic
  format            TEXT NOT NULL
                    CONSTRAINT tq_format_chk CHECK (format IN
                      ('facts', 'myth', 'top_3', 'news', 'gameplay_highlight')),
  topic_title       TEXT NOT NULL,
  topic_slug        TEXT NOT NULL,           -- lowercase-hyphens; matches topics.topic_slug convention
  status            TEXT NOT NULL DEFAULT 'pending'
                    CONSTRAINT tq_status_chk CHECK (status IN
                      ('pending', 'processing', 'completed', 'skipped')),
  priority          INT  NOT NULL DEFAULT 1, -- higher = sooner
  locked_at         TIMESTAMPTZ,             -- crash recovery: processing + locked_at older than 30min -> pending
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_run_id  TEXT REFERENCES runs(run_id),
  CONSTRAINT topic_queue_channel_slug_key UNIQUE (channel_id, topic_slug)
);

CREATE INDEX IF NOT EXISTS idx_topic_queue_pick
  ON topic_queue (channel_id, status, priority DESC);
