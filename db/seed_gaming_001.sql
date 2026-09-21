-- ============================================================
-- Seed: initial games registry + test topics. Safe to re-run.
-- ============================================================
INSERT INTO games (game_id, name, genre, cooldown_days) VALUES
  ('minecraft',        'Minecraft',          'sandbox',       2),
  ('bgmi',             'BGMI',               'battle-royale', 2),
  ('black-myth-wukong','Black Myth: Wukong', 'action-rpg',    3),
  ('resident-evil',    'Resident Evil',      'horror',        3),
  ('the-last-of-us',   'The Last of Us',     'horror',        3)
ON CONFLICT (game_id) DO NOTHING;

-- Test topics — one per format. gameplay_highlight will not render
-- until G-4 indexes your own clips into gameplay_assets.
INSERT INTO topic_queue (channel_id, game_id, format, topic_title, topic_slug, priority) VALUES
  ('default', 'minecraft',     'facts',              '5 Minecraft secrets jo 99% players miss', 'minecraft-5-secrets-99-miss',  2),
  ('default', 'resident-evil', 'myth',               'Resident Evil ki sabse dark unsolved myth', 'resident-evil-dark-myth',    2),
  ('default', NULL,            'top_3',              'Top 3 horror games of all time',           'top-3-horror-games',         1),
  ('default', NULL,            'news',               'Is hafte ki 3 badi gaming updates',        'weekly-gaming-3-updates',    1),
  ('default', 'bgmi',          'gameplay_highlight', 'BGMI 1v4 clutch — impossible survival',    'bgmi-1v4-clutch',            3)
ON CONFLICT DO NOTHING;
