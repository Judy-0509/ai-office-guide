-- dataplat's own SQLite file (separate from the team's source database and from aioffice's
-- aioffice.db) -- see dataplat/store.py. A "load" is one snapshot of one dataset's rows, taken
-- from the source query at `dataplat.snapshot` run time.

CREATE TABLE IF NOT EXISTS datasets (
  name TEXT PRIMARY KEY,
  title TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS loads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dataset TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  rows INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,          -- ok | error | skipped_duplicate
  error TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  -- version_label/version_sort: '' for every load when the dataset has no `version_column`
  -- (source.yaml) -- the whole versioning scheme below then collapses to exactly today's
  -- single-lineage behavior. See dataplat/store.py's module docstring for the sort scheme.
  version_label TEXT NOT NULL DEFAULT '',
  version_sort TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_loads_dataset ON loads(dataset);
CREATE INDEX IF NOT EXISTS idx_loads_dataset_status ON loads(dataset, status);
CREATE INDEX IF NOT EXISTS idx_loads_dataset_version ON loads(dataset, version_label);

CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  load_id INTEGER NOT NULL REFERENCES loads(id),
  dataset TEXT NOT NULL,
  metric TEXT NOT NULL DEFAULT '',
  entity TEXT NOT NULL DEFAULT '',
  region TEXT NOT NULL DEFAULT '',
  period TEXT NOT NULL DEFAULT '',
  period_sort TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  value REAL,
  unit TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_obs_load ON observations(load_id);
CREATE INDEX IF NOT EXISTS idx_obs_query
  ON observations(dataset, metric, entity, region, source, period_sort);

CREATE TABLE IF NOT EXISTS load_reports (
  load_id INTEGER PRIMARY KEY REFERENCES loads(id),
  report TEXT NOT NULL
);

-- Chat spec cache: normalized message + catalog_version -> the spec dataplat.chat resolved for
-- it (from any path: rule/llm), so a repeated question skips the LLM call entirely while the
-- catalog hasn't changed. See dataplat/store.py's catalog_version()/get_cached_spec().
CREATE TABLE IF NOT EXISTS chat_spec_cache (
  message_key TEXT NOT NULL,
  catalog_version TEXT NOT NULL,
  spec_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (message_key, catalog_version)
);

-- Batch-generated summaries (dataplat.digest, run after a snapshot -- waiting on the LLM is
-- harmless there). kind: "version_summary" | "load_summary" | "entity_note". `table_json` is
-- the code-computed table the text was number-checked against.
CREATE TABLE IF NOT EXISTS digests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dataset TEXT NOT NULL,
  load_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  text TEXT NOT NULL,
  table_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_digests_dataset ON digests(dataset, load_id);

-- Async "AI에게 물어보기" queue (POST /api/ask): a single FIFO worker thread processes these
-- via chat.answer_via_llm() -- see dataplat/ask.py. Persisted so a server restart never loses a
-- finished answer; any job still "running" at startup is stale (the process that was running it
-- is gone) and gets reaped to status="error" -- see store.reap_stale_ask_jobs().
CREATE TABLE IF NOT EXISTS ask_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message TEXT NOT NULL,
  history_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'queued',  -- queued | running | done | error | cancelled
  result_json TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ask_jobs_status ON ask_jobs(status, id);

-- Learned aliases (Round 8 "learning loop"): a phrase the fast path/alias config didn't resolve
-- on its own, but an /api/ask job (LLM) or a clarify-button pick did -- reused by the fast path
-- next time the same phrase comes up. `values_json` is a JSON list of resolved catalog values.
CREATE TABLE IF NOT EXISTS learned_aliases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dataset TEXT NOT NULL,
  dim TEXT NOT NULL,          -- entities | regions | metrics
  phrase TEXT NOT NULL,
  values_json TEXT NOT NULL,
  source_job_id INTEGER,
  hit_count INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  UNIQUE (dataset, dim, phrase)
);

-- Latest = the newest ok load of the HIGHEST version_label (lexical max of version_sort).
-- Without a version_column every load's version_sort is '' (all equal), so this reduces to
-- "the newest ok load per dataset" -- today's exact behavior.
CREATE VIEW IF NOT EXISTS latest_versions AS
SELECT dataset, MAX(version_sort) AS max_version_sort
FROM loads
WHERE status = 'ok'
GROUP BY dataset;

CREATE VIEW IF NOT EXISTS latest_loads AS
SELECT l.dataset, MAX(l.id) AS load_id
FROM loads l
JOIN latest_versions lv ON l.dataset = lv.dataset AND l.version_sort = lv.max_version_sort
WHERE l.status = 'ok'
GROUP BY l.dataset;

CREATE VIEW IF NOT EXISTS latest_observations AS
SELECT o.*
FROM observations o
JOIN latest_loads ll ON o.load_id = ll.load_id;
