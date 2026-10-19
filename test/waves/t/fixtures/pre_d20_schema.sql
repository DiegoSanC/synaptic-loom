-- FROZEN FIXTURE (wave t): the database schema as it stood BEFORE D20 (main at dae54e1).
-- `server/schema.sql` verbatim, then the D19-era additive objects of `server/auth.py` and
-- `server/ui_auth.py` copied as literals. Wave t seeds a volume with it to prove §6.4's
-- migration rule on a pre-migration copy: it must never be regenerated from the code under
-- test, which is exactly the code whose migration it grades.
CREATE TABLE users (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
  token_hash TEXT NOT NULL, active INTEGER DEFAULT 1, created_at TEXT NOT NULL
);
CREATE TABLE projects (
  id INTEGER PRIMARY KEY,
  canonical_remote TEXT UNIQUE,            -- "github.com/org/repo" (NULL if path-only)
  name TEXT NOT NULL,
  created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL
);
CREATE TABLE project_paths (                -- helper: discovery + fallback with no remote
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  user_id INTEGER NOT NULL REFERENCES users(id),
  path TEXT NOT NULL, hostname TEXT, created_at TEXT NOT NULL,
  UNIQUE(user_id, hostname, path)
);
CREATE TABLE entries (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  user_id INTEGER NOT NULL REFERENCES users(id),
  session_id TEXT NOT NULL,
  covered_from TEXT, covered_to TEXT,       -- uuids of the summarized range (evidence anchor)
  content_md TEXT NOT NULL,
  content_hash TEXT UNIQUE NOT NULL,        -- sha256(range + model + prompt_version + delta)
  model TEXT, prompt_version TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_entries_project_date ON entries(project_id, created_at DESC);
CREATE TABLE device_tokens (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), token_hash TEXT UNIQUE NOT NULL, device_name TEXT NOT NULL, issued_at TEXT NOT NULL, expires_at TEXT NOT NULL, revoked_at TEXT, replaced_by INTEGER REFERENCES device_tokens(id));
CREATE INDEX idx_device_tokens_user ON device_tokens(user_id);
CREATE INDEX idx_device_tokens_replaced_by ON device_tokens(replaced_by);
CREATE TABLE enrolment_codes (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), code_hash TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, redeemed_at TEXT, device_token_id INTEGER REFERENCES device_tokens(id));
CREATE TABLE ui_login_codes (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), device_token_id INTEGER REFERENCES device_tokens(id), code_hash TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, redeemed_at TEXT);
CREATE TABLE ui_sessions (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), device_token_id INTEGER REFERENCES device_tokens(id), session_hash TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE INDEX idx_ui_sessions_device ON ui_sessions(device_token_id);
