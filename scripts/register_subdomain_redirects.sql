-- Run once against the FastCAS database after taking a backup.
-- Append new callbacks without changing existing callbacks or client credentials.
BEGIN;
DO $$
BEGIN
  IF (SELECT count(*) FROM applications WHERE id IN ('fastread','fastwrite','news')) <> 3 THEN
    RAISE EXCEPTION 'Expected FastRead, FastWrite and News registrations';
  END IF;
END $$;
WITH desired(id, uri) AS (VALUES
  ('fastread', 'https://read.fastrd.io/api/auth/fastcas/callback'),
  ('fastwrite', 'https://write.fastrd.io/api/auth/fastcas/callback'),
  ('news', 'https://news.fastrd.io/api/auth/fastcas/callback')
), changed AS (
  UPDATE applications AS app
  SET config = jsonb_set(app.config, '{redirect_uris}',
    coalesce(nullif(app.config->'redirect_uris', 'null'::jsonb), '[]'::jsonb) || jsonb_build_array(desired.uri))
  FROM desired
  WHERE app.id = desired.id
    AND NOT coalesce(nullif(app.config->'redirect_uris', 'null'::jsonb), '[]'::jsonb) @> jsonb_build_array(desired.uri)
  RETURNING app.id, desired.uri
)
INSERT INTO audit_events(actor, action, target)
SELECT 'fastdeploy', 'add_redirect_uri', id || ' ' || uri FROM changed;
COMMIT;
