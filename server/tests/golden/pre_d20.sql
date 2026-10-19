-- A database as server.db.init_db created it before D20 (main at dae54e1): every object's
-- DDL in creation order, then rows in every table D20 migrates. Frozen: it IS the old
-- shape, so it is never regenerated from newer code.
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
CREATE VIRTUAL TABLE entries_fts USING fts5(content_md, content='entries', content_rowid='id');
CREATE TRIGGER entries_ai_fts AFTER INSERT ON entries BEGIN INSERT INTO entries_fts(rowid, content_md) VALUES (new.id, new.content_md); END;
CREATE TABLE entry_item_types (entry_id INTEGER NOT NULL REFERENCES entries(id), item_type TEXT NOT NULL CHECK (item_type IN ('decision', 'method', 'task', 'fact')), UNIQUE(entry_id, item_type));
CREATE INDEX idx_entry_item_types_type ON entry_item_types(item_type, entry_id);
CREATE TABLE entry_links (id INTEGER PRIMARY KEY, from_entry INTEGER NOT NULL REFERENCES entries(id), to_entry INTEGER NOT NULL REFERENCES entries(id), kind TEXT NOT NULL CHECK(kind IN ('supersedes', 'duplicates')), rationale TEXT, created_at TEXT NOT NULL, UNIQUE(from_entry, to_entry, kind));
CREATE INDEX idx_entry_links_to ON entry_links(to_entry);
CREATE TABLE entry_items (id INTEGER PRIMARY KEY, entry_id INTEGER NOT NULL REFERENCES entries(id), ordinal INTEGER NOT NULL CHECK (ordinal >= 1), item_type TEXT NOT NULL CHECK (item_type IN ('decision', 'method', 'task', 'fact')), labelled INTEGER NOT NULL CHECK (labelled IN (0, 1)), text_md TEXT NOT NULL, UNIQUE (entry_id, ordinal));
CREATE VIRTUAL TABLE entry_items_fts USING fts5(text_md, content='entry_items', content_rowid='id');
CREATE TRIGGER entry_items_ai_fts AFTER INSERT ON entry_items BEGIN INSERT INTO entry_items_fts(rowid, text_md) VALUES (new.id, new.text_md); END;
CREATE TABLE item_links (id INTEGER PRIMARY KEY, from_item INTEGER NOT NULL REFERENCES entry_items(id), to_item INTEGER NOT NULL REFERENCES entry_items(id), kind TEXT NOT NULL CHECK (kind IN ('supersedes', 'duplicates')), rationale TEXT, created_by INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, revoked_at TEXT, revoked_by INTEGER REFERENCES users(id), revoke_reason TEXT);
CREATE UNIQUE INDEX idx_item_links_active ON item_links(from_item, to_item, kind) WHERE revoked_at IS NULL;
CREATE INDEX idx_item_links_to ON item_links(to_item);
CREATE TABLE device_tokens (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), token_hash TEXT UNIQUE NOT NULL, device_name TEXT NOT NULL, issued_at TEXT NOT NULL, expires_at TEXT NOT NULL, revoked_at TEXT, replaced_by INTEGER REFERENCES device_tokens(id));
CREATE INDEX idx_device_tokens_user ON device_tokens(user_id);
CREATE INDEX idx_device_tokens_replaced_by ON device_tokens(replaced_by);
CREATE TABLE enrolment_codes (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), code_hash TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, redeemed_at TEXT, device_token_id INTEGER REFERENCES device_tokens(id));
CREATE TABLE ui_login_codes (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), device_token_id INTEGER REFERENCES device_tokens(id), code_hash TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, redeemed_at TEXT);
CREATE TABLE ui_sessions (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), device_token_id INTEGER REFERENCES device_tokens(id), session_hash TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE INDEX idx_ui_sessions_device ON ui_sessions(device_token_id);
CREATE TABLE recall_events (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), project_id INTEGER NOT NULL REFERENCES projects(id), source TEXT NOT NULL CHECK (source IN ('skill', 'cli')), query TEXT, result_count INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE recall_feedback (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), project_id INTEGER NOT NULL REFERENCES projects(id), item_id INTEGER NOT NULL REFERENCES entry_items(id), verdict TEXT NOT NULL CHECK (verdict IN ('useful', 'not_applicable', 'stale')), note TEXT NOT NULL, feedback_hash TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL);
INSERT INTO "enrolment_codes" VALUES (1, 1, '8888888888888888888888888888888888888888888888888888888888888888', '2026-10-01T09:00:00Z', '2026-12-30T09:00:00Z', '2026-10-01T09:00:00Z', 1);
INSERT INTO "enrolment_codes" VALUES (2, 3, '9999999999999999999999999999999999999999999999999999999999999999', '2026-10-01T09:00:00Z', '2026-12-30T09:00:00Z', NULL, NULL);
INSERT INTO "ui_login_codes" VALUES (1, 1, 2, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', '2026-10-01T09:00:00Z', '2026-12-30T09:00:00Z', '2026-10-01T09:00:00Z');
INSERT INTO "device_tokens" VALUES (1, 1, '5555555555555555555555555555555555555555555555555555555555555555', 'laptop', '2026-10-01T09:00:00Z', '2026-12-30T09:00:00Z', NULL, 2);
INSERT INTO "device_tokens" VALUES (2, 1, '6666666666666666666666666666666666666666666666666666666666666666', 'laptop', '2026-10-01T09:00:00Z', '2026-12-30T09:00:00Z', NULL, NULL);
INSERT INTO "device_tokens" VALUES (3, 2, '7777777777777777777777777777777777777777777777777777777777777777', 'desktop', '2026-10-01T09:00:00Z', '2026-12-30T09:00:00Z', '2026-10-01T09:00:00Z', NULL);
INSERT INTO "ui_sessions" VALUES (1, 1, 2, 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', '2026-10-01T09:00:00Z', '2026-12-30T09:00:00Z');
INSERT INTO "users" VALUES (1, 'ana', 'ana@example.test', '1111111111111111111111111111111111111111111111111111111111111111', 1, '2026-10-01T09:00:00Z');
INSERT INTO "users" VALUES (2, 'bo', 'bo@example.test', '2222222222222222222222222222222222222222222222222222222222222222', 0, '2026-10-01T09:00:00Z');
INSERT INTO "users" VALUES (3, 'cy', 'cy@example.test', '3333333333333333333333333333333333333333333333333333333333333333', 1, '2026-10-01T09:00:00Z');
INSERT INTO "entries" VALUES (1, 1, 1, 's-1', NULL, NULL, 'chose sqlite over postgres', '4444444444444444444444444444444444444444444444444444444444444444', NULL, NULL, '2026-10-01T09:00:00Z');
INSERT INTO "projects" VALUES (1, 'github.com/example/repo', 'repo', 1, '2026-10-01T09:00:00Z');
