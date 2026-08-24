-- 015. CSRF TOKENS FOR THE BROWSER SESSION
--
-- The JSON API authenticates with a bearer token, which a browser will not send
-- on its own -- so cross-site request forgery does not apply to it. The admin
-- web UI is different: it authenticates with a cookie, and a cookie IS sent
-- automatically on any request the browser is tricked into making, including a
-- form POST from another site.
--
-- So every state-changing form carries a token tied to the session, checked on
-- submit. SameSite=Strict on the cookie is the first line of defence; this is
-- the second, because SameSite is a browser behaviour and the consequence of
-- getting this wrong is somebody else's national ID number.
--
-- Stored per session rather than derived from a server secret so that it dies
-- with the session -- no separate rotation to remember.

BEGIN;

ALTER TABLE staff_sessions ADD COLUMN csrf_token TEXT;

COMMIT;
