-- Additive migration: preserves every existing row and column.
ALTER TABLE social_posts ADD COLUMN platform_post_url TEXT NULL AFTER platform_post_id;
