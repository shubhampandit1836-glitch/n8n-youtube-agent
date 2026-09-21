-- ============================================================
-- ShortsPilot — 003_pick_next_topic.sql (Anti-Fatigue Selector)
-- Step G-2: Atomic queue claim, genre alternation, cooldown.
-- ============================================================

CREATE OR REPLACE FUNCTION pick_next_topic(p_channel_id TEXT DEFAULT 'default')
RETURNS TABLE (
  id BIGINT,
  channel_id TEXT,
  game_id TEXT,
  genre TEXT,
  format TEXT,
  topic_title TEXT,
  topic_slug TEXT,
  priority INT
)
LANGUAGE plpgsql
AS $$
DECLARE
  v_last_genre TEXT;
  v_picked_id BIGINT;
BEGIN
  -- 1. Crash recovery: auto-reclaim topics stuck in processing > 30 mins
  UPDATE topic_queue
  SET status = 'pending', locked_at = NULL
  WHERE status = 'processing'
    AND locked_at < now() - INTERVAL '30 minutes';

  -- 2. Anti-fatigue check: find the genre of the most recently completed topic
  SELECT g.genre
  INTO v_last_genre
  FROM topic_queue tq
  JOIN games g ON tq.game_id = g.game_id
  WHERE tq.channel_id = p_channel_id
    AND tq.status = 'completed'
  ORDER BY tq.locked_at DESC NULLS LAST, tq.id DESC
  LIMIT 1;

  -- 3. Select candidate topic with priority, cooldown, and asset checks
  SELECT tq.id
  INTO v_picked_id
  FROM topic_queue tq
  LEFT JOIN games g ON tq.game_id = g.game_id
  WHERE tq.channel_id = p_channel_id
    AND tq.status = 'pending'
    -- Cooldown rule: game must not have been used within cooldown_days
    AND (
      tq.game_id IS NULL
      OR g.last_used_at IS NULL
      OR g.last_used_at < now() - (g.cooldown_days || ' days')::INTERVAL
    )
    -- Rule #1 Guard: gameplay_highlight requires active owned clips in library
    AND (
      tq.format != 'gameplay_highlight'
      OR EXISTS (
        SELECT 1 FROM gameplay_assets ga
        WHERE ga.game_id = tq.game_id AND ga.is_active = TRUE
      )
    )
  ORDER BY
    -- Genre alternation: penalize matching the last completed video's genre
    (CASE WHEN v_last_genre IS NOT NULL AND g.genre = v_last_genre THEN 1 ELSE 0 END) ASC,
    tq.priority DESC,
    tq.created_at ASC
  LIMIT 1
  FOR UPDATE OF tq SKIP LOCKED;

  -- 4. Transition candidate row to processing
  IF v_picked_id IS NOT NULL THEN
    UPDATE topic_queue
    SET status = 'processing',
        locked_at = now()
    WHERE topic_queue.id = v_picked_id;

    RETURN QUERY
    SELECT
      tq.id,
      tq.channel_id,
      tq.game_id,
      COALESCE(g.genre, 'general'),
      tq.format,
      tq.topic_title,
      tq.topic_slug,
      tq.priority
    FROM topic_queue tq
    LEFT JOIN games g ON tq.game_id = g.game_id
    WHERE tq.id = v_picked_id;
  END IF;
END;
$$;
