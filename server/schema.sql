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
