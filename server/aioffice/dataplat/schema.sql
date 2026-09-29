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
